import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.responses import envelope
from app.core.authorization import get_project_member, require_role
from app.core.dependencies import get_current_user
from app.core.request_id import get_request_id
from app.db.engine import get_db
from app.models.project_member import ProjectMember
from app.models.user import User
from app.schemas.projects import ProjectCreate, ProjectUpdate
from app.services.projects import (
    create_project,
    delete_project,
    get_project,
    list_project_members,
    list_projects,
    update_project,
)

router = APIRouter(prefix="/v1/projects", tags=["projects"])


@router.post("", status_code=201)
async def create(
    data: ProjectCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    project = await create_project(data, current_user, db)
    return envelope(project.model_dump(mode="json"), status_code=201)


@router.get("")
async def list_all(
    cursor: str | None = Query(default=None, description="Pagination cursor"),
    limit: int = Query(default=20, ge=1, le=100, description="Results per page"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    projects, next_cursor = await list_projects(current_user, db, limit, cursor)

    return JSONResponse(
        status_code=200,
        content={
            "data": [p.model_dump(mode="json") for p in projects],
            "meta": {
                "request_id": get_request_id(),
                "next_cursor": next_cursor,
            },
        },
    )


@router.get("/{project_id}")
async def get_one(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    project = await get_project(project_id, current_user, db)
    return envelope(project.model_dump(mode="json"))


@router.get("/{project_id}/members")
async def list_members(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    # Membership required to view the member list
    await get_project_member(project_id, current_user, db)

    members = await list_project_members(project_id, db)
    return envelope([m.model_dump(mode="json") for m in members])


@router.patch("/{project_id}")
async def update(
    project_id: uuid.UUID,
    data: ProjectUpdate,
    member: ProjectMember = Depends(require_role("editor")),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    project = await update_project(project_id, data, db)
    # Attach the member's role to response
    project.role = member.role
    return envelope(project.model_dump(mode="json"))


@router.delete("/{project_id}", status_code=204)
async def delete(
    project_id: uuid.UUID,
    member: ProjectMember = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> None:
    await delete_project(project_id, db)