# apps/worker/tests/test_metrics_server.py

"""
Unit tests for the Prometheus metrics server in the Celery worker.

Verifies the HTTP server starts, binds correctly, and serves metrics
from the multiprocess registry.
"""
import threading
import time
import urllib.request
from unittest.mock import patch

import pytest
from prometheus_client import CollectorRegistry

from app.tasks.metrics_server import start_metrics_server


@pytest.fixture
def mock_registry():
    """Provides a clean registry for testing."""
    return CollectorRegistry()


def test_metrics_server_starts_and_serves_data(mock_registry):
    """
    Starts the metrics server on a high random port in a daemon thread,
    makes a real HTTP request to it, and verifies it returns a 200 OK
    with Prometheus-formatted text.
    """
    test_port = 19090  # Use a specific test port to avoid conflicts

    # Start the server in a background thread just like the real worker does
    server_thread = threading.Thread(
        target=start_metrics_server,
        args=(test_port, mock_registry),
        daemon=True
    )
    server_thread.start()

    # Give the server a moment to bind
    time.sleep(0.5)

    try:
        url = f"http://localhost:{test_port}/metrics"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=2) as response:
            assert response.status == 200
            content = response.read().decode('utf-8')
            # Verify it looks like a Prometheus response (even if empty of custom metrics)
            assert "HELP" in content or "TYPE" in content or content == ""
    except Exception as e:
        pytest.fail(f"Failed to connect to metrics server: {e}")


def test_metrics_server_handles_port_in_use(mock_registry, caplog):
    """
    Ensures that if the server fails to bind (e.g., port already in use),
    it logs an error and exits gracefully rather than crashing the worker process.
    """
    import socket
    test_port = 19091

    # Occupy the port first
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(('0.0.0.0', test_port))
    sock.listen(1)

    try:
        # Attempt to start the server on the occupied port
        with patch('app.tasks.metrics_server.logger') as mock_logger:
            start_metrics_server(test_port, mock_registry)
            
            # Verify it logged the error instead of raising an unhandled exception
            assert mock_logger.error.called
            error_message = mock_logger.error.call_args[0][0]
            assert "Failed to start Prometheus metrics server" in error_message
    finally:
        sock.close()