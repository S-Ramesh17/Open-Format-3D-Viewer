import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.responses import Envelope, envelope
from app.core.dependencies import get_current_user
from app.db.engine import get_db
from app.models.user import User
from app.schemas.models import (
    ModelChunksResponse,
    ModelElementResponse,
    ModelResponse,
    ModelTreeResponse,
    ModelUploadRequest,
    ModelUploadResponse,
)
import app.services.models
from app.core.profiling import profile

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/models", tags=["models"])


@router.post(
    "/upload/local",
    status_code=200,
    summary="Direct-upload raw bytes (STORAGE_PROVIDER=local only)",
    responses={
        200: {"description": "File stored"},
        404: {"description": "Not available when STORAGE_PROVIDER != local"},
    },
)
async def upload_local_file(
    storage_key: str = Query(..., description="The storage_key returned by POST /upload"),
    file: UploadFile = ...,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """
    Direct file upload endpoint for STORAGE_PROVIDER=local (development only).

    In S3 mode the client PUTs directly to the presigned URL returned by
    POST /upload.  In local mode that URL is a ``local://`` sentinel that
    cannot be used for HTTP upload, so this endpoint fills the gap.

    Workflow (local mode):
      1.  POST /v1/models/upload  → receive model_id, storage_key, upload_url
      2.  POST /v1/models/upload/local?storage_key={key} + multipart file body
      3.  POST /v1/models/{model_id}/confirm

    Returns 404 when STORAGE_PROVIDER != "local" so it is a no-op in production
    and does not create an unintended code path.
    """
    from app.config import settings

    if settings.STORAGE_PROVIDER != "local":
        raise HTTPException(
            status_code=404,
            detail="Direct upload endpoint is only available when STORAGE_PROVIDER=local.",
        )

    import os

    # Security: re-validate the storage_key so a client cannot escape LOCAL_STORAGE_PATH
    # via path traversal.  storage_key is user_id/model_id/filename — all UUID-safe
    # segments separated by "/" — so we only allow that shape.
    if ".." in storage_key or storage_key.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid storage_key.")

    # Security: storage_key alone does not prove the caller owns this upload —
    # it is returned by POST /upload and is not secret (visible in browser
    # devtools/network logs), so without this check any authenticated user
    # could write bytes into another user's model by guessing/observing their
    # storage_key. Parse the model_id out of it and require the same
    # project-editor role that /confirm and /upload already enforce.
    key_parts = storage_key.split("/")
    if len(key_parts) != 3:
        raise HTTPException(status_code=400, detail="Invalid storage_key.")
    try:
        key_model_id = uuid.UUID(key_parts[1])
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid storage_key.")

    from app.core.authorization import require_role_for_project

    model_for_auth = await app.services.models.get_model(key_model_id, db)
    await require_role_for_project(model_for_auth.project_id, "editor", current_user, db)

    dest = os.path.join(settings.LOCAL_STORAGE_PATH, "raw", storage_key)
    os.makedirs(os.path.dirname(dest), exist_ok=True)

    try:
        contents = await file.read()
        if not contents:
            raise HTTPException(status_code=400, detail="Uploaded file is empty.")
        with open(dest, "wb") as fh:
            fh.write(contents)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to save uploaded file: {exc}",
        )

    logger.info(
        "models.upload_local.stored",
        extra={
            "user_id": str(current_user.id),
            "model_id": str(key_model_id),
            "size_bytes": len(contents),
        },
    )

    return envelope(
        {
            "storage_key": storage_key,
            "size_bytes": len(contents),
            "path": dest,
            "status": "stored",
        }
    )


