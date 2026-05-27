# sado-api

Backend REST API for the **SADO** AI speech therapy platform — built with
FastAPI, SQLAlchemy 2.0 (async), PostgreSQL, Redis, MinIO, and Celery.

This service powers:

* the parent / teacher **mobile app** (Expo),
* the speech-therapist / admin **dashboard** (React + Vite),
* and the asynchronous **audio-analysis pipeline** (Whisper STT + librosa
  feature extraction + XGBoost risk classifier — mocked locally).

---

## Quick start (development)

```bash
# 1. create a virtual environment
python3.12 -m venv .venv
source .venv/bin/activate

# 2. install the project + dev tools
pip install -e ".[dev]"

# 3. copy the env template and edit if needed
cp .env.example .env

# 4. run the API
uvicorn app.main:app --reload --port 8000
```

Then open:

* <http://localhost:8000/docs> — Swagger UI
* <http://localhost:8000/redoc> — ReDoc
* <http://localhost:8000/api/v1/health> — liveness probe

The default configuration uses **SQLite** (`./sado.db`) so the API
boots with no external dependencies. Postgres / Redis / MinIO are only
required for the production-style stack (see [Docker Compose](#docker-compose)
below).

---

## Docker Compose

A full stack — API, Celery worker, Postgres, Redis, MinIO — is
described in [`docker-compose.yml`](./docker-compose.yml).

```bash
# 1. seed environment variables (JWT_SECRET is mandatory)
cp .env.example .env
sed -i.bak 's|^DATABASE_URL=.*|DATABASE_URL=postgresql+asyncpg://sado:sado@db:5432/sado|' .env
sed -i.bak 's|^REDIS_URL=.*|REDIS_URL=redis://redis:6379/0|' .env
sed -i.bak 's|^MINIO_ENDPOINT=.*|MINIO_ENDPOINT=http://minio:9000|' .env
sed -i.bak 's|^CELERY_BROKER_URL=.*|CELERY_BROKER_URL=redis://redis:6379/1|' .env
sed -i.bak 's|^CELERY_RESULT_BACKEND=.*|CELERY_RESULT_BACKEND=redis://redis:6379/2|' .env
rm -f .env.bak

# 2. build & launch
docker compose up --build

# 3. verify
curl http://localhost:8000/api/v1/health
open http://localhost:9001          # MinIO console (minioadmin / minioadmin)
```

Services exposed on the host:

| Service | Port  | Notes                                           |
| ------- | ----- | ----------------------------------------------- |
| api     | 8000  | FastAPI / Swagger UI at `/docs`                 |
| db      | 5432  | PostgreSQL 16 — credentials in `.env`           |
| redis   | 6379  | Redis 7 with append-only persistence            |
| minio   | 9000  | S3 API endpoint                                 |
| minio   | 9001  | MinIO web console                               |

The `api` service runs `alembic upgrade head` on startup, so schema
migrations are applied automatically. The `createbuckets` one-shot
container provisions the audio bucket (`sado-audio` by default).

The image is built once and reused for the `worker` (Celery) service —
`command:` selects between `uvicorn` and `celery worker`. To validate
the compose file without building images:

```bash
JWT_SECRET=ci-test-secret-please-rotate docker compose config -q
```

---

## Demo data

For manual exploration of the dashboard and mobile clients, the
project ships an idempotent seed script:

```bash
# Against the default SQLite database
python -m app.scripts.seed_demo

# Or pointed at the Compose Postgres
DATABASE_URL=postgresql+asyncpg://sado:sado@localhost:5432/sado \
    python -m app.scripts.seed_demo
```

The script creates:

| Entity         | Count | Notes                                            |
| -------------- | ----- | ------------------------------------------------ |
| Regions        | 3     | Country + Toshkent + Samarqand                   |
| Users          | 4     | admin / therapist / teacher / parent             |
| Kindergartens  | 1     | MTM № 1 — Toshkent                               |
| Children       | 2     | Registered to the parent                         |
| Exercises      | 6     | Across 6 categories, all in Uzbek                |
| Assessments    | 2     | Each with one recording + analysis (mock data)   |
| Notifications  | 3     | Pre-populated inbox for parent + therapist       |

Every demo account uses the password **`demo1234`**:

| Role      | Email                |
| --------- | -------------------- |
| admin     | `admin@sado.uz`      |
| therapist | `therapist@sado.uz`  |
| teacher   | `teacher@sado.uz`    |
| parent    | `parent@sado.uz`     |

**Idempotency.** The script uses deterministic UUIDv5 identifiers so
re-running it never duplicates rows and never wipes manual edits — it
inserts only what is missing. Running `seed_demo` ten times leaves the
database in the same state as running it once.

---

## Layout

```
sado-api/
├── app/
│   ├── main.py             # FastAPI factory
│   ├── config.py           # pydantic-settings configuration
│   ├── api/v1/             # versioned routers (health, auth, users, …)
│   ├── core/               # cross-cutting concerns (errors, middleware)
│   ├── models/             # SQLAlchemy ORM models
│   ├── schemas/            # Pydantic request/response schemas
│   ├── services/           # business logic (auth, audio, ML)
│   └── workers/            # Celery app + tasks
├── alembic/                # database migrations
├── tests/                  # pytest test-suite
├── pyproject.toml
└── .env.example
```

---

## Verification

```bash
# Type & syntax check (no deps required beyond stdlib + pydantic)
python -c "from app.main import app; print('ok')"

# Test-suite
python -m pytest -q

# Test-suite with coverage (matches CI threshold)
python -m pytest --cov=app --cov-report=term-missing --cov-fail-under=80 -q

# Linting
ruff check .
```

---

## Continuous Integration

[`.github/workflows/ci.yml`](./.github/workflows/ci.yml) runs three
jobs on every push / PR to `main`:

| Job     | Tools                                    | Gate                          |
| ------- | ---------------------------------------- | ----------------------------- |
| `lint`  | `ruff check .`                           | zero lint errors              |
| `test`  | `pytest --cov=app --cov-fail-under=80`   | all tests green, coverage ≥80%|
| `docker`| `docker compose config -q` + image build | compose file & Dockerfile OK  |

> **Coverage configuration.** `pyproject.toml` configures coverage with
> `concurrency = ["greenlet", "thread"]` so SQLAlchemy's async ORM
> traffic (which travels through greenlet under the hood) is traced
> correctly. Without it the audio-pipeline coverage shows up as ~30 %
> even when those code paths execute. The CI floor is **80 %**.

---

## Configuration

All settings are read from environment variables (or `.env`). See
[`.env.example`](./.env.example) for the full list. The most important
keys:

| Variable                | Default                          | Purpose                       |
| ----------------------- | -------------------------------- | ----------------------------- |
| `DATABASE_URL`          | `sqlite+aiosqlite:///./sado.db`  | Async SQLAlchemy URL          |
| `REDIS_URL`             | `redis://localhost:6379/0`       | Cache + rate limit backend    |
| `JWT_SECRET`            | dev-only string                  | **Must** be rotated in prod   |
| `MINIO_ENDPOINT`        | `http://localhost:9000`          | S3-compatible audio storage   |
| `CORS_ORIGINS`          | localhost dev origins            | Comma-separated allow-list    |
| `MAX_AUDIO_DURATION_SEC`| `60`                             | Hard cap on a single recording|

---

## Roadmap

The implementation follows the milestone plan in
[`../PROJECT_BRIEF.md`](../PROJECT_BRIEF.md):

1. **Foundation** — scaffold, DB models, JWT auth, core CRUD ✅
2. **Core features** — audio upload, Celery workers, ML scoring mock ✅
3. **Polish** — tests, error handling, **Docker Compose** ✅, health checks ✅, **CI** ✅

External services (Whisper, XGBoost, push notifications) are mocked
with deterministic fakes for local development; nothing in this repo
makes paid API calls.
