"""
Structured JSON logging — Week 3 Day 5.

Configures the root logger to emit JSON lines in production
(ENVIRONMENT != development) with standard fields:
  - timestamp (ISO-8601)
  - level
  - logger (module name)
  - message
  - request_id (injected via contextvars by RequestIDMiddleware)
  - exc_info (when present)

In development, falls back to a human-readable format with the same fields.

Usage:
    from app.core.logging import configure_logging
    configure_logging()  # call once at startup, before any loggers are created
"""

import json
import logging
import sys
import traceback
from datetime import datetime, timezone

from app.core.request_id import get_request_id


class _JSONFormatter(logging.Formatter):
    """
    Format log records as single-line JSON objects.
    Includes request_id from context when available.
    """

    def format(self, record: logging.LogRecord) -> str:
        log: dict = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Attach request_id if set (may be empty string outside request context)
        try:
            rid = get_request_id()
            if rid:
                log["request_id"] = rid
        except Exception:
            pass

        if record.exc_info:
            log["exc_info"] = "".join(traceback.format_exception(*record.exc_info))

        if record.stack_info:
            log["stack_info"] = self.formatStack(record.stack_info)

        # Attach any extra kwargs passed to logger.xxx(msg, extra={...})
        for key, value in record.__dict__.items():
            if key not in {
                "args", "asctime", "created", "exc_info", "exc_text",
                "filename", "funcName", "id", "levelname", "levelno",
                "lineno", "module", "msecs", "message", "msg",
                "name", "pathname", "process", "processName",
                "relativeCreated", "stack_info", "thread", "threadName",
                "taskName",
            } and not key.startswith("_"):
                try:
                    json.dumps(value)  # only include JSON-serialisable extras
                    log[key] = value
                except (TypeError, ValueError):
                    log[key] = str(value)

        return json.dumps(log, ensure_ascii=False, default=str)


class _DevFormatter(logging.Formatter):
    """Human-readable formatter for development."""

    FMT = "%(asctime)s %(levelname)-8s %(name)s  %(message)s"
    DATEFMT = "%H:%M:%S"

    def __init__(self):
        super().__init__(fmt=self.FMT, datefmt=self.DATEFMT)


def configure_logging(environment: str = "development") -> None:
    """
    Configure the root logging handler.
    Call once at application startup before any other code runs.

    Parameters
    ----------
    environment : str
        Should be settings.ENVIRONMENT ("development", "production", "staging").
        Non-development environments use JSON output.
    """
    root = logging.getLogger()

    # Remove any handlers already attached (e.g. by uvicorn startup)
    root.handlers.clear()

    formatter: logging.Formatter
    if environment == "development":
        formatter = _DevFormatter()
    else:
        formatter = _JSONFormatter()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root.addHandler(handler)

    # Default level — can be overridden per-module
    root.setLevel(logging.INFO)

    # Suppress noisy third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