@router.get(
    "",
    summary="List models in a project",
    responses={200: {"model": Envelope[list[ModelResponse]]}},
)
async def list_all(
    project_id: uuid.UUID = Query(..., description="Filter models by project ID"),
    cursor: str | None = Query(default=None, description="Pagination cursor"),
    limit: int = Query(default=20, ge=1, le=100, description="Results per page"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List models belonging to a project the caller is a member of."""
    # Enforce project membership before listing
    from app.core.authorization import get_project_member

    await get_project_member(project_id, current_user, db)

    items, next_cursor = await app.services.models.list_models(project_id, db, limit=limit, cursor=cursor)

    return envelope(
        [m.model_dump(mode="json") for m in items],
        meta_extra={"next_cursor": next_cursor}
    )


@router.post(
    "/upload",
    status_code=201,
    summary="Start a model upload",
    responses={201: {"model": Envelope[ModelUploadResponse]}},
)
@profile("upload_confirm")
async def upload(
    data: ModelUploadRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """
    Create a pending model record and return a presigned upload URL
    (S3 mode) or a local-upload storage_key (local mode). Requires
    editor+ role on the target project.
    """
    # Verify project membership AND editor+ role (viewers cannot upload)
    from app.core.authorization import require_role_for_project

    await require_role_for_project(data.project_id, "editor", current_user, db)

    model_id, upload_result, storage_key = await app.services.models.initiate_upload(data, current_user, db)

    logger.info(
        "models.upload.started",
        extra={"user_id": str(current_user.id), "model_id": str(model_id), "project_id": str(data.project_id)},
    )

    # generate_presigned_upload_url returns a dict {"url": str, "fields": dict}
    # in both local and S3 modes. Unpack it here so the response is always flat.
    if isinstance(upload_result, dict):
        url_str = upload_result["url"]
        url_fields = upload_result.get("fields", {})
    else:
        # Fallback: plain string (e.g. mocked in tests)
        url_str = upload_result
        url_fields = {}

    response = ModelUploadResponse(
        model_id=model_id,
        upload_url=url_str,
        upload_fields=url_fields,
        storage_key=storage_key,
    )
    return envelope(response.model_dump(mode="json"), status_code=201)


@router.post(
    "/{model_id}/confirm",
    summary="Confirm an upload and start processing",
    responses={200: {"model": Envelope[ModelResponse]}},
)
@profile("model_confirm")
async def confirm(
    model_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """
    Confirm a completed upload — verifies the object exists in storage,
    triggers the ClamAV scan, and (once clean) dispatches conversion.
    """
    from app.core.authorization import require_role_for_project

    model = await app.services.models.get_model(model_id, db)
    await require_role_for_project(model.project_id, "editor", current_user, db)

    response = await app.services.models.confirm_upload(model_id, db)
    logger.info(
        "models.upload.completed",
        extra={"user_id": str(current_user.id), "model_id": str(model_id)},
    )
    return envelope(response.model_dump(mode="json"))


@router.get(
    "/{model_id}",
    summary="Get a model",
    responses={200: {"model": Envelope[ModelResponse]}},
)
async def get_one(
    model_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Get a single model's metadata and processing status."""
    model = await app.services.models.get_model(model_id, db)
    from app.core.authorization import get_project_member

    await get_project_member(model.project_id, current_user, db)

    return envelope(model.model_dump(mode="json"))


@router.delete(
    "/{model_id}",
    status_code=204,
    summary="Delete a model",
    responses={204: {"description": "Model and its storage objects deleted"}},
)
async def delete_one(
    model_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a model (DB row, elements, annotations, and storage objects). Requires admin role."""
    model = await app.services.models.get_model(model_id, db)
    from app.core.authorization import get_project_member, ROLE_HIERARCHY
    from app.core.exceptions import AuthorizationException

    member = await get_project_member(model.project_id, current_user, db)
    if ROLE_HIERARCHY.get(member.role, -1) < ROLE_HIERARCHY.get("admin", 0):
        raise AuthorizationException("This action requires 'admin' role.")

    await app.services.models.delete_model(model_id, db)
    logger.info(
        "models.deleted",
        extra={"user_id": str(current_user.id), "model_id": str(model_id)},
    )


@router.get(
    "/{model_id}/elements",
    summary="List a model's elements",
    responses={200: {"model": Envelope[list[ModelElementResponse]]}},
)
async def elements(
    model_id: uuid.UUID,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    ifc_type: str | None = Query(default=None),
    search: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List a model's elements, optionally filtered by IFC type or name search."""
    model = await app.services.models.get_model(model_id, db)
    from app.core.authorization import get_project_member

    await get_project_member(model.project_id, current_user, db)

    items, next_cursor = await app.services.models.list_elements(
        model_id, db, limit=limit, cursor=cursor, ifc_type=ifc_type, search=search
    )

    return envelope(
        [i.model_dump(mode="json") for i in items],
        meta_extra={"next_cursor": next_cursor}
    )


@router.get(
    "/{model_id}/elements/{guid}",
    summary="Get a single element by GUID",
    responses={200: {"model": Envelope[ModelElementResponse]}},
)
async def element_by_guid(
    model_id: uuid.UUID,
    guid: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Get one element's full properties by its IFC GlobalId."""
    model = await app.services.models.get_model(model_id, db)
    from app.core.authorization import get_project_member

    await get_project_member(model.project_id, current_user, db)

    element = await app.services.models.get_element_by_guid(model_id, guid, db)
    return envelope(element.model_dump(mode="json"))


@router.get(
    "/{model_id}/tree",
    summary="Get a model's spatial tree",
    responses={200: {"model": Envelope[ModelTreeResponse]}},
)
async def tree(
    model_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Get the spatial containment tree (site/building/storey/element) for a model."""
    model = await app.services.models.get_model(model_id, db)
    from app.core.authorization import get_project_member

    await get_project_member(model.project_id, current_user, db)

    tree_data = await app.services.models.get_tree(model_id, db)
    return envelope({"model_id": str(model_id), "tree": tree_data})


@router.get(
    "/{model_id}/chunks",
    summary="Get a model's processed XKT chunk URLs",
    responses={200: {"model": Envelope[ModelChunksResponse]}},
)
async def chunks(
    model_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Get the list of chunk URLs a viewer should load for this model."""
    model = await app.services.models.get_model(model_id, db)
    from app.core.authorization import get_project_member

    await get_project_member(model.project_id, current_user, db)

    chunk_list = await app.services.models.get_chunks(model_id, db)
    return envelope({"model_id": str(model_id), "chunks": chunk_list})
