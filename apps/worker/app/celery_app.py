import importlib
import os
from celery import Celery
from celery.signals import worker_process_init, worker_process_shutdown, celeryd_after_setup

from app.config import settings
from app.sentry import init_worker_sentry

init_worker_sentry()


@celeryd_after_setup.connect
def _start_worker_metrics_server(sender, instance, **kwargs):
    """
    Starts the Prometheus metrics HTTP server persistently in the worker background
    once the worker daemon setup is complete.
    """
    from app.tasks.metrics_server import start_metrics_server
    start_metrics_server()


@worker_process_init.connect
def _configure_worker_logging(**kwargs):
    """Configure JSON logging once per worker process after fork."""
    from app.logging import configure_worker_logging
    configure_worker_logging(settings.ENVIRONMENT)


@worker_process_shutdown.connect
def _cleanup_prometheus_multiproc_files(pid, **kwargs):
    """
    Celery's prefork pool forks a fresh child per --concurrency slot, and
    recycles children based on max_tasks_per_child / crashes. Each child
    gets its own PID-keyed *.db shard under PROMETHEUS_MULTIPROC_DIR (that's
    the whole point of multiprocess mode). Without this, a dead child's
    shard files linger forever and MultiProcessCollector keeps aggregating
    metrics from PIDs that no longer exist — this is prometheus_client's
    own documented cleanup hook for exactly that (see
    prometheus_client.multiprocess.mark_process_dead). No-op if
    PROMETHEUS_MULTIPROC_DIR isn't set (e.g. the `beat` service, which
    doesn't run in multiprocess mode).
    """
    if not os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        return
    try:
        multiprocess = importlib.import_module("prometheus_client.multiprocess")
        multiprocess.mark_process_dead(pid)
    except Exception:
        pass


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