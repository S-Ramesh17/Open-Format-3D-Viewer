import logging
from celery import current_app
from app.tasks.common import get_sync_engine, _raw_sql, update_model_status

logger = logging.getLogger(__name__)

@current_app.task(name="app.tasks.maintenance.cleanup_abandoned_uploads")
def cleanup_abandoned_uploads():
    """Find 'pending' models > 24h, mark 'failed', delete from S3/local."""
    engine = get_sync_engine()
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    
    with engine.connect() as conn:
        rows = conn.execute(
            _raw_sql("SELECT id, raw_s3_key FROM models WHERE status = 'pending' AND created_at < :cutoff"),
            {"cutoff": cutoff}
        ).fetchall()
        
    for row in rows:
        model_id, s3_key = str(row[0]), row[1]
        logger.info("[SWEEPER] Cleaning up abandoned upload model_id=%s", model_id)
        update_model_status(engine, model_id, "failed", error_message="Upload expired")
        try:
            from app.tasks.scan import _delete_s3_object
            _delete_s3_object(s3_key)
        except Exception as exc:
            logger.error("[SWEEPER] Failed to delete %s: %s", s3_key, exc)