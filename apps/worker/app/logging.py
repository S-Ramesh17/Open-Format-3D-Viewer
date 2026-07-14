"""
Structured JSON logging for the Celery worker — Week 3 Day 5.

Call configure_worker_logging() from celery_app.py signals so every
worker process emits JSON log lines with task_id and correlation_id
automatically injected into each record.
"""

import json
import logging
import sys
import traceback
from datetime import datetime, timezone
from celery.signals import worker_ready

class _WorkerJSONFormatter(logging.Formatter):
    """
    JSON formatter for Celery worker processes.
    Includes Celery task_id from the thread-local task context when available.
    """

    def format(self, record: logging.LogRecord) -> str:
        log: dict = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Celery makes the current task_id available on the thread-local
        # state when inside a task execution.
        try:
            from celery._state import get_current_task
            task = get_current_task()
            if task and task.request:
                log["task_id"] = task.request.id
                log["task_name"] = task.name
                log["task_retries"] = task.request.retries
        except Exception:
            pass

        if record.exc_info:
            log["exc_info"] = "".join(traceback.format_exception(*record.exc_info))

        return json.dumps(log, ensure_ascii=False, default=str)


def configure_worker_logging(environment: str = "development") -> None:
    """Wire JSON logging into the worker process."""
    root = logging.getLogger()
    root.handlers.clear()

    if environment == "development":
        fmt = "%(asctime)s %(levelname)-8s %(name)s  %(message)s"
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(fmt=fmt, datefmt="%H:%M:%S"))
    else:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_WorkerJSONFormatter())

    root.addHandler(handler)
    root.setLevel(logging.INFO)

    logging.getLogger("celery").setLevel(logging.INFO)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)



@worker_ready.connect
def _start_metrics_server(**kwargs):
    """Start Prometheus HTTP server once per worker process after init."""
    from app.tasks.metrics_server import start_metrics_server
    start_metrics_server()