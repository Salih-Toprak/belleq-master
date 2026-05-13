# mnemo-master

The **Mnemo Master Container** is the master orchestration layer for the Mnemo knowledge infrastructure platform. It is not an end-user application: it is one independently deployable building block that coordinates a central vector store, a container registry, and HTTP calls into many user-scoped containers.

## The building block principle

Each Mnemo component lives in its own repository and ships as its own container image. Components communicate over stable HTTP contracts and configuration (environment variables and the SQLite registry), so you can upgrade, scale, or replace one piece without rebuilding the whole platform. This master container never imports user container code and never reads user container databases; it only calls their HTTP APIs.

## Quick start

```bash
git clone https://github.com/sstprk/mnemo-master.git
cd mnemo-master
cp .env.example .env
docker compose up --build
```

Verify liveness:

```bash
curl -s http://localhost:9000/health | jq
```

Register your first user container (set `ADMIN_API_KEY` in `.env` first if you enabled admin auth):

```bash
curl -sS -X POST http://localhost:9000/master/registry/containers \
  -H 'Content-Type: application/json' \
  -H 'X-Admin-Key: YOUR_ADMIN_KEY' \
  -d '{
    "container_id": "user-001",
    "display_name": "User 001",
    "container_type": "user",
    "base_url": "http://user-001:8100",
    "api_key": "",
    "metadata": {}
  }' | jq
```

## Switching vector DB backends

| Mode | What to set |
|------|-------------|
| Local Qdrant (default) | `VECTORDB_BACKEND=qdrant`, `QDRANT_URL=http://qdrant:6333`, empty `QDRANT_API_KEY` |
| Qdrant Cloud | `QDRANT_URL` to your cluster URL, non-empty `QDRANT_API_KEY` |
| Pinecone | `VECTORDB_BACKEND=pinecone`, `PINECONE_API_KEY`, `PINECONE_INDEX_NAME`, and `PINECONE_ENVIRONMENT` as your serverless **region** (defaults to `us-east-1` when empty). Optional `PINECONE_CLOUD` (default `aws`). |
| Future backend | Implement `VectorDBAdapter`, register it in `app/vectordb/factory.py`, add settings + env vars. |

**Note:** `scroll`-based admin listing is not supported on Pinecone; use Qdrant for that workflow or export tooling.

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `VECTORDB_BACKEND` | `qdrant` | `qdrant` or `pinecone`. |
| `QDRANT_URL` | `http://qdrant:6333` | Qdrant REST URL. |
| `QDRANT_API_KEY` | _(empty)_ | Non-empty enables Qdrant Cloud auth. |
| `QDRANT_COLLECTION` | `company_knowledge` | Default master collection name for aggregate vector deletes. |
| `QDRANT_VECTOR_SIZE` | `768` | Default embedding size reference (collection creation uses explicit API body). |
| `PINECONE_API_KEY` | _(empty)_ | Required when `VECTORDB_BACKEND=pinecone`. |
| `PINECONE_ENVIRONMENT` | _(empty)_ | Serverless region (e.g. `us-east-1`) when empty defaults internally. |
| `PINECONE_INDEX_NAME` | _(empty)_ | Default Pinecone index; may be overridden per API `collection_name`. |
| `PINECONE_CLOUD` | `aws` | Serverless cloud for `create_index`. |
| `MASTER_DB_URL` | `sqlite:///./data/master.db` | Master SQLite URL (SQLAlchemy). |
| `ADMIN_API_KEY` | _(empty)_ | When set, all `/master/*` routes require matching `X-Admin-Key`. |
| `APP_HOST` | `0.0.0.0` | Bind host for local `uvicorn` runs (Docker CMD uses `0.0.0.0`). |
| `APP_PORT` | `9000` | HTTP port inside the container / `uvicorn` port. |
| `CONTAINER_CALL_TIMEOUT` | `10.0` | httpx timeout for `/stats`, `/docs`, etc. |
| `CONTAINER_HEALTH_TIMEOUT` | `3.0` | httpx timeout for `/health` probes. |
| `EMBEDDING_BACKEND` | `ollama` | `ollama` or `openai`. |
| `OLLAMA_BASE_URL` | `http://ollama:11434` | Ollama API base URL. |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Embedding model name. |
| `OPENAI_API_KEY` | _(empty)_ | Required when `EMBEDDING_BACKEND=openai`. |
| `OPENAI_EMBED_MODEL` | `text-embedding-3-small` | OpenAI embedding model id. |
| `EMBEDDING_VECTOR_SIZE` | `768` | Must match model output dimension. |
| `INGESTION_INTERVAL_MINUTES` | `60` | Scheduler interval (also stored in SQLite after first run). |
| `INGESTION_COLLECTION` | `company_knowledge` | Target Qdrant collection / Pinecone index for ingested chunks. |
| `CREDENTIAL_ENCRYPTION_KEY` | _(empty)_ | Optional Fernet key for encrypting source credentials at rest. |

