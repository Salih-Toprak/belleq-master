"""Belleq Master API — FastAPI application entrypoint."""

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

from app.api import (
    aggregate_routes,
    containers_routes,
    embeddings_routes,
    ingestion_routes,
    mcp_routes,
    registry_routes,
    sources_routes,
    vectordb_routes,
)
from app.mcp_connectors.registry import MCPConnectorRegistry
from app.clients.container_client import ContainerClient
from app.config import settings
from app.database import MasterDB
from app.embeddings.base import EmbeddingError
from app.embeddings.factory import get_embedding_adapter
from app.ingestion.pipeline import IngestionPipeline
from app.ingestion.scheduler import IngestionScheduler
from app.registry.registry import ContainerRegistry
from app.sources.registry import DataSourceRegistry
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
    logger.info("starting_belleq_master")

    db = MasterDB(
        url=settings.master_db_url,
        credential_encryption_key=settings.credential_encryption_key,
    )
    app.state.db = db
    logger.info("master_db_initialized url=%s", settings.master_db_url)

    sched_min = db.get_scheduler_interval()
    if sched_min is None:
        db.set_scheduler_interval(settings.ingestion_interval_minutes)
        sched_min = settings.ingestion_interval_minutes

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

    try:
        embedder = get_embedding_adapter(settings)
        embed_health = await embedder.health()
        app.state.embedder = embedder
        if embed_health.get("status") == "ok":
            logger.info(
                "embedding_adapter_ready backend=%s model=%s vector_size=%s",
                settings.embedding_backend,
                embed_health.get("model", ""),
                embed_health.get("vector_size", ""),
            )
        else:
            logger.warning("embedding_adapter_unhealthy_at_startup health=%s", embed_health)
    except Exception as e:  # noqa: BLE001
        logger.warning("embedding_adapter_init_failed error=%s", e)
        app.state.embedder = None

    source_registry = DataSourceRegistry(db)
    app.state.source_registry = source_registry
    srcs = source_registry.list_all(enabled_only=True)
    logger.info("data_source_registry_loaded enabled_sources=%s", len(srcs))

    mcp_connector_registry = MCPConnectorRegistry(db)
    app.state.mcp_connector_registry = mcp_connector_registry
    conns = mcp_connector_registry.list_all(enabled_only=False)
    logger.info("mcp_connector_registry_loaded connectors=%s", len(conns))

    pipeline = IngestionPipeline(
        vectordb=app.state.vectordb,
        embedder=app.state.embedder,
        source_registry=source_registry,
        collection_name=settings.ingestion_collection,
    )
    app.state.pipeline = pipeline

    scheduler = IngestionScheduler(
        pipeline=pipeline,
        interval_minutes=int(sched_min or settings.ingestion_interval_minutes),
    )
    scheduler.start()
    app.state.ingestion_scheduler = scheduler

    logger.info("belleq_master_ready host=%s port=%s", settings.app_host, settings.app_port)
    yield

    scheduler.stop()
    emb = getattr(app.state, "embedder", None)
    if emb is not None and hasattr(emb, "aclose"):
        await emb.aclose()
    await client.close()
    logger.info("belleq_master_shutdown")


app = FastAPI(
    title="Belleq Master API",
    description="""
Master orchestration layer for the Belleq knowledge infrastructure platform.

Responsibilities:
- Manage the master vector database (collection CRUD, document operations)
- Maintain the container registry (track all user/chatbot containers)
- Aggregate health and document stats across all registered containers
- Proxy document management actions (flag, unflag, delete) to containers
- Configure embedding backends and external data sources (Slack, Notion)
- Run scheduled ingestion into the master vector collection

Authentication: all `/master/*` endpoints require the `X-Admin-Key` header
unless `ADMIN_API_KEY` is empty (development mode).
""",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(vectordb_routes.router)
app.include_router(registry_routes.router)
app.include_router(aggregate_routes.router)
app.include_router(embeddings_routes.router)
app.include_router(sources_routes.router)
app.include_router(ingestion_routes.router)
app.include_router(mcp_routes.router)
app.include_router(containers_routes.router)


@app.get("/health", tags=["Health"])
async def health(request: Request) -> dict:
    """Basic liveness check. No auth required."""
    registry: ContainerRegistry = request.app.state.registry
    db = request.app.state.db
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
    emb = getattr(request.app.state, "embedder", None)
    if emb is None:
        estatus = "unavailable"
        ebackend = settings.embedding_backend
    else:
        ebackend = settings.embedding_backend
        eh = await emb.health()
        estatus = "ok" if eh.get("status") == "ok" else "error"

    sreg = request.app.state.source_registry
    all_sources = sreg.list_all(enabled_only=False)
    en_sources = sreg.list_all(enabled_only=True)
    sched = getattr(request.app.state, "ingestion_scheduler", None)
    dbi = db.get_scheduler_interval()
    interval = dbi if dbi is not None else settings.ingestion_interval_minutes

    return {
        "status": "ok",
        "vectordb_backend": backend,
        "vectordb_status": vstatus,
        "registered_containers": len(all_rows),
        "enabled_containers": len(enabled_rows),
        "master_db": settings.master_db_url,
        "embedding_backend": ebackend,
        "embedding_status": estatus,
        "configured_sources": len(all_sources),
        "enabled_sources": len(en_sources),
        "ingestion_scheduler_running": bool(sched and sched.running),
        "ingestion_interval_minutes": int(interval),
    }


@app.exception_handler(EmbeddingError)
async def embedding_error_handler(_request: Request, exc: EmbeddingError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc)})


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
