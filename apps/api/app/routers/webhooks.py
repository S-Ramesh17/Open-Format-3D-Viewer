import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.responses import envelope
from app.core.authorization import get_project_member
from app.core.dependencies import get_current_user
from app.db.engine import get_db
from app.models.user import User
from app.schemas.webhooks import WebhookCreate, WebhookUpdate
from app.services.bcf_export import export_bcf
from app.services.models import get_model
from app.services.webhooks import (
    create_webhook,
    delete_webhook,
    list_webhooks,
    update_webhook,
)

router = APIRouter(prefix="/v1", tags=["webhooks"])


@router.post("/webhooks", status_code=201)
async def create(
    data: WebhookCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    result = await create_webhook(data, current_user, db)
    return envelope(result.model_dump(mode="json"), status_code=201)


@router.get("/webhooks")
async def list_all(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    items = await list_webhooks(current_user, db)
    return envelope([i.model_dump(mode="json") for i in items])


@router.patch("/webhooks/{webhook_id}")
async def update_one(
    webhook_id: uuid.UUID,
    data: WebhookUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    result = await update_webhook(webhook_id, data, current_user, db)
    return envelope(result.model_dump(mode="json"))


@router.delete("/webhooks/{webhook_id}", status_code=204)
async def delete_one(
    webhook_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await delete_webhook(webhook_id, current_user, db)


@router.get("/models/{model_id}/export/bcf")
async def export_model_bcf(
    model_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    model = await get_model(model_id, db)
    await get_project_member(model.project_id, current_user, db)

    zip_bytes = await export_bcf(model_id, db)

    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="model_{model_id}_bcf.zip"'
        },
    )