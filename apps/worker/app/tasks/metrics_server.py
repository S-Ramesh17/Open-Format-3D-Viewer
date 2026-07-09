# apps/worker/app/tasks/metrics_server.py
"""
Minimal Prometheus HTTP metrics server for the Celery worker.

Starts a background thread serving GET /metrics on METRICS_PORT.
Call start_metrics_server() once from celery_app.py after worker init.

The server is intentionally NOT the API /metrics endpoint — Prometheus
scrapes both API (:8000/metrics) and worker (:9090/metrics) separately.
"""
import os
import logging
import threading
from wsgiref.simple_server import WSGIServer, WSGIRequestHandler, make_server

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest, REGISTRY, CollectorRegistry, multiprocess

from app.config import settings

logger = logging.getLogger(__name__)

_METRICS_PORT = settings.WORKER_METRICS_PORT


class _SilentHandler(WSGIRequestHandler):
    """Suppress access log spam in metrics server."""
    def log_message(self, format, *args):  # noqa: A002
        pass


def _metrics_app(environ, start_response):
    path = environ.get("PATH_INFO", "")
    if path == "/metrics":
        if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
            registry = CollectorRegistry()
            multiprocess.MultiProcessCollector(registry)
        else:
            registry = REGISTRY
        output = generate_latest(registry)
        status = "200 OK"
        headers = [("Content-Type", CONTENT_TYPE_LATEST)]
    elif path == "/health":
        output = b"ok"
        status = "200 OK"
        headers = [("Content-Type", "text/plain")]
    else:
        output = b"not found"
        status = "404 Not Found"
        headers = [("Content-Type", "text/plain")]
    start_response(status, headers)
    return [output]


def start_metrics_server() -> None:
    """
    Start the Prometheus metrics HTTP server in a daemon thread.
    Safe to call multiple times — only starts once (guarded by module-level flag).
    """
    global _started
    if getattr(start_metrics_server, "_started", False):
        return
    start_metrics_server._started = True

    def _serve():
        try:
            httpd = make_server(
                "0.0.0.0",
                _METRICS_PORT,
                _metrics_app,
                server_class=WSGIServer,
                handler_class=_SilentHandler,
            )
            logger.info("Worker metrics server listening on :%d/metrics", _METRICS_PORT)
            httpd.serve_forever()
        except Exception as exc:
            logger.error("Worker metrics server failed to start: %s", exc)

    t = threading.Thread(target=_serve, daemon=True, name="metrics-server")
    t.start()