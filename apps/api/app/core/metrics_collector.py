# apps/api/app/core/metrics_collector.py
"""
Background gauge collector — runs once per scrape if staleness guard allows.

Updates PROCESSING_MODELS gauge from live DB state.
Designed to be called from a FastAPI lifespan background task or from the
/metrics instrumentator's before_process_request hook.
"""

import logging
import time

from sqlalchemy import text

logger = logging.getLogger(__name__)

# Minimum seconds between DB reads (prevents thundering herd on /metrics scrapes)
_COLLECTION_INTERVAL = 15.0
_last_collected = 0.0


async def collect_db_gauges(session_factory) -> None:
    """
    Update Prometheus gauges that require a DB query.
    Call this from the lifespan background task or /metrics handler.
    Safe to call frequently — internally rate-limited.
    """
    global _last_collected
    now = time.monotonic()
    if now - _last_collected < _COLLECTION_INTERVAL:
        return
    _last_collected = now

    from app.core.metrics import PROCESSING_MODELS

    try:
        async with session_factory() as session:
            result = await session.execute(
                text("SELECT COUNT(*) FROM models WHERE status = 'processing'")
            )
            count = result.scalar_one_or_none() or 0
            PROCESSING_MODELS.set(count)
    except Exception as exc:
        logger.warning("metrics_collector: DB gauge collection failed: %s", exc)