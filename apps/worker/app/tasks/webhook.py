from app.celery_app import celery_app


@celery_app.task(name="app.tasks.webhook.dispatch_webhook")
def dispatch_webhook(webhook_id: str, event: str, payload: dict) -> dict:
    """Scaffold webhook dispatch task. Real impl sends HMAC-signed HTTP POST."""
    return {"webhook_id": webhook_id, "event": event, "status": "dispatched", "queue": "webhook"}