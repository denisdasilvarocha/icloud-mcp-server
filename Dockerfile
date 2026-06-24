# syntax=docker/dockerfile:1

FROM python:3.12-slim AS build

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN python -m venv /venv && /venv/bin/pip install --no-cache-dir .

FROM python:3.12-slim

ENV PATH="/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    ICLOUD_MCP_DATABASE_PATH=/data/icloud-mcp.sqlite3 \
    ICLOUD_MCP_USE_KEYCHAIN=false

RUN groupadd --system --gid 10001 icloud \
    && useradd --system --uid 10001 --gid icloud --home-dir /app --create-home icloud \
    && mkdir -p /data \
    && chown -R icloud:icloud /app /data

WORKDIR /app
COPY --from=build /venv /venv

VOLUME ["/data"]
USER icloud
CMD ["icloud-mcp"]
