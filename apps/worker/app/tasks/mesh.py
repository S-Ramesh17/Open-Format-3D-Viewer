from app.celery_app import celery_app


@celery_app.task(name="app.tasks.mesh.generate_chunks")
def generate_chunks(model_id: str) -> dict:
    """Scaffold mesh/XKT chunk generation task."""
    return {"model_id": model_id, "status": "chunked", "queue": "mesh"}