FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_HTTP_TIMEOUT=300 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH="/app:/app/src" \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN addgroup --system --gid 1001 appgroup && \
    adduser --system --uid 1001 --gid 1001 --no-create-home appuser

COPY --from=builder /app/.venv /app/.venv

COPY --chown=appuser:appgroup src/ /app/src/
COPY --chown=appuser:appgroup init_db.py /app/
COPY --chown=appuser:appgroup scripts/ /app/scripts/

RUN mkdir -p /app/data && chown appuser:appgroup /app/data

USER appuser
EXPOSE 8000

CMD ["sh", "-c", "python init_db.py && uvicorn src.interface.webhook_app:app --host 0.0.0.0 --port 8000 --workers 1"]
