FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim-bookworm

RUN groupadd --gid 10001 ros-mcp \
    && useradd --uid 10001 --gid ros-mcp --no-create-home --home-dir /tmp ros-mcp

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv

ENV HOME=/tmp \
    PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 10001:10001
ENTRYPOINT ["ros-mcp"]
