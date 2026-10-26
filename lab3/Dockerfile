FROM python:3.12-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

FROM python:3.12-slim AS runtime
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=5000 \
    LOG_LEVEL=INFO
WORKDIR /app
RUN useradd --create-home --uid 10001 appuser
COPY --from=builder /opt/venv /opt/venv
COPY --chown=appuser:appuser library_app ./library_app
COPY --chown=appuser:appuser wsgi.py .
USER appuser
EXPOSE 5000
HEALTHCHECK --interval=20s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','5000')+'/health',timeout=3)"
CMD ["sh", "-c", "exec gunicorn --workers 1 --threads 1 --bind 0.0.0.0:${PORT} --log-level ${LOG_LEVEL} --access-logfile - --error-logfile - wsgi:app"]
