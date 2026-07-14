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

_DEFAULT_HOST = "0.0.0.0"
_DEFAULT_PORT = settings.WORKER_METRICS_PORT


class _SilentHandler(WSGIRequestHandler):
    """Suppress access log spam in metrics server."""
    def log_message(self, format, *args):  # noqa: A002
        pass


def _make_metrics_app(registry=None):
    """
    Build the WSGI app.

    If `registry` is given, /metrics always serves that exact registry
    (used by tests, for isolation from process-wide state). Otherwise —
    the production default — /metrics serves the multiprocess-aggregated
    registry when PROMETHEUS_MULTIPROC_DIR is set, or the global default
    REGISTRY otherwise. This matches the existing production behavior
    exactly; only the test path is new.
    """
    def _metrics_app(environ, start_response):
        path = environ.get("PATH_INFO", "")
        if path == "/metrics":
            if registry is not None:
                active_registry = registry
            elif os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
                active_registry = CollectorRegistry()
                multiprocess.MultiProcessCollector(active_registry)
            else:
                active_registry = REGISTRY
            output = generate_latest(active_registry)
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

    return _metrics_app


def _serve_blocking(host: str, port: int, registry=None) -> None:
    """
    Bind and serve forever on the calling thread.
    Logs and returns (does not raise) if the server fails to start —
    e.g. the port is already in use.
    """
    try:
        httpd = make_server(
            host,
            port,
            _make_metrics_app(registry),
            server_class=WSGIServer,
            handler_class=_SilentHandler,
        )
        logger.info("Worker metrics server listening on :%d/metrics", port)
        httpd.serve_forever()
    except Exception as exc:
        logger.error("Failed to start Prometheus metrics server: %s", exc)


def start_metrics_server(port: int = None, registry: CollectorRegistry = None) -> None:
    """
    Start the Prometheus metrics HTTP server.

    Two supported call styles:

    - start_metrics_server() — production usage, called once from
      celery_app.py after worker init. Starts the server in a daemon
      thread and returns immediately; safe to call multiple times (only
      starts once, guarded by start_metrics_server._started). Serves the
      module's default registry logic (multiprocess-aware).

    - start_metrics_server(port, registry) — explicit/blocking usage.
      Binds and serves on the CALLING thread — does not return unless
      the bind fails, in which case it logs the error and returns.
      Intended for tests that want an isolated registry and full control
      over when the call returns.
    """
    if port is not None or registry is not None:
        # Explicit-argument call: synchronous, on the caller's thread.
        _serve_blocking(
            _DEFAULT_HOST,
            port if port is not None else _DEFAULT_PORT,
            registry,
        )
        return

    # No-argument call: existing production behavior — non-blocking,
    # started once in a background daemon thread.
    if getattr(start_metrics_server, "_started", False):
        return
    start_metrics_server._started = True

    t = threading.Thread(
        target=_serve_blocking,
        args=(_DEFAULT_HOST, _DEFAULT_PORT, None),
        daemon=True,
        name="metrics-server",
    )
    t.start()