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

    # Conversation fact-extraction (propagated to every provisioned user container).
    # Set these once on the master; user containers inherit them automatically.
    conversation_extraction_enabled: bool = Field(
        default=False,
        description="Enable LLM fact-extraction from conversations in user containers.",
    )
    extraction_backend: str = Field(
        default="gemini",
        description='Extraction LLM provider for user containers: "gemini" or "anthropic".',
    )
    gemini_api_key: str = Field(
        default="",
        description="Gemini (google-genai) API key. NOTE: the free AI-Studio tier may "
        "train on submitted data — link billing before routing customer data.",
    )
    gemini_model: str = Field(default="gemini-2.5-flash")
    extraction_anthropic_api_key: str = Field(
        default="",
        description="Anthropic API key for Claude-Haiku extraction (if extraction_backend=anthropic).",
    )
    extraction_model: str = Field(default="claude-haiku-4-5")

    # Agent web access (Tavily). Fallback for the key the backend pushes per
    # provision; lets a standalone master grant agents web tools too.
    tavily_api_key: str = Field(default="")

    # Security (optional Fernet key for credentials at rest)
    credential_encryption_key: str = Field(default="")

    # Connector durability: mirror connector changes to the backend (the only
    # static server) so they survive this instance being terminated. When
    # backend_sync_url is empty the mirror is disabled (standalone/dev).
    backend_sync_url: str = Field(
        default="",
        description="Base URL of belleq-backend, e.g. https://api.belleq.app. "
        "Connector mutations are mirrored to {backend_sync_url}/internal/connectors.",
    )
    backend_internal_token: str = Field(
        default="",
        description="Shared secret sent as X-Internal-Token when mirroring "
        "connectors to the backend. Must match the backend's BACKEND_INTERNAL_TOKEN.",
    )
    # This master's own network-reachable base URL, used to tell a user container
    # where to find its aggregated MCP endpoint ({self_base_url}/mcp/{container})
    # when running an agent's connector tools. Defaults to the stable compose
    # address on belleq-net (container_name `belleq-master`, port 9000) so agent
    # connector tools work on any master with no env var — override via
    # SELF_BASE_URL only for non-standard topologies.
    self_base_url: str = Field(
        default="http://belleq-master:9000",
        description="This master's base URL reachable from user containers, "
        "e.g. http://belleq-master:9000. Used for agent connector-tool access.",
    )

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
    kb_instructions_enabled: bool = Field(
        default=True,
        description=(
            "Attach behavioral MCP server `instructions` to each per-context endpoint "
            "so a connected AI client recalls Belleq context first and saves exchanges "
            "automatically, with no user instructions. Returned to the client at MCP "
            "initialize. Only applied to real per-context endpoints (not workspace ones)."
        ),
    )
    mcp_capture_enabled: bool = Field(
        default=True,
        description=(
            "MCP response capture (4C): observe connector tool results flowing "
            "through each per-context proxy and ingest document-like ones into that "
            "context's knowledge base. Best-effort; never affects the tool call."
        ),
    )
    mcp_capture_min_chars: int = Field(
        default=800,
        description="Minimum result text length to treat a tool response as document-like.",
    )
    mcp_capture_exclude: str = Field(
        default="",
        description="Comma-separated connector namespaces to never capture (per-source opt-out).",
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
