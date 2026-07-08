# apps/api/app/core/profiling.py
"""
Performance profiling decorator for hot code paths.

Records timing to the matching Prometheus histogram so Prometheus
can compute P50, P95, P99 via histogram_quantile().

Usage:
    from app.core.profiling import profile

    @profile("annotation_create")
    async def create_annotation(...):
        ...

Tracked paths:
    annotation_create   → ANNOTATION_DURATION
    auth_login          → AUTH_DURATION (label: "login")
    auth_register       → AUTH_DURATION (label: "register")
    model_confirm       → MODEL_CONFIRM_DURATION
    upload_confirm      → UPLOAD_DURATION
"""

import functools
import time
import logging
from typing import Callable

logger = logging.getLogger(__name__)

# Mapping of profile name → (histogram, label_kwargs)
_PROFILE_MAP: dict[str, tuple] = {}


def _build_profile_map():
    from app.core.metrics import (
        ANNOTATION_DURATION,
        AUTH_DURATION,
        MODEL_CONFIRM_DURATION,
        UPLOAD_DURATION,
    )
    return {
        "annotation_create": (ANNOTATION_DURATION, {}),
        "auth_login": (AUTH_DURATION, {"operation": "login"}),
        "auth_register": (AUTH_DURATION, {"operation": "register"}),
        "auth_refresh": (AUTH_DURATION, {"operation": "refresh"}),
        "model_confirm": (MODEL_CONFIRM_DURATION, {}),
        "upload_confirm": (UPLOAD_DURATION, {}),
    }


def profile(name: str):
    """
    Decorator that times a sync or async function and records to Prometheus.
    """
    def decorator(fn: Callable) -> Callable:
        if asyncio_available(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                start = time.monotonic()
                try:
                    return await fn(*args, **kwargs)
                finally:
                    _record(name, time.monotonic() - start)
            return async_wrapper
        else:
            @functools.wraps(fn)
            def sync_wrapper(*args, **kwargs):
                start = time.monotonic()
                try:
                    return fn(*args, **kwargs)
                finally:
                    _record(name, time.monotonic() - start)
            return sync_wrapper
    return decorator


def asyncio_available(fn: Callable) -> bool:
    import asyncio
    return asyncio.iscoroutinefunction(fn)


def _record(name: str, elapsed: float) -> None:
    global _PROFILE_MAP
    if not _PROFILE_MAP:
        try:
            _PROFILE_MAP = _build_profile_map()
        except Exception:
            return

    entry = _PROFILE_MAP.get(name)
    if entry is None:
        logger.debug("profile: unknown profile name %r", name)
        return

    histogram, labels = entry
    try:
        if labels:
            histogram.labels(**labels).observe(elapsed)
        else:
            histogram.observe(elapsed)
    except Exception as exc:
        logger.debug("profile: failed to record %s: %s", name, exc)