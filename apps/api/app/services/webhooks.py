import base64
import secrets
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundException
from app.models.user import User
from app.models.webhook import Webhook
from app.models.webhook_delivery_log import WebhookDeliveryLog
from app.schemas.webhooks import (
    WebhookCreate, 
    WebhookCreateResponse, 
    WebhookDeliveryLogResponse, 
    WebhookResponse, 
    WebhookUpdate
)


def _encode_cursor(created_at: datetime, log_id: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}|{log_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts_str, id_str = raw.split("|")
        return datetime.fromisoformat(ts_str), uuid.UUID(id_str)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("Invalid pagination cursor") from exc


async def create_webhook(
    data: WebhookCreate, user: User, db: AsyncSession
) -> WebhookCreateResponse:
    webhook = Webhook(
        id=uuid.uuid4(),
        user_id=user.id,
        url=data.url,
        secret=secrets.token_hex(32),
        events=data.events,
        is_active=True,
    )
    db.add(webhook)
    await db.commit()
    await db.refresh(webhook)
    return WebhookCreateResponse.model_validate(webhook)


async def list_webhook_deliveries(
    webhook_id: uuid.UUID,
    user: User,
    db: AsyncSession,
    limit: int = 20,
    cursor: str | None = None,
) -> tuple[list[WebhookDeliveryLogResponse], str | None]:
    
    # Verify webhook exists and belongs to the user
    result = await db.execute(
        select(Webhook).where(Webhook.id == webhook_id, Webhook.user_id == user.id)
    )
    if not result.scalar_one_or_none():
        raise NotFoundException("Webhook not found")

    query = select(WebhookDeliveryLog).where(WebhookDeliveryLog.webhook_id == webhook_id)
    
    if cursor:
        cursor_ts, cursor_id = _decode_cursor(cursor)
        query = query.where(
            (WebhookDeliveryLog.created_at < cursor_ts)
            | (
                (WebhookDeliveryLog.created_at == cursor_ts)
                & (WebhookDeliveryLog.id < cursor_id)
            )
        )

    query = query.order_by(
        WebhookDeliveryLog.created_at.desc(),
        WebhookDeliveryLog.id.desc(),
    ).limit(limit + 1)

    result = await db.execute(query)
    rows = result.scalars().all()

    has_more = len(rows) > limit
    rows = rows[:limit]

    logs = [WebhookDeliveryLogResponse.model_validate(r) for r in rows]

    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = _encode_cursor(last.created_at, last.id)

    return logs, next_cursor


async def list_webhooks(user: User, db: AsyncSession) -> list[WebhookResponse]:
    result = await db.execute(select(Webhook).where(Webhook.user_id == user.id))
    rows = result.scalars().all()
    return [WebhookResponse.model_validate(r) for r in rows]


async def update_webhook(
    webhook_id: uuid.UUID, data: WebhookUpdate, user: User, db: AsyncSession
) -> WebhookResponse:
    result = await db.execute(
        select(Webhook).where(Webhook.id == webhook_id, Webhook.user_id == user.id)
    )
    webhook = result.scalar_one_or_none()
    if not webhook:
        raise NotFoundException("Webhook not found")

    if data.url is not None:
        webhook.url = data.url
    if data.events is not None:
        webhook.events = data.events
    if data.is_active is not None:
        webhook.is_active = data.is_active

    webhook.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(webhook)
    return WebhookResponse.model_validate(webhook)


async def delete_webhook(webhook_id: uuid.UUID, user: User, db: AsyncSession) -> None:
    result = await db.execute(
        select(Webhook).where(Webhook.id == webhook_id, Webhook.user_id == user.id)
    )
    webhook = result.scalar_one_or_none()
    if not webhook:
        raise NotFoundException("Webhook not found")
    await db.delete(webhook)
    await db.commit()


async def dispatch_event(event: str, payload: dict, user_id: uuid.UUID, db: AsyncSession) -> None:
    """
    Find all active webhooks for a user subscribed to this event and enqueue
    delivery via Celery. Called by other services (models, annotations) on
    state changes — not exposed as a direct API route.
    """
    from app.core.celery_client import get_celery_client

    result = await db.execute(
        select(Webhook).where(
            Webhook.user_id == user_id,
            Webhook.is_active.is_(True),
        )
    )
    webhooks = result.scalars().all()

    celery_client = get_celery_client()

    for webhook in webhooks:
        if webhook.events and event in webhook.events:
            celery_client.send_task(
                "app.tasks.webhook.dispatch_webhook",
                args=[str(webhook.id), event, payload],
            )