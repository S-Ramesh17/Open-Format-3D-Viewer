# OpenFormat Worker

Celery worker for IFC model processing.

## Environment setup

```bash
cd apps/worker
poetry install
cp ../../.env.example .env
# Edit .env — required fields: DATABASE_URL, REDIS_URL, AWS_*, S3_*
```

## External dependencies (must be installed separately)

### ifcopenshell 0.7.x

```bash
pip install ifcopenshell --extra-index-url https://blenderbim.org/pypi/
```

### xeokit-convert (Node.js CLI)

```bash
npm install -g @xeokit/xeokit-convert
```

Requires Node.js 20+. Verify install:

```bash
xeokit-convert --version
```

## Run worker

```bash
poetry run celery -A app.celery_app worker \
  --pool=solo \
  --loglevel=info \
  -Q ifc,mesh,bcf,scan,webhook
```

## Verify queues

```bash
poetry run celery -A app.celery_app inspect active_queues
```

## IFC processing pipeline (Week 2 Day 1)

For each model with `format=ifc`:

1. Download raw file from `S3_RAW_BUCKET`
2. Validate schema — IFC2X3 / IFC4 / IFC4X3 only, reject all others
3. Extract spatial tree (IfcProject → IfcSite → IfcBuilding → IfcBuildingStorey → IfcSpace)
4. Extract all IfcProduct elements (guid, ifc_type, Name, Description, Psets)
5. Convert to chunked XKT via `xeokit-convert` (max chunk size 16MB)
6. Upload XKT chunks to `S3_PROCESSED_BUCKET/{user_id}/{model_id}/processed/`
7. Write `model_elements` + `model_metadata` to PostgreSQL (batch size 500)
8. Update `models.status = "ready"`, `models.s3_processed_prefix`
9. Publish `model_events:{user_id}` on Redis for WebSocket relay

## Task routing

| Task | Queue | Scope |
|------|-------|-------|
| `app.tasks.ifc.process_model` | `ifc` | Week 2 Day 1 — implemented |
| `app.tasks.mesh.generate_chunks` | `mesh` | Week 2 Day 2+ — stub |
| `app.tasks.bcf.export_bcf` | `bcf` | Week 2 Day 2+ — stub |
| `app.tasks.scan.scan_file` | `scan` | Week 2 Day 2+ — stub |
| `app.tasks.webhook.dispatch_webhook` | `webhook` | Week 2 Day 2+ — stub |