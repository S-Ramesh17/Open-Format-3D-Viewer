## Project Overview

OpenFormat is a backend platform for uploading, converting, and collaboratively viewing 3D BIM/CAD models in the browser. A user uploads a model in one of several native CAD/BIM formats; a worker pipeline converts it into web-renderable XKT chunks; and a WebSocket layer provides real-time multi-user collaboration (cursors, annotations, presence) on top of the converted model.

**Supported model formats** (verified against `apps/worker/app/tasks/`): IFC, STEP (`.step`/`.stp`), OBJ, STL, GLTF/GLB.

**High-level architecture:**
```
Browser
  │
  ├── REST (HTTPS) ──────────────► API (FastAPI, :8000)
  │                                     │
  └── WebSocket ───────► ws-server (Fastify, :8001)      │
                              │                            │
                              ├── Redis (pub/sub + queue) ◄┘
                              │        │
                              │        ▼
                              │   Celery Worker(s) ──► ClamAV (malware scan)
                              │        │
                              │        ▼
                              │   Storage (local disk in dev / S3 in prod)
                              │        │
                              └────────┴──► PostgreSQL (via API & Worker)
```

## Features

Verified as implemented by reading the actual routers/services/worker tasks (not assumed):

| Feature | Where implemented |
|---|---|
| Authentication (register/login/refresh/logout, cookie + Bearer) | `apps/api/app/routers/auth.py`, `app/services/auth.py` |
| Google OAuth2 login | `apps/api/app/routers/auth.py` (`/auth/google`, `/auth/google/callback`), `app/services/oauth.py` |
| API Keys | `apps/api/app/routers/auth.py` (`/auth/keys`), `app/services/api_key.py` |
| Projects & role-based membership (viewer/editor/admin) | `apps/api/app/routers/projects.py`, `app/core/authorization.py` |
| Model upload (presigned S3 URL or local direct-upload) | `apps/api/app/routers/models.py`, `app/services/storage.py` |
| Malware scanning (ClamAV) before processing | `apps/worker/app/tasks/scan.py` |
| Conversion pipeline (IFC/STEP/OBJ/STL/GLTF → XKT) | `apps/worker/app/tasks/{ifc,step,obj,stl,gltf}.py` |
| Per-plan upload size limits (Free/Pro/Enterprise) | `apps/worker/app/tasks/common.py::get_plan_max_bytes` |
| WebSocket real-time collaboration (cursors, presence, annotations) | `apps/ws-server/src/server.js` |
| Annotations + threaded comments | `apps/api/app/routers/annotations.py` |
| BCF export | `apps/api/app/routers/webhooks.py` (`/models/{id}/export/bcf`), `app/services/bcf_export.py` |
| Webhooks (outbound, with delivery log) | `apps/api/app/routers/webhooks.py`, `apps/worker/app/tasks/webhook.py` |
| Public share links | `apps/api/app/routers/share.py` |
| Prometheus metrics (worker + ws-server) | `apps/worker/app/tasks/metrics_server.py`, `apps/ws-server/src/metrics.js` |

## Architecture

```
Frontend (browser)
      │
      ▼
   API (FastAPI, Python 3.12) ──────────► PostgreSQL 15
      │        │
      │        └──────────────► Redis (pub/sub for model events, Celery broker)
      │                                │
      ▼                                ▼
  Storage (local disk / S3)      Celery Workers (Python 3.12)
                                        │
                                        ├──► ClamAV (scan queue)
                                        └──► IFC/STEP/OBJ/STL/GLTF conversion queues
                                                    │
                                                    ▼
                                            XKT chunks → Storage
                                                    │
                                                    ▼
                                    Redis pub/sub (model_events:{user_id})
                                                    │
                                                    ▼
                                        ws-server (Node.js/Fastify, :8001)
                                                    │
                                                    ▼
                                              Browser (WebSocket)
```

