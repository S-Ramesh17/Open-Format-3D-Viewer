import secrets
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundException
from app.models.user import User
from app.models.webhook import Webhook
from app.schemas.webhooks import WebhookCreate, WebhookResponse, WebhookUpdate


async def create_webhook(
    data: WebhookCreate, user: User, db: AsyncSession
) -> WebhookResponse:
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
    return WebhookResponse.model_validate(webhook)


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