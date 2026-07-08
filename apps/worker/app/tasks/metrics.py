# apps/worker/app/tasks/metrics.py
"""
Prometheus metrics for the OpenFormat Celery worker.

Import these singletons from any task file. The worker starts a minimal
HTTP server on METRICS_PORT (default 9090) so Prometheus can scrape it
separately from the API.

Usage in a task:
    from app.tasks.metrics import (
        CONVERSION_DURATION, IFC_CONVERSION_DURATION,
        WORKER_SUCCESS, WORKER_FAILURE, ACTIVE_TASKS,
    )

    with ACTIVE_TASKS.track_inprogress():
        with IFC_CONVERSION_DURATION.time():
            # ... processing ...
"""

from prometheus_client import Counter, Gauge, Histogram
import functools
import time as _time
import logging as _logging
# ---------------------------------------------------------------------------
# Per-task duration histograms
# ---------------------------------------------------------------------------

CONVERSION_DURATION = Histogram(
    "conversion_duration_seconds",
    "Total conversion wall time per task (all formats)",
    ["task_name"],
    buckets=[1, 5, 10, 30, 60, 120, 300, 600, 1200, 1800],
)

IFC_CONVERSION_DURATION = Histogram(
    "ifc_conversion_duration_seconds",
    "IFC model processing duration (download + parse + xkt + upload)",
    buckets=[5, 10, 30, 60, 120, 300, 600, 1200, 1800],
)

STEP_CONVERSION_DURATION = Histogram(
    "step_conversion_duration_seconds",
    "STEP model processing duration",
    buckets=[1, 5, 10, 30, 60, 120, 300, 600],
)

CLAMAV_SCAN_DURATION = Histogram(
    "clamav_scan_duration_seconds",
    "Time to scan a file through clamd",
    buckets=[0.1, 0.5, 1, 2, 5, 10, 30, 60],
)

BCF_EXPORT_DURATION = Histogram(
    "bcf_export_duration_seconds",
    "BCF export duration (build + upload)",
    buckets=[0.1, 0.5, 1, 2, 5, 10, 30],
)

WEBHOOK_DELIVERY_DURATION = Histogram(
    "webhook_delivery_duration_seconds",
    "Webhook HTTP delivery duration per attempt",
    buckets=[0.1, 0.5, 1, 2, 5, 10],
)

# ---------------------------------------------------------------------------
# Task outcome counters
# ---------------------------------------------------------------------------

WORKER_SUCCESS = Counter(
    "worker_success_total",
    "Tasks completed successfully",
    ["task_name"],
)

WORKER_FAILURE = Counter(
    "worker_failure_total",
    "Tasks that reached permanent failure (all retries exhausted)",
    ["task_name", "error_type"],
)

WORKER_RETRY = Counter(
    "worker_retry_total",
    "Task retry attempts",
    ["task_name"],
)

# ---------------------------------------------------------------------------
# Active task gauge
# ---------------------------------------------------------------------------

ACTIVE_TASKS = Gauge(
    "active_tasks",
    "Tasks currently executing on this worker",
    ["task_name"],
)

# ---------------------------------------------------------------------------
# Queue depth gauges (updated by collector, not by tasks)
# ---------------------------------------------------------------------------

QUEUE_DEPTH = Gauge(
    "queue_depth",
    "Approximate number of pending tasks in each Celery queue",
    ["queue_name"],
)

# ---------------------------------------------------------------------------
# Domain-level gauges
# ---------------------------------------------------------------------------

SCAN_INFECTIONS_TOTAL = Counter(
    "clamav_infections_total",
    "Total infected files detected",
)

WEBHOOK_DELIVERY_SUCCESS = Counter(
    "webhook_delivery_success_total",
    "Webhook deliveries that received 2xx response",
)

WEBHOOK_DELIVERY_FAILURE = Counter(
    "webhook_delivery_failure_total",
    "Webhook deliveries that failed permanently",
    ["reason"],  # "http_4xx", "http_5xx", "timeout", "connection_error"
)

# ---------------------------------------------------------------------------
# Task instrumentation decorator
# ---------------------------------------------------------------------------



_log = _logging.getLogger(__name__)


def instrument_task(task_name: str, duration_histogram: Histogram | None = None):
    """
    Decorator that wraps a Celery task function with:
      - ACTIVE_TASKS gauge inc/dec
      - CONVERSION_DURATION observation (always)
      - Optional per-format duration_histogram observation
      - WORKER_SUCCESS / WORKER_FAILURE counters

    Usage:
        @celery_app.task(...)
        @instrument_task("ifc.process_model", IFC_CONVERSION_DURATION)
        def process_model(self, model_id):
            ...

    Note: must be applied AFTER @celery_app.task so `self` is the bound task.
    Because Celery's @task decorator replaces the function with a Task object,
    this decorator is applied to the *inner function* before binding, not to
    the Task object itself. See usage pattern in each task file.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            ACTIVE_TASKS.labels(task_name=task_name).inc()
            start = _time.monotonic()
            try:
                result = fn(*args, **kwargs)
                WORKER_SUCCESS.labels(task_name=task_name).inc()
                return result
            except Exception as exc:
                # Distinguish retry from permanent failure
                from celery.exceptions import Retry
                if isinstance(exc, Retry):
                    WORKER_RETRY.labels(task_name=task_name).inc()
                else:
                    WORKER_FAILURE.labels(
                        task_name=task_name,
                        error_type=type(exc).__name__,
                    ).inc()
                raise
            finally:
                elapsed = _time.monotonic() - start
                ACTIVE_TASKS.labels(task_name=task_name).dec()
                CONVERSION_DURATION.labels(task_name=task_name).observe(elapsed)
                if duration_histogram is not None:
                    duration_histogram.observe(elapsed)
        return wrapper
    return decorator