`apps/beat` (a second worker container running `celery beat`) drives scheduled tasks — currently queue-depth metrics collection and abandoned-upload cleanup (verified in `apps/worker/app/celery_app.py`'s `beat_schedule`).

## Repository Structure

```
apps/
  api/            FastAPI backend — REST API, auth, DB models, Alembic migrations
    app/
      routers/    One file per resource (auth, projects, models, annotations, share, webhooks, internal)
      services/   Business logic called by routers
      models/     SQLAlchemy ORM models
      schemas/    Pydantic request/response schemas
      core/       Cross-cutting: security, authorization, cookies, exceptions, logging
      middleware/ Auth, rate-limit, CSRF, security-headers middleware
    migrations/   Alembic migration history
    tests/        Pytest suite (unit + integration)
  worker/         Celery worker — format converters, malware scan, webhook delivery
    app/tasks/    One file per Celery task/converter
    tests/        Pytest suite
  ws-server/      Fastify WebSocket server — real-time collaboration relay
    src/          server.js (connection lifecycle, rooms, Redis pub/sub), metrics.js
    tests/        Node built-in test runner suite
docs/             This documentation
docker-compose.yml  Local dev stack: postgres, redis, api, clamav, worker, beat, ws-server
```

## Requirements

- Docker & Docker Compose
- Python `>=3.12,<3.13` (only needed for running services outside Docker)
- Node.js `>=20.0.0` (only needed for running ws-server outside Docker)

## Local Setup

Only commands verified against the actual repository structure:

```bash
git clone <repo-url>
cd Open-Format-3D-Viewer-main

# Each service has its own .env — copy each example
cp apps/api/.env.example apps/api/.env
cp apps/worker/.env.example apps/worker/.env
cp apps/ws-server/.env.example apps/ws-server/.env

# Generate real secrets before starting — JWT_SECRET/INTERNAL_SERVICE_KEY
# placeholders are rejected in production and should not be used anywhere:
python3 -c "import secrets; print(secrets.token_hex(32))"
# Set the SAME value for JWT_SECRET in apps/api/.env and apps/ws-server/.env

docker compose up --build -d
docker compose exec api alembic upgrade head
```

No `scripts/seed.py` was found in the repository at audit time — there is currently no scripted way to create a test user/project; use `POST /v1/auth/register` directly.

## Environment Variables

**`apps/api/.env`**

| Variable | Required | Description | Default |
|---|---|---|---|
| `DATABASE_URL` | Yes | Async Postgres connection string (`postgresql+asyncpg://...`) | `postgresql+asyncpg://user:password@localhost:5432/openformat` |
| `JWT_SECRET` | Yes | Signs access/refresh JWTs. Must be ≥32 chars, non-placeholder; production additionally requires sufficient entropy. Must match `apps/ws-server`'s `JWT_SECRET`. | `CHANGE_ME_TO_A_RANDOM_64_CHAR_HEX_STRING` (placeholder — rejected) |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | No | Access token lifetime | `60` |
| `REFRESH_TOKEN_EXPIRE_DAYS` | No | Refresh token lifetime | `30` |
| `REDIS_URL` | Yes | Redis connection string | `redis://localhost:6379/0` |
| `AWS_REGION` | Only if `STORAGE_PROVIDER=s3` | S3 region | `us-east-1` |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | No | Leave empty to use the AWS default credential chain (IAM role) instead of static keys | *(empty)* |
| `S3_RAW_BUCKET` / `S3_PROCESSED_BUCKET` | Only if `STORAGE_PROVIDER=s3` | S3 bucket names | `openformat-raw` / `openformat-processed` |
| `CDN_BASE_URL` | Only if `STORAGE_PROVIDER=s3` | Public base URL for processed chunks | `https://cdn.example.com` |
| `STORAGE_PROVIDER` | Yes | `local` or `s3` | `local` |
| `LOCAL_STORAGE_PATH` | Only if `STORAGE_PROVIDER=local` | Filesystem root for local storage mode | `/data/openformat` |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Only if Google OAuth is used | OAuth2 app credentials | *(empty)* |
| `GOOGLE_REDIRECT_URI` | Only if Google OAuth is used | OAuth2 callback URL | `http://localhost:8000/v1/auth/google/callback` |
| `ALLOWED_ORIGINS` | Yes | Comma-separated CORS allowlist. Must not be `*` in production (credentialed cookies are used). | `http://localhost:3000` |
| `FRONTEND_URL` | Yes | Used for OAuth redirect targets | `http://localhost:3000` |
| `RATE_LIMIT_FREE_PER_HOUR` / `RATE_LIMIT_PRO_PER_HOUR` | No | Per-plan API rate limits | `100` / `10000` |
| `MAX_UPLOAD_SIZE_BYTES` | No | Global upload ceiling (in addition to per-plan limits enforced by the worker) | `524288000` (500 MB) |
| `ENVIRONMENT` | Yes | `development` / `staging` / `production` — gates secure-cookie flags, DB SSL, and secret-strength validation | `development` |
| `SENTRY_DSN` | No | Error tracking | *(empty — disabled)* |
| `INTERNAL_SERVICE_KEY` | Yes | Shared secret for `apps/ws-server` → API service-to-service calls (`/internal/*`). Must match `apps/ws-server`'s `WS_INTERNAL_SERVICE_KEY`. | `CHANGE_ME_TO_A_RANDOM_SECRET` (placeholder — rejected) |

**`apps/worker/.env`**

| Variable | Required | Description | Default |
|---|---|---|---|
| `DATABASE_URL` | Yes | Sync Postgres connection string (`postgresql+psycopg2://...`) | `postgresql+psycopg2://user:password@localhost:5432/openformat` |
| `REDIS_URL` | Yes | Celery broker + pub/sub | `redis://localhost:6379/0` |
| `XEOKIT_CONVERT_BIN` | No | Path/command for IFC→XKT conversion | `xeokit-convert` |
| `XKT_CHUNK_MAX_BYTES` | No | Max size per XKT output chunk | `16777216` (16 MB) |
| `GLTF_PIPELINE_BIN` / `GLTF_VALIDATOR_BIN` | No | GLTF processing tool paths | `gltf-pipeline` / `gltf-validator` |
| `CLAMD_HOST` / `CLAMD_PORT` / `CLAMD_TIMEOUT` | Yes | ClamAV daemon connection | `localhost` / `3310` / `60` |
| `METRICS_PORT` | No | Prometheus metrics HTTP port | `9090` |
| `MAX_UPLOAD_SIZE_BYTES` | No | Fallback size ceiling; per-plan limits (`get_plan_max_bytes`) take precedence | `524288000` |

**`apps/ws-server/.env`**

| Variable | Required | Description | Default |
|---|---|---|---|
| `PORT` | No | WebSocket server port | `8001` |
| `JWT_SECRET` | Yes | Must match `apps/api`'s `JWT_SECRET` exactly — no insecure fallback exists; the server refuses to start if unset. | `CHANGE_ME_TO_A_RANDOM_64_CHAR_HEX_STRING` (placeholder — rejected) |
| `REDIS_URL` | Yes | Room-broadcast pub/sub + model-event relay | `redis://localhost:6379/0` |
| `NODE_ENV` | No | `development` / `production` | `development` |
| `WS_METRICS_PORT` | No | Prometheus metrics HTTP port | `9091` |
| `WS_INTERNAL_API_URL` | Yes | Base URL of the API's internal service endpoints, used to authorize `JOIN_MODEL` | `http://localhost:8000` |
| `WS_INTERNAL_SERVICE_KEY` | Yes | Must match the API's `INTERNAL_SERVICE_KEY` | `CHANGE_ME_TO_A_RANDOM_SECRET` (placeholder — rejected) |

## Docker

| Service | Port(s) | Healthcheck |
|---|---|---|
| `postgres` | `5432` | `pg_isready` |
| `redis` | `6379` | `redis-cli ping` |
| `api` | `8000` | `curl -f http://localhost:8000/health` |
| `clamav` | `3310` | `clamdcheck.sh` |
| `worker` | `9090` (metrics) | `curl -f http://localhost:9090/metrics` |
| `beat` | *(none exposed)* | `pgrep -f 'celery.*beat'` |
| `ws-server` | `8001`, `9091` (metrics) | `GET /health` (via Node `http` client — no `curl` in the base image) |

Volumes: `postgres_data`, `redis_data`, `uploads_data` (shared between `api` and `worker` for local-storage mode), `clamav_data`.

## Workers

All five converters (`apps/worker/app/tasks/{ifc,step,obj,stl,gltf}.py`) share the same shape, verified directly:

- **Queues:** routed via `task_routes` in `app/celery_app.py` — each format has a dedicated Celery queue, plus separate `scan` and `webhook` queues.
- **Retries:** `max_retries=2`, `time_limit=1800`s (hard), `soft_time_limit=1500`s — the soft limit is caught internally and routed through `handle_task_failure()` before the hard limit would kill the process.
- **Idempotency:** each task checks for a terminal model status before starting (skips redelivered tasks) and takes a Redis lock (`acquire_task_lock`) to prevent duplicate concurrent execution.
- **Outputs:** XKT chunks uploaded to storage under `processed/{model_id}/...`, with `Model.s3_processed_prefix` recording the prefix.
- **Status flow:** `pending` → (confirm) → `processing` → `ready` or `failed`. Every failure path — including oversized files and format-specific errors — routes through the shared `handle_task_failure()` helper, which sets `status="failed"` with a stored `error_message` and publishes a `MODEL_FAILED`-equivalent event.

## WebSocket

Endpoint: `GET /connect?token=<jwt>` (verified in `apps/ws-server/src/server.js`).

| Event | Direction | Payload | Notes |
|---|---|---|---|
| `JOIN_MODEL` | Client → Server | `{event, model_id}` | Authorization-checked via `GET /internal/models/{id}/authorize` on the API before the room join completes |
| `LEAVE_MODEL` | Client → Server | `{event, model_id}` | |
| `CURSOR_MOVE` | Client → Server | `{event, model_id, position: [x,y,z], normal?: [x,y,z]}` | Validated (`isVector3`) and throttled server-side before relay |
| `CURSOR_MOVED` | Server → Client | `{event, user_id, position: [x,y,z]}` | `normal` intentionally not relayed |
| `USER_JOINED` | Server → Client | `{event, user: {id}}` | |
| `USER_LEFT` | Server → Client | `{event, user_id}` | |
| `PING` / `PONG` | Bidirectional | `{event}` | Server-initiated heartbeat + client-initiated ping supported |
| `ANNOTATION_CREATED` | Bidirectional | `{event, annotation: {...}}` | |
| `ANNOTATION_UPDATED` | Bidirectional | `{event, annotation_id, status, updated_by}` | |
| `ANNOTATION_DELETED` | Bidirectional | `{event, annotation_id}` | |
| `MODEL_SYNC` | Bidirectional | `{event, data: {...}}` | |
| `MODEL_PROCESSING` | Server → Client | `{event, data: {model_id, progress_pct, stage}}` | Relayed from the worker via `model_events:{user_id}` |
| `MODEL_READY` / `MODEL_FAILED` | Server → Client | Worker-originated | Relayed from `model_events:{user_id}` |
| `ERROR` | Server → Client | `{event, message}` | |

Room broadcasts are fanned out across multiple `ws-server` replicas via a Redis pub/sub channel (`ws:room:{model_id}`), not just in-process state — verified in `server.js`.

## REST API

41 endpoints across 7 routers, verified via direct route inspection. Full request/response schemas live in `apps/api/app/schemas/`; grouped summary:

**Authentication** (`/v1/auth`): `POST /register`, `POST /login`, `POST /refresh`, `POST /logout`, `GET /me`, `POST /keys`, `GET /keys`, `DELETE /keys/{id}`, `GET /google`, `GET /google/callback`

**Projects** (`/v1/projects`): `POST /`, `GET /`, `GET /{id}`, `GET /{id}/members`, `POST /{id}/members`, `PATCH /{id}/members/{user_id}`, `DELETE /{id}/members/{user_id}`, `PATCH /{id}`, `DELETE /{id}`

**Models** (`/v1/models`): `POST /upload`, `GET /`, `POST /upload/local`, `POST /{id}/confirm`, `GET /{id}`, `DELETE /{id}`, `GET /{id}/elements`, `GET /{id}/elements/{guid}`, `GET /{id}/tree`, `GET /{id}/chunks`

**Annotations** (`/v1`): `GET /models/{id}/annotations`, `POST /models/{id}/annotations`, `PATCH /annotations/{id}`, `POST /annotations/{id}/comments`, `GET /annotations/{id}/comments`

**Share** (`/v1/share`): `POST /`, `GET /{token}`, `DELETE /{link_id}`, `GET /model/{model_id}`

**Webhooks** (`/v1`): `POST /webhooks`, `GET /webhooks`, `PATCH /webhooks/{id}`, `DELETE /webhooks/{id}`, `GET /webhooks/{id}/deliveries`

**BCF**: `GET /v1/models/{id}/export/bcf`

**Internal** (`/internal`, service-to-service only, not for API consumers): `GET /models/{id}/authorize`

**Health**: `GET /health` (verified in `apps/api/app/main.py`)

## Running Tests

```bash
# API
cd apps/api && poetry run pytest tests/ -v

# Worker
cd apps/worker && poetry run pytest tests/ -v

# WebSocket server
cd apps/ws-server && npm test

# Full stack up, then run integration suites against it
docker compose up -d
docker compose exec api pytest tests/ -v
```

## Troubleshooting

| Symptom | Likely cause | Check |
|---|---|---|
| API fails to start, `ValidationError` on `SECRET_KEY`/`JWT_SECRET` | Placeholder or too-short secret | Generate a real secret (see Local Setup) |
| Cookie auth works over `http://localhost` but breaks in a deployed environment | `secure`/`samesite` cookie flags depend on `ENVIRONMENT` | Confirm `ENVIRONMENT=production` and the deployment is HTTPS |
| Worker never picks up jobs | Redis unreachable, or wrong queue name | `docker compose logs worker`, confirm `REDIS_URL` matches across `api`/`worker` |
| Model stuck in `processing` forever | Check worker logs for the specific task; all known failure paths route through `handle_task_failure()`, so a stuck state usually means an unhandled exception outside that path | `docker compose logs worker \| grep model_id` |
| `docker compose up` fails immediately | Missing `.env` files (gitignored, not created by default) | See Local Setup |
| WebSocket connects but never receives room events | Client and server on `JWT_SECRET` mismatch, or `JOIN_MODEL` authorization failing silently | Check for a `MODEL_ACCESS_DENIED`/`ERROR` event; confirm `WS_INTERNAL_SERVICE_KEY` matches `INTERNAL_SERVICE_KEY` |
| ClamAV healthcheck never turns healthy | ClamAV's virus database takes time to load on first start | Wait — `start_period: 90s` is already configured for this |
