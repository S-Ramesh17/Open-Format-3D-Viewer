from celery import Celery
from celery.signals import worker_process_init

from app.config import settings
from app.sentry import init_worker_sentry

init_worker_sentry()


@worker_process_init.connect
def _configure_worker_logging(**kwargs):
    """Configure JSON logging once per worker process after fork."""
    from app.logging import configure_worker_logging
    configure_worker_logging(settings.ENVIRONMENT)

celery_app = Celery(
    "openformat_worker",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_routes={
        "app.tasks.ifc.*": {"queue": "ifc"},
        "app.tasks.mesh.*": {"queue": "mesh"},
        "app.tasks.step.*": {"queue": "mesh"},
        "app.tasks.gltf.*": {"queue": "mesh"},
        "app.tasks.obj.*":  {"queue": "mesh"},
        "app.tasks.stl.*":  {"queue": "mesh"},
        "app.tasks.bcf.*": {"queue": "bcf"},
        "app.tasks.scan.*": {"queue": "scan"},
        "app.tasks.webhook.*": {"queue": "webhook"},
    },
    task_default_queue="ifc",
)

# Beat schedule for queue depth collection — Task 2
celery_app.conf.beat_schedule = {
    "collect-queue-depths": {
        "task": "app.tasks.queue_collector.collect_queue_depths",
        "schedule": 30.0,  # every 30 seconds
    },
    "cleanup-abandoned-uploads": {
        "task": "app.tasks.common.cleanup_abandoned_uploads",
        "schedule": 3600.0,  # every 1 hour
    }
}

import app.tasks.queue_collector  # noqa: E402, F401  — register beat task
import app.tasks.common        # noqa: E402, F401
# Explicit imports so autodiscover works even if PYTHONPATH is minimal
import app.tasks.ifc           # noqa: E402, F401
import app.tasks.mesh          # noqa: E402, F401
import app.tasks.step          # noqa: E402, F401
import app.tasks.gltf          # noqa: E402, F401
import app.tasks.obj           # noqa: E402, F401
import app.tasks.stl           # noqa: E402, F401
import app.tasks.bcf           # noqa: E402, F401
import app.tasks.scan          # noqa: E402, F401
import app.tasks.webhook       # noqa: E402, F401
  
celery_app.autodiscover_tasks(["app"])