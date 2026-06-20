from app.celery_app import celery_app


@celery_app.task(name="app.tasks.ifc.process_model")
def process_model(model_id: str) -> dict:
    """
    Scaffold IFC processing task.
    Real implementation parses IFC, extracts elements/metadata, writes to DB.
    """
    return {"model_id": model_id, "status": "processed", "queue": "ifc"}