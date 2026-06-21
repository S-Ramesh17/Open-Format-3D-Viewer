from app.celery_app import celery_app


@celery_app.task(name="app.tasks.bcf.export_bcf")
def export_bcf(model_id: str) -> dict:
    """Scaffold BCF export task."""
    return {"model_id": model_id, "status": "exported", "queue": "bcf"}