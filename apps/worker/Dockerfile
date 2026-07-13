# apps/worker/Dockerfile
FROM python:3.12-slim AS builder

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

RUN npm install -g gltf-pipeline @xeokit/xeokit-convert gltf-validator

FROM python:3.12-slim AS runtime

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    procps \
    nodejs \
    npm \
    gosu \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local /usr/local
COPY --from=builder /app /app

RUN groupadd -r appuser \
    && useradd -r -g appuser -d /app -s /usr/sbin/nologin appuser \
    && mkdir -p /data/openformat \
    && chown -R appuser:appuser /app /data/openformat

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Intentionally NOT `USER appuser` here — the container must start as root
# so entrypoint.sh can fix ownership of PROMETHEUS_MULTIPROC_DIR (a tmpfs
# mounted fresh, root-owned, at container start — see entrypoint.sh for
# why this can't be done at image build time). entrypoint.sh drops to
# appuser via gosu before the real process starts.
ENTRYPOINT ["/entrypoint.sh"]

CMD ["celery", "-A", "app.celery_app.celery_app", "worker", \
     "--loglevel=info", \
     "--queues=ifc,mesh,scan,bcf,webhook", \
     "--concurrency=2"]