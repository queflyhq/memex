FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install uv (fast, hermetic dep manager)
RUN pip install --no-cache-dir uv

WORKDIR /app

# Copy project metadata first to leverage Docker layer cache on dep changes
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

# Install memex into the system site-packages
RUN uv pip install --system --no-cache .

# Runtime config
VOLUME ["/data"]
EXPOSE 7777
ENV MEMEX_DATA_DIR=/data \
    MEMEX_LISTEN=0.0.0.0:7777

# Default command runs the HTTP daemon. Override with `memex serve` for stdio MCP.
ENTRYPOINT ["memex"]
CMD ["daemon", "--listen", "0.0.0.0:7777"]
