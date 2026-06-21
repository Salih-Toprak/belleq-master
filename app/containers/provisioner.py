"""Launch and tear down belleq-user containers via the host Docker daemon."""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class ProvisionError(Exception):
    """Raised when a user container cannot be provisioned."""

    def __init__(self, message: str, *, status_code: int = 500) -> None:
        super().__init__(message)
        self.status_code = status_code


def _docker_client():
    """Return a docker client bound to the mounted socket.

    Imported lazily so the rest of the master still works when the docker
    SDK or socket is unavailable (the error surfaces only on provisioning).
    """
    try:
        import docker
    except ImportError as e:  # pragma: no cover - dependency missing
        raise ProvisionError(
            "docker SDK not installed in the master image", status_code=500
        ) from e
    try:
        return docker.from_env()
    except Exception as e:  # noqa: BLE001
        raise ProvisionError(
            "Cannot reach the Docker daemon. Mount /var/run/docker.sock into "
            "the master container.",
            status_code=503,
        ) from e


def _container_env(
    *,
    container_name: str,
    api_key: str,
    user_id: str,
    qdrant_collection: str | None = None,
    vector_db: dict | None = None,
    extraction: dict | None = None,
) -> dict[str, str]:
    """Environment passed to the spawned user container.

    Defaults to the host's shared qdrant with a per-context collection. If a
    ``vector_db`` config is supplied (bring-your-own provider), it overrides the
    backend/url/key/collection so a context can point at the customer's own
    vector database instead of the shared one.
    """
    collection = qdrant_collection or settings.qdrant_collection
    env = {
        "USER_ID": user_id or container_name,
        "DISPLAY_NAME": container_name,
        "CONTAINER_TYPE": "user",
        "DATA_DIR": "/app/data",
        "MASTER_API_KEY": settings.admin_api_key or "",
        "USER_API_KEY": api_key or "",
        "VECTORDB_BACKEND": settings.vectordb_backend,
        "QDRANT_URL": settings.qdrant_url,
        "QDRANT_API_KEY": settings.qdrant_api_key or "",
        "QDRANT_COLLECTION": collection,
        "EMBEDDING_BACKEND": settings.embedding_backend,
        "OLLAMA_BASE_URL": settings.ollama_base_url,
        "OLLAMA_EMBED_MODEL": settings.ollama_embed_model,
        "EMBEDDING_VECTOR_SIZE": str(settings.embedding_vector_size),
        "OPENAI_API_KEY": settings.openai_api_key or "",
        "OPENAI_EMBED_MODEL": settings.openai_embed_model,
        "MCP_ENABLED": "true",
        "APP_HOST": "0.0.0.0",
        "APP_PORT": str(settings.user_container_port),
        "LOG_LEVEL": "INFO",
    }

    # Conversation fact-extraction — the (static) backend pushes these down in
    # the provision request so the ephemeral master never stores the keys. The
    # master's own env (settings.*) is only a fallback for standalone use.
    ex = extraction or {}
    env["CONVERSATION_EXTRACTION_ENABLED"] = (
        "true" if ex.get("enabled", settings.conversation_extraction_enabled) else "false"
    )
    env["EXTRACTION_BACKEND"] = ex.get("backend") or settings.extraction_backend
    env["GEMINI_API_KEY"] = ex.get("gemini_api_key", settings.gemini_api_key) or ""
    env["GEMINI_MODEL"] = ex.get("gemini_model") or settings.gemini_model
    env["ANTHROPIC_API_KEY"] = ex.get("anthropic_api_key", settings.extraction_anthropic_api_key) or ""
    env["EXTRACTION_MODEL"] = ex.get("extraction_model") or settings.extraction_model
    # Agent web access (Tavily) — backend pushes it down; master env is fallback.
    env["TAVILY_API_KEY"] = ex.get("tavily_api_key", settings.tavily_api_key) or ""
    if settings.vectordb_backend.strip().lower() == "pinecone":
        env["PINECONE_API_KEY"] = settings.pinecone_api_key or ""
        env["PINECONE_INDEX_NAME"] = settings.pinecone_index_name or ""
        env["PINECONE_ENVIRONMENT"] = settings.pinecone_environment or ""
        env["PINECONE_CLOUD"] = settings.pinecone_cloud or "aws"

    # Bring-your-own vector DB override (per context).
    if vector_db:
        backend = (vector_db.get("backend") or "").strip().lower()
        if backend:
            env["VECTORDB_BACKEND"] = backend
        if vector_db.get("collection"):
            env["QDRANT_COLLECTION"] = vector_db["collection"]
        if backend == "pinecone":
            env["PINECONE_API_KEY"] = vector_db.get("api_key", "")
            env["PINECONE_INDEX_NAME"] = vector_db.get("index", "")
            env["PINECONE_ENVIRONMENT"] = vector_db.get("environment", "")
            env["PINECONE_CLOUD"] = vector_db.get("cloud", "aws")
        else:  # qdrant-compatible (cloud or self-hosted)
            if vector_db.get("url"):
                env["QDRANT_URL"] = vector_db["url"]
            if vector_db.get("api_key"):
                env["QDRANT_API_KEY"] = vector_db["api_key"]
    return env


