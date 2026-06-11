"""Application settings loaded from environment variables."""

import logging

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Belleq Master Container configuration (env-driven, no hardcoded secrets)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Vector DB backend selector
    vectordb_backend: str = Field(
        default="qdrant",
        description="Supported: qdrant, pinecone",
    )

    # Qdrant (local or cloud)
    qdrant_url: str = Field(default="http://qdrant:6333")
    qdrant_api_key: str = Field(default="")
    qdrant_collection: str = Field(default="company_knowledge")
    qdrant_vector_size: int = Field(default=768)

    # Pinecone
    pinecone_api_key: str = Field(default="")
    pinecone_environment: str = Field(
        default="",
        description="Pinecone serverless region (e.g. us-east-1) when using v3 serverless index creation",
    )
    pinecone_index_name: str = Field(default="")
    pinecone_cloud: str = Field(
        default="aws",
        description="ServerlessSpec cloud for create_index when backend is pinecone",
    )

    # Master DB
    master_db_url: str = Field(default="sqlite:///./data/master.db")

    # Master API
    admin_api_key: str = Field(default="")
    app_host: str = Field(default="0.0.0.0")
    app_port: int = Field(default=9000)

    # Container HTTP client
    container_call_timeout: float = Field(default=10.0)
    container_health_timeout: float = Field(default=3.0)

    # Embedding
    embedding_backend: str = Field(default="ollama")
    ollama_base_url: str = Field(default="http://ollama:11434")
    ollama_embed_model: str = Field(default="nomic-embed-text")
    openai_api_key: str = Field(default="")
    openai_embed_model: str = Field(default="text-embedding-3-small")
    embedding_vector_size: int = Field(default=768)

    # Ingestion
    ingestion_interval_minutes: int = Field(default=60)
    ingestion_collection: str = Field(default="company_knowledge")

    # Security (optional Fernet key for credentials at rest)
    credential_encryption_key: str = Field(default="")

    # User container provisioning (Docker-out-of-Docker via mounted socket)
    user_container_image: str = Field(
        default="ghcr.io/salih-toprak/belleq-user:latest",
        description=(
            "Image used when provisioning a user container. Pulled on demand if "
            "not present on the host (published by belleq-user CI to GHCR)."
        ),
    )
    user_container_always_pull: bool = Field(
        default=False,
        description="Always pull the image before run (gets the newest :latest).",
    )
    belleq_network: str = Field(
        default="belleq-net",
        description="Docker network user containers join (same as master/qdrant).",
    )
    user_container_port: int = Field(
        default=8000,
        description="Port the user container listens on inside the network.",
    )
    kb_autowire_enabled: bool = Field(
        default=True,
        description=(
            "Auto-mount each context's own belleq-user MCP (the knowledge base + "
            "conversation capture tools) into its aggregated /mcp/{container} endpoint, "
            "so the public per-context bridge exposes them alongside the user's connectors."
        ),
    )
    user_container_health_timeout: float = Field(
        default=15.0,
        description=(
            "Seconds to wait inline for a freshly provisioned container's /health. "
            "Kept under the platform proxy's 30s budget — if the container is not "
            "ready in time, provision returns status=starting and the registry "
            "health check picks it up shortly after."
        ),
    )


settings = Settings()
logger.debug(
    "settings_loaded vectordb_backend=%s app_port=%s",
    settings.vectordb_backend,
    settings.app_port,
)
