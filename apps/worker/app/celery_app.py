from celery import Celery

from app.config import settings
from app.sentry import init_worker_sentry

init_worker_sentry()

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
        "app.tasks.bcf.*": {"queue": "bcf"},
        "app.tasks.scan.*": {"queue": "scan"},
        "app.tasks.webhook.*": {"queue": "webhook"},
    },
    task_default_queue="ifc",
)

# Explicit imports so autodiscover works even if PYTHONPATH is minimal
import app.tasks.ifc       # noqa: E402, F401
import app.tasks.mesh      # noqa: E402, F401
import app.tasks.bcf       # noqa: E402, F401
import app.tasks.scan      # noqa: E402, F401
import app.tasks.webhook   # noqa: E402, F401
import app.tasks.step
import app.tasks.gltf  

celery_app.autodiscover_tasks(["app"])
