import sentry_sdk

from app.config import settings


def init_worker_sentry() -> None:
    """Initialize Sentry for the Celery worker process. No-op if unset."""
    sentry_dsn = getattr(settings, "SENTRY_DSN", "")
    if not sentry_dsn:
        return

    from sentry_sdk.integrations.celery import CeleryIntegration

    sentry_sdk.init(
        dsn=sentry_dsn,
        environment=getattr(settings, "ENVIRONMENT", "development"),
        integrations=[CeleryIntegration()],
        traces_sample_rate=0.1,
    )