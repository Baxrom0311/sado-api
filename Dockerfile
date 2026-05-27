# syntax=docker/dockerfile:1.7
#
# Multi-stage Dockerfile for the SADO backend API.
#
# - Stage 1 (builder): installs Python deps into an isolated virtualenv.
# - Stage 2 (runtime): copies the venv + app code, runs as a non-root
#   user, and exposes uvicorn on port 8000.
#
# The same image is used for both the FastAPI service and the Celery
# worker — `command` in docker-compose.yml selects the entrypoint.

ARG PYTHON_VERSION=3.12.7

# ---------------------------------------------------------------- builder
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

# Build dependencies for asyncpg, bcrypt, cryptography, and friends.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        libffi-dev \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv "$VIRTUAL_ENV" \
    && pip install --upgrade pip==24.2 setuptools==75.1.0 wheel==0.44.0

WORKDIR /build

# Copy only the dependency manifest first to maximise cache reuse.
COPY pyproject.toml README.md ./

# Pre-create a minimal package layout so `pip install -e .` succeeds
# without copying the full source tree at this stage.
RUN mkdir -p app && touch app/__init__.py

RUN pip install --no-cache-dir -e .

# ---------------------------------------------------------------- runtime
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    APP_HOME=/app \
    HOST=0.0.0.0 \
    PORT=8000

# Runtime libraries only — no compilers in the final image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/*

# Non-root user keeps the image safer in shared environments.
RUN groupadd --system --gid 1001 sado \
    && useradd --system --uid 1001 --gid sado --create-home --shell /sbin/nologin sado

COPY --from=builder /opt/venv /opt/venv

WORKDIR ${APP_HOME}

# Application source is copied last to keep layer cache hits cheap.
COPY --chown=sado:sado pyproject.toml alembic.ini README.md ./
COPY --chown=sado:sado app ./app
COPY --chown=sado:sado alembic ./alembic

# Re-install in editable mode so the package metadata sees the real
# code (the builder stage used a stub layout).
RUN pip install --no-deps --no-cache-dir -e . \
    && mkdir -p /app/storage \
    && chown -R sado:sado /app

USER sado

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl --fail --silent --max-time 4 \
        "http://127.0.0.1:${PORT}/api/v1/health" >/dev/null || exit 1

# tini ensures graceful signal handling for both uvicorn and celery.
ENTRYPOINT ["/usr/bin/tini", "--"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