## API overview

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Liveness + high-level subsystem status (no auth). |
| `/master/vectordb/health` | GET | Vector DB adapter health. |
| `/master/vectordb/collections` | GET | List collections/indexes. |
| `/master/vectordb/collections/{name}` | GET | Collection metadata. |
| `/master/vectordb/collections/{name}/count` | GET | Point count with optional `source` / `department` filters. |
| `/master/vectordb/collections/{name}/docs` | GET | Paginated payload scan (`scroll`). |
| `/master/vectordb/collections/{name}/docs/{doc_id}` | GET | All chunks for `doc_id`. |
| `/master/vectordb/collections/{name}/docs/{doc_id}` | DELETE | Delete vectors for `doc_id` only. |
| `/master/vectordb/collections` | POST | Create collection/index. |
| `/master/vectordb/collections/{name}` | DELETE | Drop collection/index. |
| `/master/registry/containers` | GET | List containers (`enabled_only` query). |
| `/master/registry/containers/{id}` | GET | Fetch one record. |
| `/master/registry/containers` | POST | Register a container. |
| `/master/registry/containers/{id}` | PATCH | Update fields. |
| `/master/registry/containers/{id}` | DELETE | Remove from registry. |
| `/master/registry/containers/{id}/enable` | POST | Enable. |
| `/master/registry/containers/{id}/disable` | POST | Disable. |
| `/master/registry/containers/{id}/health-check` | POST | Live `/health` probe + log row. |
| `/master/registry/containers/{id}/health-history` | GET | Recent health log entries. |
| `/master/aggregate/health` | GET | Parallel `/health` across enabled containers + logging. |
| `/master/aggregate/stats` | GET | Parallel `/stats` merge + dashboard totals. |
| `/master/aggregate/docs` | GET | Parallel `/docs` merge with `container_id` on each row. |
| `/master/aggregate/containers/{id}/docs/{doc_id}/flag` | POST | Proxy flag. |
| `/master/aggregate/containers/{id}/docs/{doc_id}/unflag` | POST | Proxy unflag. |
| `/master/aggregate/containers/{id}/docs/{doc_id}` | DELETE | User delete + master vector delete (independent steps). |
| `/master/embeddings/health` | GET | Embedding adapter health. |
| `/master/embeddings/config` | GET | Stored / env embedding config (secrets redacted). |
| `/master/embeddings/config` | PUT | Validate + persist embedding config (restart required). |
| `/master/embeddings/test` | POST | Sample embed; returns first 5 dimensions only. |
| `/master/sources` | GET | List data sources (`enabled_only`, `source_type`). |
| `/master/sources` | POST | Register Slack/Notion source (validates credentials first). |
| `/master/sources/{id}` | GET | One source (redacted). |
| `/master/sources/{id}` | PATCH | Update display/access/metadata. |
| `/master/sources/{id}` | DELETE | Remove source + sync logs. |
| `/master/sources/{id}/enable` | POST | Enable source. |
| `/master/sources/{id}/disable` | POST | Disable source. |
| `/master/sources/{id}/validate` | POST | Re-check credentials. |
| `/master/sources/{id}/sync-history` | GET | Recent sync log rows. |
| `/master/ingestion/status` | GET | Per-source sync fields. |
| `/master/ingestion/sync` | POST | Background `ingest_all` (`full_resync` query). |
| `/master/ingestion/sync/{source_id}` | POST | Background single-source ingest. |
| `/master/ingestion/scheduler` | GET | Scheduler status + next run. |
| `/master/ingestion/scheduler` | PUT | Update interval minutes. |

Interactive OpenAPI docs: `http://localhost:9000/docs`.

## Container registry contract

Register each user container with its reachable `base_url` (for example `http://rag-user-001:8100` on the shared Docker network).

Each user container **must** expose:

- `GET /health` — return `200` when healthy.
- `GET /stats` — JSON document counters (see aggregate totals keys for recommended fields).
- `GET /docs` — JSON list under `documents`, `items`, or `docs`, or a bare JSON array.

Optional document routes used by aggregate proxies:

- `GET /docs/{doc_id}`
- `POST /docs/{doc_id}/flag` with JSON `{"reason": "..."}`
- `POST /docs/{doc_id}/unflag`
- `DELETE /docs/{doc_id}`

