# OpenFormat Worker

## Run
```bash
cd apps/worker
poetry install
cp .env.example .env
poetry run celery -A app.celery_app worker --loglevel=info -Q ifc,mesh,bcf,scan,webhook
```

## Verify
```bash
poetry run celery -A app.celery_app inspect active_queues
```