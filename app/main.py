"""Mnemo Master API — FastAPI application entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api import aggregate_routes, registry_routes, vectordb_routes
from app.clients.container_client import ContainerClient
from app.config import settings
from app.database import MasterDB
from app.registry.registry import ContainerRegistry
from app.vectordb.base import VectorDBError
from app.vectordb.factory import get_vector_db_adapter

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown: DB, vector adapter (best-effort), registry, HTTP client."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger.info("starting_mnemo_master")

    db = MasterDB(url=settings.master_db_url)
    app.state.db = db
    logger.info("master_db_initialized url=%s", settings.master_db_url)

    try:
        adapter = get_vector_db_adapter(settings)
        health = await adapter.health()
        app.state.vectordb = adapter
        if health.get("status") == "ok":
            logger.info(
                "vectordb_connected backend=%s detail=%s",
                settings.vectordb_backend,
                health.get("detail", ""),
            )
        else:
            logger.warning("vectordb_unhealthy_at_startup health=%s", health)
    except ValueError as e:
        logger.warning("vectordb_factory_value_error error=%s", e)
        app.state.vectordb = None
    except Exception as e:  # noqa: BLE001
        logger.warning("vectordb_adapter_init_failed error=%s", e)
        app.state.vectordb = None

    registry = ContainerRegistry(db)
    app.state.registry = registry
    enabled = registry.list_enabled()
    logger.info("registry_loaded enabled_containers=%s", len(enabled))

    client = ContainerClient(
        timeout=settings.container_call_timeout,
        health_timeout=settings.container_health_timeout,
    )
    app.state.client = client

    logger.info("mnemo_master_ready host=%s port=%s", settings.app_host, settings.app_port)
    yield

    await client.close()
    logger.info("mnemo_master_shutdown")


app = FastAPI(
    title="Mnemo Master API",
    description="""
Master orchestration layer for the Mnemo knowledge infrastructure platform.

Responsibilities:
- Manage the master vector database (collection CRUD, document operations)
- Maintain the container registry (track all user/chatbot containers)
- Aggregate health and document stats across all registered containers
- Proxy document management actions (flag, unflag, delete) to containers

Authentication: all `/master/*` endpoints require the `X-Admin-Key` header
unless `ADMIN_API_KEY` is empty (development mode).
""",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(vectordb_routes.router)
app.include_router(registry_routes.router)
app.include_router(aggregate_routes.router)


@app.get("/health", tags=["Health"])
async def health(request: Request) -> dict:
    """Basic liveness check. No auth required."""
    registry: ContainerRegistry = request.app.state.registry
    all_rows = registry.list_all()
    enabled_rows = registry.list_enabled()
    adapter = getattr(request.app.state, "vectordb", None)
    if adapter is None:
        vstatus = "unavailable"
        backend = settings.vectordb_backend
    else:
        backend = adapter.backend_name
        h = await adapter.health()
        vstatus = "ok" if h.get("status") == "ok" else "error"
    return {
        "status": "ok",
        "vectordb_backend": backend,
        "vectordb_status": vstatus,
        "registered_containers": len(all_rows),
        "enabled_containers": len(enabled_rows),
        "master_db": settings.master_db_url,
    }


@app.exception_handler(VectorDBError)
async def vectordb_error_handler(_request: Request, exc: VectorDBError) -> JSONResponse:
    msg = str(exc).lower()
    status = 404 if "not found" in msg else 503
    return JSONResponse(status_code=status, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, HTTPException):
        return await http_exception_handler(request, exc)
    if isinstance(exc, RequestValidationError):
        return await request_validation_exception_handler(request, exc)
    logger.error("unhandled_exception error=%s", exc, exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})