If `api_key` is set on the registry row, it is sent as `X-Container-Key`.

## Network setup

`docker-compose.yml` defines a **named bridge** network `mnemo-net`. Attach other stacks to the same network name so the master can call user containers by DNS name:

```yaml
networks:
  default:
    name: mnemo-net
    external: true
```

Alternatively, run all services from a single Compose project that shares `mnemo-net`.

## Production Compose + HTTPS

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

- Qdrant is **not** published on host ports.
- Caddy listens on **443** and proxies to `master:9000` using `Caddyfile` (self-signed `tls internal` by default).
- Log rotation: `json-file`, `max-size=10m`, `max-file=3`.

Replace `tls internal` with your domain block for real certificates (Let’s Encrypt).

## Adding a new vector DB backend

1. Implement `VectorDBAdapter` in `app/vectordb/your_adapter.py`, translating all operations and mapping errors to `VectorDBError`.
2. Add filter helpers if needed (or reuse normalized filters in `filters.py`).
3. Extend `get_vector_db_adapter()` in `app/vectordb/factory.py`.
4. Add settings + `.env.example` entries and document them in this README.

## Data Sources & Ingestion

Ingestion pulls content from registered **Slack** or **Notion** sources, splits it into overlapping text chunks, attaches **access-control metadata** (`ac_*` fields) to every vector payload, embeds chunks via the configured **EmbeddingAdapter**, and upserts points into the master vector collection (`INGESTION_COLLECTION`, default `company_knowledge`). A background **APScheduler** job runs `ingest_all` on `INGESTION_INTERVAL_MINUTES` (also persisted in SQLite and adjustable via `PUT /master/ingestion/scheduler`).

### Supported embedding backends

| Backend | Example model | Vector size | Notes |
|---------|----------------|------------|--------|
| ollama | nomic-embed-text | 768 | Local, no API cost |
| openai | text-embedding-3-small | 1536 | Cloud |
| openai | text-embedding-3-large | 3072 | Cloud, highest quality |

### Register Slack + trigger sync

```bash
curl -sS -X POST "http://localhost:9000/master/sources" \
  -H "Content-Type: application/json" \
  -H "X-Admin-Key: YOUR_KEY" \
  -d '{
    "source_id": "slack-main",
    "source_type": "slack",
    "display_name": "Main Slack",
    "credentials": { "bot_token": "xoxb-..." },
    "access_control": {
      "allowed_channels": ["general"],
      "excluded_channels": [],
      "include_threads": true,
      "max_history_days": 90,
      "departments": ["engineering"]
    },
    "metadata": {}
  }'

curl -sS -X POST "http://localhost:9000/master/ingestion/sync/slack-main?full_resync=false" \
  -H "X-Admin-Key: YOUR_KEY"
```

### Register Notion + trigger sync

```bash
curl -sS -X POST "http://localhost:9000/master/sources" \
  -H "Content-Type: application/json" \
  -H "X-Admin-Key: YOUR_KEY" \
  -d '{
    "source_id": "notion-hr",
    "source_type": "notion",
    "display_name": "HR Handbook",
    "credentials": { "integration_token": "secret_..." },
    "access_control": {
      "allowed_page_ids": ["PAGE_UUID"],
      "allowed_database_ids": [],
      "excluded_page_ids": [],
      "max_depth": 2,
      "departments": ["hr"]
    },
    "metadata": {}
  }'

curl -sS -X POST "http://localhost:9000/master/ingestion/sync/notion-hr" \
  -H "X-Admin-Key: YOUR_KEY"
```

### Access-control metadata on vectors

Each chunk payload includes: `ac_source_id` (matches `DataSourceRecord.source_id`), `ac_channels` (Slack channel names), `ac_page_ids` (Notion page ids for the chunk), and `ac_departments` (department tags). User containers can filter retrieval using these fields once per-user ACL is implemented in the dashboard.

### Scheduler API

- `GET /master/ingestion/scheduler` — `running`, `interval_minutes`, `next_run` (ISO or null).
- `PUT /master/ingestion/scheduler` with `{"interval_minutes": 30}` — updates SQLite and reschedules the job immediately.

### Credential storage

Credentials are stored in SQLite (`credentials_json`). With `CREDENTIAL_ENCRYPTION_KEY` empty, values are **plaintext JSON (development only)**. Set a Fernet key to encrypt at rest; all HTTP responses **redact** secrets (`***set***`).

## Local development without Docker

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export QDRANT_URL=http://localhost:6333
uvicorn app.main:app --reload --host 0.0.0.0 --port 9000
```

## License

Proprietary / TBD — set the license for the `sstprk/mnemo-master` repository as appropriate.
