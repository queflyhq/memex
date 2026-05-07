FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install uv (fast, hermetic dep manager) and minimal native deps required by
# DuckDB extensions (curl is needed at runtime by VSS extension auto-fetch on
# fresh installs; ca-certificates makes HTTPS work).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv

WORKDIR /app

# Copy project metadata first to leverage Docker layer cache on dep changes
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

# Install memex with the embed extra so the daemon ships with fastembed.
# Skip pre-pulling models — they download to /data/models (a volume) on first
# use; baking them into the image bloats it from ~250 MB to ~700 MB.
RUN uv pip install --system --no-cache ".[embed]"

# Pre-install DuckDB extensions so the first request doesn't trigger a fetch.
RUN python -c "import duckdb; c = duckdb.connect(':memory:'); \
    c.execute('INSTALL vss; INSTALL fts;'); c.close()"

# Non-root user; data volume is owned by it. K8s setups should match this UID
# in their PVC fsGroup or run with the right securityContext.
RUN useradd --system --uid 10001 --shell /sbin/nologin memex && \
    mkdir -p /data && chown -R memex:memex /data
USER memex

VOLUME ["/data"]
EXPOSE 7777
ENV MEMEX_DATA_DIR=/data \
    MEMEX_LISTEN=0.0.0.0:7777

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:7777/health || exit 1

# Default: long-running HTTP daemon. Override CMD for stdio MCP, migrate, etc.
ENTRYPOINT ["memex"]
CMD ["daemon", "--listen", "0.0.0.0:7777"]
