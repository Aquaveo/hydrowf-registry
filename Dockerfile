# hydrowf-registry: the lego registry (hydrowf_registry.app) on uvicorn, port 8000.
#
#   docker build -t hydrowf-registry .
#   docker run --rm -p 8000:8000 -v registry-data:/data hydrowf-registry          # SQLite in the volume
#
# Each start runs `alembic upgrade head` on DATABASE_URL, then serves. Without DATABASE_URL the database
# is SQLite at /data/registry.db (local use); a cluster sets DATABASE_URL to its Postgres. The rest of the
# environment: HYDROWF_OIDC_ISSUER and HYDROWF_OIDC_AUDIENCE (bearer tokens, as hydrowf-ogc), and
# FORWARDED_ALLOW_IPS (the ingress addresses whose X-Forwarded-* headers uvicorn trusts).
# Seed (also migrates, so it is safe on a fresh database): docker run ... hydrowf-registry \
#   python -m hydrowf_registry.seed /path/to/legos.yaml
# On a cluster with several replicas, run the migrations once (an init Job) rather than at every start.

ARG PYTHON=3.12-slim-bookworm
ARG UV=0.13.0

FROM ghcr.io/astral-sh/uv:${UV} AS uv

FROM python:${PYTHON} AS builder
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*                  # hydrowf-core is pinned by git revision
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /src

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable


FROM python:${PYTHON}
RUN useradd --system --uid 10001 --no-create-home hydrowf \
    && mkdir /data && chown 10001 /data
COPY --from=builder /opt/venv /opt/venv
COPY alembic.ini /opt/hydrowf-registry/
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    DATABASE_URL=sqlite:////data/registry.db
WORKDIR /opt/hydrowf-registry
USER 10001
VOLUME /data
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn --factory hydrowf_registry.app:app_from_env --host 0.0.0.0 --port 8000 --proxy-headers"]
