import sentry_sdk

from app.config import settings


def init_sentry() -> None:
    """
    Initialize Sentry error tracking for the API service.
    No-op if SENTRY_DSN is not configured — safe to call unconditionally.
    """
    if not settings.SENTRY_DSN:
        return

    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.ENVIRONMENT,
        release=settings.APP_VERSION,
        traces_sample_rate=0.1,
        before_send=_fingerprint_grouping,
    )


def _fingerprint_grouping(event, hint):
    """
    Group errors by exception type + the originating module, so a
    ValidationException raised from 5 different routers doesn't all
    collapse into one noisy issue, but the same exception from the
    same router does group together.
    """
    exc_info = hint.get("exc_info")
    if exc_info:
        exc_type = exc_info[0].__name__
        module = exc_info[1].__traceback__.tb_frame.f_globals.get("__name__", "unknown")
        event["fingerprint"] = [exc_type, module]
    return event