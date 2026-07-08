# apps/worker/app/tasks/queue_collector.py
"""
Celery beat task that updates queue depth Prometheus gauges.

Schedule in celery_app.py beat_schedule to run every 30 seconds.
Updates QUEUE_DEPTH gauge for all registered queues.
"""

import logging

from app.celery_app import celery_app
from app.config import settings

logger = logging.getLogger(__name__)

_QUEUES = ["ifc", "mesh", "bcf", "scan", "webhook"]


@celery_app.task(name="app.tasks.queue_collector.collect_queue_depths", queue="ifc")
def collect_queue_depths() -> dict:
    """
    Read queue lengths from Redis and update QUEUE_DEPTH gauge.
    Celery queues are Redis lists — LLEN gives the pending task count.
    """
    from app.tasks.metrics import QUEUE_DEPTH
    import redis as redis_lib

    depths = {}
    try:
        r = redis_lib.from_url(settings.REDIS_URL, decode_responses=True)
        for queue in _QUEUES:
            depth = r.llen(queue) or 0
            QUEUE_DEPTH.labels(queue_name=queue).set(depth)
            depths[queue] = depth
        r.close()
        logger.debug("Queue depths: %s", depths)
    except Exception as exc:
        logger.warning("Failed to collect queue depths: %s", exc)

    return depths