def _ensure_image(client, force: bool = False) -> None:
    """Pull the user image if it isn't already on the host.

    Auto-pull means the host never needs a manual clone/build — the image
    is published to GHCR by belleq-user CI and fetched on first use. Pass
    ``force=True`` (a context rebuild) to always re-pull so the container is
    recreated from the newest published image.
    """
    from docker.errors import APIError, ImageNotFound

    image = settings.user_container_image
    if not force and not settings.user_container_always_pull:
        try:
            client.images.get(image)
            return
        except ImageNotFound:
            pass
    logger.info("pulling_user_image image=%s", image)
    try:
        client.images.pull(image)
    except ImageNotFound as e:
        raise ProvisionError(
            f"Image '{image}' not found in the registry. Has belleq-user CI "
            f"published it, and is the package public (or the host logged into "
            f"the registry)?",
            status_code=422,
        ) from e
    except APIError as e:
        raise ProvisionError(
            f"Could not pull '{image}': {e}. If the package is private, run "
            f"`docker login ghcr.io` on the host.",
            status_code=502,
        ) from e


def _run_container(
    container_name: str,
    env: dict[str, str],
    labels: dict[str, str],
    caps: dict | None = None,
    force_pull: bool = False,
) -> str:
    """Blocking docker run with resource caps + labels. Returns the docker id.

    Caps: ram_mb -> --memory; cpu_vcpu -> --cpus (nano_cpus). A disk hard-cap is
    intentionally not applied here (it needs xfs pquota on the host); KB storage
    is metered at the app level instead.
    """
    from docker.errors import APIError, ImageNotFound

    client = _docker_client()
    _ensure_image(client, force=force_pull)

    # Remove any stale container with the same name (e.g. failed prior attempt,
    # or the previous image on a rebuild). The named -data volume is left intact
    # so the context's knowledge base survives a rebuild.
    try:
        stale = client.containers.get(container_name)
        logger.info("removing_stale_container name=%s", container_name)
        stale.remove(force=True)
    except Exception:  # noqa: BLE001 - not found is the common, fine case
        pass

    run_kwargs: dict = dict(
        image=settings.user_container_image,
        name=container_name,
        detach=True,
        network=settings.belleq_network,
        environment=env,
        volumes={f"{container_name}-data": {"bind": "/app/data", "mode": "rw"}},
        restart_policy={"Name": "on-failure"},
        labels=labels,
    )
    if caps:
        if caps.get("ram_mb"):
            run_kwargs["mem_limit"] = f"{int(caps['ram_mb'])}m"
        if caps.get("cpu_vcpu"):
            run_kwargs["nano_cpus"] = int(float(caps["cpu_vcpu"]) * 1_000_000_000)

    try:
        container = client.containers.run(**run_kwargs)
    except ImageNotFound as e:
        raise ProvisionError(
            f"Image '{settings.user_container_image}' not found on the host. "
            f"Build it first: docker build -t {settings.user_container_image} "
            f"path/to/belleq-user",
            status_code=422,
        ) from e
    except APIError as e:
        raise ProvisionError(f"Docker API error: {e}", status_code=502) from e

    logger.info("user_container_started name=%s id=%s", container_name, container.id[:12])
    return container.id


async def _wait_healthy(base_url: str, timeout: float) -> bool:
    """Poll the new container's /health until ok or timeout."""
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=3.0) as client:
        while time.monotonic() < deadline:
            try:
                r = await client.get(f"{base_url}/health")
                if r.status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(2.0)
    return False


async def provision_user_container(
    *,
    container_name: str,
    api_key: str,
    user_id: str,
    qdrant_collection: str | None = None,
    vector_db: dict | None = None,
    caps: dict | None = None,
    labels: dict | None = None,
    extraction: dict | None = None,
    force_pull: bool = False,
) -> dict:
    """Launch a user container on belleq-net and wait for it to be healthy.

    Returns dict with docker_id, base_url, port, healthy. Raises ProvisionError
    on docker-level failures. ``force_pull`` re-pulls the image first (rebuild).
    """
    env = _container_env(
        container_name=container_name,
        api_key=api_key,
        user_id=user_id,
        qdrant_collection=qdrant_collection,
        vector_db=vector_db,
        extraction=extraction,
    )
    # Default labels guarantee role + managed-by even if the caller passes none.
    run_labels = {"belleq.role": "context", "belleq.managed-by": "belleq-platform"}
    run_labels.update(labels or {})
    docker_id = await asyncio.to_thread(
        _run_container, container_name, env, run_labels, caps, force_pull
    )
    base_url = f"http://{container_name}:{settings.user_container_port}"
    healthy = await _wait_healthy(base_url, settings.user_container_health_timeout)
    if not healthy:
        logger.warning(
            "user_container_unhealthy_after_timeout name=%s base_url=%s",
            container_name,
            base_url,
        )
    return {
        "docker_id": docker_id,
        "base_url": base_url,
        "port": settings.user_container_port,
        "healthy": healthy,
    }


def _remove_container(container_name: str) -> None:
    """Blocking docker stop+remove (and its data volume)."""
    client = _docker_client()
    try:
        c = client.containers.get(container_name)
        c.remove(force=True)
        logger.info("user_container_removed name=%s", container_name)
    except Exception:  # noqa: BLE001 - already gone is fine
        logger.info("user_container_not_present_on_delete name=%s", container_name)
    try:
        client.volumes.get(f"{container_name}-data").remove(force=True)
    except Exception:  # noqa: BLE001
        pass


async def delete_user_container(container_name: str) -> None:
    await asyncio.to_thread(_remove_container, container_name)
