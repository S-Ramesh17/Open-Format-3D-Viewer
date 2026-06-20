from app.celery_app import celery_app


@celery_app.task(name="app.tasks.scan.scan_file")
def scan_file(model_id: str, s3_key: str) -> dict:
    """
    Scaffold ClamAV scan task.
    Real implementation streams S3 object through clamd, quarantines on infection.
    """
    return {"model_id": model_id, "s3_key": s3_key, "status": "clean", "queue": "scan"}