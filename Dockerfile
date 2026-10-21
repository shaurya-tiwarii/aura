FROM python:3.13-slim

WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 aura

COPY backend /app/backend
COPY backend/docker-entrypoint.py /usr/local/bin/aura-entrypoint.py
RUN chown -R aura:aura /app
WORKDIR /app/backend

ENV APP_ENV=production
ENV PYTHONPATH=/app/backend
EXPOSE 8000

USER aura
ENTRYPOINT ["python", "/usr/local/bin/aura-entrypoint.py"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3)"]
