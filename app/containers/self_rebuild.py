"""In-place rebuild of the master container itself.

The master already speaks to the host Docker daemon (it launches user
containers), so it can refresh *itself* to the newest published image without
any host-level SSH/SSM access:

1. Inspect the running master container to capture its full run spec
   (image ref, env, mounts, ports, networks, restart policy, labels).
2. Pull the newest image for that ref.
3. Launch a short-lived **rebuilder** container — the freshly pulled master
   image, run with ``python -m app.containers.self_rebuild`` — that waits for
   this process to finish replying, removes the old master, and recreates it
   from the captured spec.

Because the rebuilder is a separate container with the Docker socket mounted,
it survives the old master being torn down and can bring the new one up.

The named data/volume mounts are reused verbatim, so Qdrant data and config
survive the rebuild.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time

from app.containers.provisioner import _docker_client

logger = logging.getLogger(__name__)

REBUILD_SPEC_ENV = "BELLEQ_REBUILD_SPEC"


def _self_container():
    """The container this process runs in (its id is the hostname)."""
    client = _docker_client()
    return client, client.containers.get(socket.gethostname())


def _binds_to_volumes(binds: list[str]) -> dict:
    """Turn HostConfig.Binds ("src:dst[:mode]") into docker-py's volumes dict."""
    volumes: dict[str, dict] = {}
    for b in binds or []:
        parts = b.split(":")
        if len(parts) >= 2:
            src, dst = parts[0], parts[1]
            mode = parts[2] if len(parts) >= 3 else "rw"
            volumes[src] = {"bind": dst, "mode": mode}
    return volumes


def _port_bindings_to_ports(port_bindings: dict) -> dict:
    """Turn HostConfig.PortBindings into docker-py's ports dict."""
    ports: dict = {}
    for container_port, bindings in (port_bindings or {}).items():
        if not bindings:
            continue
        host_port = bindings[0].get("HostPort")
        ports[container_port] = int(host_port) if host_port else None
    return ports


def build_self_spec(new_image: str) -> dict:
    """Capture the running master's run spec so it can be recreated verbatim."""
    _client, me = _self_container()
    attrs = me.attrs
    config = attrs.get("Config", {})
    host_config = attrs.get("HostConfig", {})
    networks = list((attrs.get("NetworkSettings", {}).get("Networks", {}) or {}).keys())
    return {
        "image": new_image,
        "name": (me.name or "").lstrip("/"),
        "env": config.get("Env", []) or [],
        "labels": config.get("Labels", {}) or {},
        "binds": host_config.get("Binds") or [],
        "port_bindings": host_config.get("PortBindings") or {},
        "restart_policy": host_config.get("RestartPolicy") or {},
        "networks": networks,
        "mem_limit": host_config.get("Memory") or 0,
        "nano_cpus": host_config.get("NanoCpus") or 0,
    }


def _prune_images(client) -> None:
    """Remove unused images + build cache to free disk. Best-effort; in-use
    images (running containers) are kept, volumes are never touched."""
    try:
        # dangling=False → prune ALL images not used by a container, not just
        # untagged ones (that's what reclaims old belleq-* versions).
        res = client.images.prune(filters={"dangling": False})
        reclaimed = res.get("SpaceReclaimed", 0)
        logger.info("master_self_rebuild_pruned reclaimed_bytes=%s", reclaimed)
    except Exception:  # noqa: BLE001 — pruning must never block a rebuild
        logger.warning("master_self_rebuild_prune_failed", exc_info=True)


def trigger_rebuild() -> dict:
    """Pull the newest master image and hand off recreation to a rebuilder.

    Returns immediately (the actual swap happens a few seconds later in the
    detached rebuilder), so the HTTP response can flush before this container
    is stopped.
    """
    client, me = _self_container()
    image = me.attrs.get("Config", {}).get("Image", "")
    if not image:
        raise RuntimeError("Could not determine the master image reference")

    # Reclaim disk BEFORE pulling — old image layers pile up on the host and a
    # full disk makes `pull` fail silently ("no space left on device"), leaving
    # the master pinned to the stale cached image. Prunes unused images only
    # (images in use by a running container are kept); never touches volumes.
    _prune_images(client)

    logger.info("master_self_rebuild_pull image=%s", image)
    client.images.pull(image)

    spec = build_self_spec(image)
    rebuilder_name = f"{spec['name']}-rebuilder"

    # Clean up any leftover rebuilder from a previous run.
    try:
        client.containers.get(rebuilder_name).remove(force=True)
    except Exception:  # noqa: BLE001
        pass

    client.containers.run(
        image,
        command=["python", "-m", "app.containers.self_rebuild"],
        environment={REBUILD_SPEC_ENV: json.dumps(spec)},
        volumes={"/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"}},
        network=spec["networks"][0] if spec["networks"] else None,
        name=rebuilder_name,
        detach=True,
        remove=True,
    )
    logger.info("master_self_rebuild_handed_off image=%s rebuilder=%s", image, rebuilder_name)
    return {"status": "rebuilding", "image": image}


def _recreate_from_spec(spec: dict) -> None:
    """(Runs inside the rebuilder.) Replace the master with the new image."""
    client = _docker_client()
    name = spec["name"]

    # Give the old master a moment to finish responding to the rebuild request.
    time.sleep(5)

    try:
        old = client.containers.get(name)
        old.remove(force=True)
        logger.info("master_old_removed name=%s", name)
    except Exception:  # noqa: BLE001
        logger.warning("master_old_remove_failed name=%s", name, exc_info=True)

    run_kwargs: dict = {
        "image": spec["image"],
        "name": name,
        "detach": True,
        "environment": spec.get("env", []),
        "labels": spec.get("labels", {}),
        "volumes": _binds_to_volumes(spec.get("binds", [])),
    }
    ports = _port_bindings_to_ports(spec.get("port_bindings", {}))
    if ports:
        run_kwargs["ports"] = ports
    rp_name = (spec.get("restart_policy") or {}).get("Name")
    if rp_name:
        run_kwargs["restart_policy"] = {"Name": rp_name}
    if spec.get("mem_limit"):
        run_kwargs["mem_limit"] = spec["mem_limit"]
    if spec.get("nano_cpus"):
        run_kwargs["nano_cpus"] = spec["nano_cpus"]
    networks = spec.get("networks", [])
    if networks:
        run_kwargs["network"] = networks[0]

    client.containers.run(**run_kwargs)
    logger.info("master_recreated name=%s image=%s", name, spec["image"])

    # Reconnect any additional networks beyond the first.
    for net in networks[1:]:
        try:
            client.networks.get(net).connect(name)
        except Exception:  # noqa: BLE001
            logger.warning("master_network_reconnect_failed net=%s", net, exc_info=True)


if __name__ == "__main__":  # Entry point for the rebuilder container.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raw = os.environ.get(REBUILD_SPEC_ENV)
    if not raw:
        raise SystemExit(f"{REBUILD_SPEC_ENV} not set")
    _recreate_from_spec(json.loads(raw))
