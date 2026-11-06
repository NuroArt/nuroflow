# syntax=docker/dockerfile:1.7
# NuroFlow backend image: ONE image, four roles (api, worker, scheduler, migrate),
# selected by the container command. One artifact per release means the API and the
# workers can never run different code against the same database.
#
#   docker build -t nuroflow-backend backend/
#   # Behind a TLS-inspecting corporate proxy, pass its CA as a build *secret*
#   # (mounted for the build only, never stored in a layer):
#   docker build --secret id=extra_ca,src=corp-ca.pem -t nuroflow-backend backend/

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------- 1. build
FROM python:${PYTHON_VERSION}-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src

# Dependencies first (cached until uv.lock changes), project code second.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then export SSL_CERT_FILE=/run/secrets/extra_ca; fi; \
    uv sync --frozen --no-dev --no-install-project

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then export SSL_CERT_FILE=/run/secrets/extra_ca; fi; \
    uv sync --frozen --no-dev --no-editable

# ---------------------------------------------------------------- 2. runtime
FROM python:${PYTHON_VERSION}-slim AS runtime
LABEL org.opencontainers.image.title="nuroflow-backend" \
      org.opencontainers.image.description="NuroFlow API, worker, scheduler and migrations" \
      org.opencontainers.image.source="https://github.com/Nuroart/nuroflow" \
      org.opencontainers.image.licenses="MIT"

# tini as PID 1: forwards SIGTERM so Celery and the scheduler shut down gracefully,
# and reaps zombie processes (Celery prefork children).
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 nuroflow \
 && useradd --system --uid 10001 --gid nuroflow --home-dir /app --shell /usr/sbin/nologin nuroflow

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NUROFLOW_ENVIRONMENT=production \
    NUROFLOW_LOG_JSON=true
# (No PROMETHEUS_MULTIPROC_DIR here: only the worker uses multiprocess metrics, and
# its CLI creates a fresh directory under /tmp before prometheus_client loads.)

WORKDIR /app
COPY --from=build --chown=root:root /opt/venv /opt/venv
COPY --chown=root:root alembic.ini ./
COPY --chown=root:root alembic ./alembic

# Code is owned by root and read-only to the app user: a compromised process can't
# modify its own code. Compose additionally mounts the root filesystem read-only.
USER nuroflow
EXPOSE 8000 9101 9102
ENTRYPOINT ["/usr/bin/tini", "--"]
# Default role: the API. Override with nuroflow-worker / nuroflow-scheduler /
# "alembic upgrade head".
CMD ["nuroflow-api", "--host", "0.0.0.0", "--proxy-headers", "--forwarded-allow-ips", "*"]
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD ["nuroflow", "healthcheck", "api"]
