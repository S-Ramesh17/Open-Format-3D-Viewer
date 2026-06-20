from celery import Celery

from app.config import settings

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
    task_routes={
        "app.tasks.ifc.*": {"queue": "ifc"},
        "app.tasks.mesh.*": {"queue": "mesh"},
        "app.tasks.bcf.*": {"queue": "bcf"},
        "app.tasks.scan.*": {"queue": "scan"},
        "app.tasks.webhook.*": {"queue": "webhook"},
    },
    task_default_queue="ifc",
)

celery_app.autodiscover_tasks(["app.tasks"])