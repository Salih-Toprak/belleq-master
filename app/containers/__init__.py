"""Docker-out-of-Docker provisioning of per-user belleq-user containers.

The master talks to the host Docker daemon over a mounted socket
(`/var/run/docker.sock`) to launch and tear down user containers on the
same `belleq-net` network it and qdrant share.
"""
