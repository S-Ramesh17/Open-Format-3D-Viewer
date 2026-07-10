import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.responses import envelope
from app.core.authorization import get_project_member, require_role_for_project
from app.core.dependencies import get_current_user
from app.core.request_id import get_request_id
from app.db.engine import get_db
from app.models.user import User
from app.schemas.annotations import AnnotationCreate, AnnotationUpdate, CommentCreate
from app.services.annotations import (
    create_annotation,
    create_comment,
    get_annotation,
    list_annotations,
    list_comments,
    update_annotation,
)

from app.services.models import get_model
from app.core.profiling import profile

router = APIRouter(prefix="/v1", tags=["annotations"])

@router.get("/models/{model_id}/annotations")
async def list_for_model(
    model_id: uuid.UUID,
    status: str | None = Query(default=None),
    cursor: str | None = Query(default=None, description="Pagination cursor"),
    limit: int = Query(default=20, ge=1, le=100, description="Results per page"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    model = await get_model(model_id, db)
    await get_project_member(model.project_id, current_user, db)

    items, next_cursor = await list_annotations(
        model_id, db, status_filter=status, limit=limit, cursor=cursor
    )
    return envelope(
        [i.model_dump(mode="json") for i in items],
        meta_extra={"next_cursor": next_cursor}
    )


@router.post("/models/{model_id}/annotations", status_code=201)
@profile("annotation_create")
async def create_for_model(
    model_id: uuid.UUID,
    data: AnnotationCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    model = await get_model(model_id, db)
    await require_role_for_project(model.project_id, "editor", current_user, db)

    result = await create_annotation(model_id, data, current_user.id, db)
    return envelope(result.model_dump(mode="json"), status_code=201)


@router.patch("/annotations/{annotation_id}")
async def update_one(
    annotation_id: uuid.UUID,
    data: AnnotationUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    annotation = await get_annotation(annotation_id, db)
    model = await get_model(annotation.model_id, db)
    await require_role_for_project(model.project_id, "editor", current_user, db)

    result = await update_annotation(annotation_id, data, db)
    return envelope(result.model_dump(mode="json"))


@router.post("/annotations/{annotation_id}/comments", status_code=201)
async def add_comment(
    annotation_id: uuid.UUID,
    data: CommentCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    annotation = await get_annotation(annotation_id, db)
    model = await get_model(annotation.model_id, db)
    await require_role_for_project(model.project_id, "editor", current_user, db)

    result = await create_comment(annotation_id, data, current_user.id, db)
    return envelope(result.model_dump(mode="json"), status_code=201)


@router.get("/annotations/{annotation_id}/comments")
async def get_comments(
    annotation_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    annotation = await get_annotation(annotation_id, db)
    model = await get_model(annotation.model_id, db)
    await get_project_member(model.project_id, current_user, db)

    items = await list_comments(annotation_id, db)
    return envelope([i.model_dump(mode="json") for i in items])