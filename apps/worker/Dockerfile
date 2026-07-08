# apps/worker/Dockerfile
FROM python:3.12-slim AS base

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

RUN pip install poetry==1.8.3 --no-cache-dir

COPY pyproject.toml poetry.lock ./
RUN poetry config virtualenvs.create false \
    && poetry install --only main --no-interaction --no-ansi

COPY app/ ./app/

# Install gltf-pipeline for GLTF/STEP Draco compression
RUN npm install -g gltf-pipeline

CMD ["celery", "-A", "app.celery_app.celery_app", "worker", \
     "--loglevel=info", \
     "--queues=ifc,mesh,scan,bcf,webhook", \
     "--concurrency=2"]