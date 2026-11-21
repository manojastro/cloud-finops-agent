FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/app/data/finops.db \
    LLM_MODE=mock

WORKDIR /app
RUN useradd --create-home --uid 10001 appuser && mkdir -p /app/data && chown -R appuser:appuser /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=appuser:appuser backend ./backend
COPY --chown=appuser:appuser frontend ./frontend
USER appuser
EXPOSE 8102
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8102/api/v1/health', timeout=2)"
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8102"]
