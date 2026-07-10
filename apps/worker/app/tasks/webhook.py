"""
Webhook delivery task — HMAC-SHA256 signed HTTP POST with retry policy.

Delivery contract:
  - POST to webhook.url with JSON payload
  - Signature header: X-OpenFormat-Signature: sha256=<hmac_hex>
  - Delivery-ID header: X-OpenFormat-Delivery: <uuid>
  - Content-Type: application/json
  - Timeout: 10 seconds per attempt
  - Retries: up to 4 times with exponential backoff (30s, 60s, 120s, 300s)
  - Failure logged; webhook NOT disabled automatically (admin responsibility)
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import socket as _socket
import uuid as uuid_module
from datetime import datetime, timezone
from urllib.parse import urlparse

from celery import Task

from app.celery_app import celery_app
from app.tasks.common import get_sync_engine, _raw_sql

logger = logging.getLogger(__name__)

# Delivery timeout per attempt (seconds)
_TIMEOUT = 10

# Exponential backoff delays for retries (seconds)
_RETRY_DELAYS = [30, 60, 120, 300]


# ---------------------------------------------------------------------------
# HMAC signature
# ---------------------------------------------------------------------------

def _build_signature(payload_bytes: bytes, secret: str) -> str:
    """
    Compute HMAC-SHA256 signature over raw payload bytes using the webhook secret.
    Returns the hex digest prefixed with "sha256=".
    """
    sig = hmac.new(
        secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={sig}"


# ---------------------------------------------------------------------------
# Webhook record helpers
# ---------------------------------------------------------------------------

def _get_webhook_row(engine, webhook_id: str) -> dict | None:
    """Fetch webhook row as a plain dict. Returns None if not found."""
    with engine.connect() as conn:
        row = conn.execute(
            _raw_sql(
                "SELECT id, user_id, url, secret, events, is_active "
                "FROM webhooks WHERE id = :wid"
            ),
            {"wid": webhook_id},
        ).fetchone()
    if row is None:
        return None
    return dict(row._mapping)


def _log_delivery(
    engine,
    webhook_id: str,
    delivery_id: str,
    event: str,
    status_code: int | None,
    success: bool,
    error: str | None,
) -> None:
    """
    Write a delivery attempt record. Best-effort — errors are swallowed
    so logging failures never cause a retry of the delivery itself.

    Note: if no webhook_delivery_logs table exists in your schema,
    this silently skips logging. Add the migration if you want audit logs.
    """
    try:
        with engine.begin() as conn:
            conn.execute(
                _raw_sql(
                    "INSERT INTO webhook_delivery_logs "
                    "(id, webhook_id, delivery_id, event, status_code, success, error, created_at) "
                    "VALUES (:id, :wid, :did, :event, :code, :ok, :err, now())"
                ),
                {
                    "id": str(uuid_module.uuid4()),
                    "wid": webhook_id,
                    "did": delivery_id,
                    "event": event,
                    "code": status_code,
                    "ok": success,
                    "err": (error or "")[:1000],
                },
            )
    except Exception as exc:
        # Table may not exist — silently skip
        logger.debug("[WEBHOOK] Delivery log insert failed (table may not exist): %s", exc)

def _validate_webhook_url(url: str) -> None:
    """
    Block SSRF attempts by rejecting:
    - non-HTTPS schemes (downgrade protection)
    - private/loopback/link-local IP addresses (RFC 1918, RFC 3927, RFC 4193)
    - localhost hostnames
    - unresolvable hosts

    Raises ValueError with a descriptive message on failure.
    """
    parsed = urlparse(url)

    if parsed.scheme != "https":
        raise ValueError(f"Webhook URL must use https scheme, got: {parsed.scheme!r}")

    host = parsed.hostname
    if not host:
        raise ValueError("Webhook URL has no hostname")

    # Block obvious localhost names
    _BLOCKED_HOSTNAMES = {
        "localhost", "localhost.localdomain",
        "::1", "0.0.0.0",
    }
    if host.lower() in _BLOCKED_HOSTNAMES:
        raise ValueError(f"Webhook URL hostname is blocked: {host!r}")

    # Resolve and check all IPs for the host
    try:
        addrinfos = _socket.getaddrinfo(host, None)
    except _socket.gaierror as exc:
        raise ValueError(f"Webhook URL hostname could not be resolved: {host!r} ({exc})")

    for addrinfo in addrinfos:
        ip_str = addrinfo[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ValueError(
                f"Webhook URL resolves to a blocked IP address: {ip_str} "
                f"(private/loopback/link-local addresses are not allowed)"
            )

# ---------------------------------------------------------------------------
# HTTP delivery
# ---------------------------------------------------------------------------
def _deliver(
    url: str,
    secret: str,
    event: str,
    payload: dict,
    delivery_id: str,
) -> tuple[int, bool]:
    """
    Send the signed HTTP POST. Returns (status_code, success).
    Raises requests.RequestException on connectivity/timeout failures.
    """
    import requests  # optional dep — already available via sentry-sdk transitive

    payload_bytes = json.dumps(
        {
            "id": delivery_id,
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": payload,
        },
        separators=(",", ":"),
    ).encode("utf-8")

    signature = _build_signature(payload_bytes, secret)

    headers = {
        "Content-Type": "application/json",
        "X-OpenFormat-Signature": signature,
        "X-OpenFormat-Delivery": delivery_id,
        "X-OpenFormat-Event": event,
        "User-Agent": "OpenFormat-Webhook/1.0",
    }

    logger.info(
        "[WEBHOOK] POST %s event=%s delivery_id=%s",
        url, event, delivery_id,
    )

    response = requests.post(
        url,
        data=payload_bytes,
        headers=headers,
        timeout=_TIMEOUT,
        allow_redirects=False,
    )

    success = 200 <= response.status_code < 300
    logger.info(
        "[WEBHOOK] Response: %d %s (delivery_id=%s)",
        response.status_code,
        "OK" if success else "FAIL",
        delivery_id,
    )
    return response.status_code, success


# ---------------------------------------------------------------------------
# Main Celery task
# ---------------------------------------------------------------------------

@celery_app.task(
    name="app.tasks.webhook.dispatch_webhook",
    bind=True,
    max_retries=4,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="webhook",
)
def dispatch_webhook(self: Task, webhook_id: str, event: str, payload: dict) -> dict:
    """
    Deliver a signed webhook HTTP POST.

    Parameters
    ----------
    webhook_id : str  — Webhook UUID (looked up to get URL + secret)
    event      : str  — Event name e.g. "model.ready", "annotation.created"
    payload    : dict — Arbitrary event data to include in the POST body
    """
    import requests

    delivery_id = str(uuid_module.uuid4())
    logger.info(
        "[WEBHOOK] Dispatching webhook_id=%s event=%s delivery_id=%s",
        webhook_id, event, delivery_id,
    )

    engine = get_sync_engine()
    webhook = _get_webhook_row(engine, webhook_id)

    if webhook is None:
        logger.error("[WEBHOOK] Webhook %s not found — skipping", webhook_id)
        return {"webhook_id": webhook_id, "status": "not_found"}

    if not webhook.get("is_active"):
        logger.info("[WEBHOOK] Webhook %s is inactive — skipping", webhook_id)
        return {"webhook_id": webhook_id, "status": "inactive"}

    url: str = webhook["url"]
    secret: str = webhook["secret"]

    # SSRF protection — block private/loopback addresses before making the request
    try:
        _validate_webhook_url(url)
    except ValueError as exc:
        error_msg = f"Webhook URL blocked by SSRF protection: {exc}"
        logger.warning("[WEBHOOK] %s webhook_id=%s", error_msg, webhook_id)
        _log_delivery(engine, webhook_id, delivery_id, event, None, False, error_msg)
        return {
            "webhook_id": webhook_id,
            "delivery_id": delivery_id,
            "event": event,
            "status": "blocked",
            "error": error_msg,
        }
    try:
        status_code, success = _deliver(url, secret, event, payload, delivery_id)
    except requests.Timeout as exc:
        attempt = self.request.retries
        delay = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
        logger.warning(
            "[WEBHOOK] Timeout delivering to %s (attempt %d) — retrying in %ds",
            url, attempt + 1, delay,
        )
        _log_delivery(engine, webhook_id, delivery_id, event, None, False, f"Timeout: {exc}")
        raise self.retry(exc=exc, countdown=delay)
    except requests.RequestException as exc:
        attempt = self.request.retries
        delay = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
        logger.warning(
            "[WEBHOOK] Connection error delivering to %s (attempt %d): %s — retrying in %ds",
            url, attempt + 1, exc, delay,
        )
        _log_delivery(engine, webhook_id, delivery_id, event, None, False, str(exc)[:500])
        raise self.retry(exc=exc, countdown=delay)
    except Exception as exc:
        # Non-requests exception (e.g. JSON serialisation error) — log, do not retry
        logger.exception(
            "[WEBHOOK] Unexpected error dispatching webhook_id=%s: %s",
            webhook_id, exc,
        )
        _log_delivery(engine, webhook_id, delivery_id, event, None, False, str(exc)[:500])
        return {
            "webhook_id": webhook_id,
            "delivery_id": delivery_id,
            "event": event,
            "status": "error",
            "error": str(exc)[:400],
        }

    _log_delivery(engine, webhook_id, delivery_id, event, status_code, success, None)

    if success:
        return {
            "webhook_id": webhook_id,
            "delivery_id": delivery_id,
            "event": event,
            "status": "delivered",
            "status_code": status_code,
        }

    # 4xx — permanent failure, do not retry
    if status_code is not None and 400 <= status_code < 500:
        error_msg = f"HTTP {status_code} — client error, not retrying"
        logger.warning("[WEBHOOK] %s for webhook_id=%s", error_msg, webhook_id)
        return {
            "webhook_id": webhook_id,
            "delivery_id": delivery_id,
            "event": event,
            "status": "failed",
            "status_code": status_code,
        }

    # 5xx or unexpected status — retry with exponential backoff
    # NOTE: self.retry() raises celery.exceptions.Retry — this must NOT be inside
    # the try/except block above, otherwise the bare `except Exception` would catch
    # Retry and swallow it, preventing the task from actually retrying.
    attempt = self.request.retries
    delay = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
    logger.warning(
        "[WEBHOOK] HTTP %s from %s (attempt %d) — retrying in %ds",
        status_code, url, attempt + 1, delay,
    )
    raise self.retry(
        exc=Exception(f"HTTP {status_code}"),
        countdown=delay,
    )
