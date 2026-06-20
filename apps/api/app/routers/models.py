import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.responses import envelope
from app.core.authorization import require_role
from app.core.dependencies import get_current_user
from app.core.request_id import get_request_id
from app.db.engine import get_db
from app.models.project_member import ProjectMember
from app.models.user import User
from app.schemas.models import ModelUploadRequest, ModelUploadResponse
from app.services.models import (
    confirm_upload,
    delete_model,
    get_chunks,
    get_element_by_guid,
    get_model,
    get_tree,
    initiate_upload,
    list_elements,
)

router = APIRouter(prefix="/v1/models", tags=["models"])


@router.post("/upload", status_code=201)
async def upload(
    data: ModelUploadRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    # Verify project membership (editor+ required to upload)
    from app.core.authorization import get_project_member

    await get_project_member(data.project_id, current_user, db)

    model_id, upload_url, storage_key = await initiate_upload(data, current_user.id, db)
    response = ModelUploadResponse(
        model_id=model_id, upload_url=upload_url, storage_key=storage_key
    )
    return envelope(response.model_dump(mode="json"), status_code=201)


@router.post("/{model_id}/confirm")
async def confirm(
    model_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    model = await get_model(model_id, db)
    from app.core.authorization import get_project_member

    await get_project_member(model.project_id, current_user, db)

    result = await confirm_upload(model_id, db)
    return envelope(result.model_dump(mode="json"))


@router.get("/{model_id}")
async def get_one(
    model_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    model = await get_model(model_id, db)
    from app.core.authorization import get_project_member

    await get_project_member(model.project_id, current_user, db)

    return envelope(model.model_dump(mode="json"))


@router.delete("/{model_id}", status_code=204)
async def delete_one(
    model_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    model = await get_model(model_id, db)
    from app.core.authorization import get_project_member, ROLE_HIERARCHY
    from app.core.exceptions import AuthorizationException

    member = await get_project_member(model.project_id, current_user, db)
    if ROLE_HIERARCHY.get(member.role, -1) < ROLE_HIERARCHY.get("admin", 0):
        raise AuthorizationException("This action requires 'admin' role.")

    await delete_model(model_id, db)


@router.get("/{model_id}/elements")
async def elements(
    model_id: uuid.UUID,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    ifc_type: str | None = Query(default=None),
    search: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    model = await get_model(model_id, db)
    from app.core.authorization import get_project_member

    await get_project_member(model.project_id, current_user, db)

    items, next_cursor = await list_elements(
        model_id, db, limit=limit, cursor=cursor, ifc_type=ifc_type, search=search
    )

    return JSONResponse(
        status_code=200,
        content={
            "data": [i.model_dump(mode="json") for i in items],
            "meta": {"request_id": get_request_id(), "next_cursor": next_cursor},
        },
    )


@router.get("/{model_id}/elements/{guid}")
async def element_by_guid(
    model_id: uuid.UUID,
    guid: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    model = await get_model(model_id, db)
    from app.core.authorization import get_project_member

    await get_project_member(model.project_id, current_user, db)

    element = await get_element_by_guid(model_id, guid, db)
    return envelope(element.model_dump(mode="json"))


@router.get("/{model_id}/tree")
async def tree(
    model_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    model = await get_model(model_id, db)
    from app.core.authorization import get_project_member

    await get_project_member(model.project_id, current_user, db)

    tree_data = await get_tree(model_id, db)
    return envelope({"model_id": str(model_id), "tree": tree_data})


@router.get("/{model_id}/chunks")
async def chunks(
    model_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    model = await get_model(model_id, db)
    from app.core.authorization import get_project_member

    await get_project_member(model.project_id, current_user, db)

    chunk_list = await get_chunks(model_id, db)
    return envelope({"model_id": str(model_id), "chunks": chunk_list})