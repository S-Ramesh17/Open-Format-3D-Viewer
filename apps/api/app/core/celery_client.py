"""
Singleton Celery client used by API services to enqueue tasks onto the
worker's queues (scan, webhook delivery, etc).

Previously each call site (`app.services.storage.trigger_clamav_scan`,
`app.services.webhooks.dispatch_event`) constructed its own
`Celery(broker=...)` instance on every invocation. That's wasted work per
request — the client only needs the broker URL and can be shared safely
across a single process, since `send_task` does not require worker-side
task registration.
"""
from celery import Celery

from app.config import settings

_celery_client: Celery | None = None


def get_celery_client() -> Celery:
    global _celery_client
    if _celery_client is None:
        _celery_client = Celery(broker=settings.REDIS_URL)
    return _celery_client