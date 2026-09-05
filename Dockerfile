FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH="/app:/app/src" \
    PATH="/app/venv/bin:$PATH"

WORKDIR /app

RUN addgroup --system --gid 1001 appgroup && \
    adduser --system --uid 1001 --gid 1001 --no-create-home appuser

COPY requirements.txt ./

RUN python -m venv /app/venv && \
    /app/venv/bin/pip install --no-cache-dir --upgrade pip && \
    /app/venv/bin/pip install --no-cache-dir -r requirements.txt

COPY --chown=appuser:appgroup src/ /app/src/
COPY --chown=appuser:appgroup init_db.py /app/
COPY --chown=appuser:appgroup scripts/ /app/scripts/

RUN mkdir -p /app/data && chown appuser:appgroup /app/data

USER appuser
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/health').raise_for_status()"

CMD ["sh", "-c", "python init_db.py && gunicorn src.interface.webhook_app:app --workers 4 --worker-class uvicorn.workers.UvicornWorker --bind  0.0.0.0:8000 --timeout 120 --access-logfile - --error-logfile -"]
