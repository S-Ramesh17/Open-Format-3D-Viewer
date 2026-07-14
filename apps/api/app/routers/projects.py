import logging
import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.responses import Envelope, envelope
from app.core.authorization import get_project_member, require_role
from app.core.dependencies import get_current_user
from app.core.request_id import get_request_id
from app.db.engine import get_db
from app.models.project_member import ProjectMember
from app.models.user import User
from app.schemas.projects import (
    ProjectCreate,
    ProjectMemberInvite,
    ProjectMemberResponse,
    ProjectMemberRoleUpdate,
    ProjectResponse,
    ProjectUpdate,
)
from app.services.projects import (
    create_project,
    delete_project,
    get_project,
    invite_project_member,
    list_project_members,
    list_projects,
    remove_project_member,
    update_project,
    update_project_member_role,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/projects", tags=["projects"])


@router.post(
    "",
    status_code=201,
    summary="Create a project",
    responses={201: {"model": Envelope[ProjectResponse]}},
)
async def create(
    data: ProjectCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create a project. The caller becomes its first member with admin role."""
    project = await create_project(data, current_user, db)
    logger.info(
        "projects.created",
        extra={"user_id": str(current_user.id), "project_id": str(project.id)},
    )
    return envelope(project.model_dump(mode="json"), status_code=201)


@router.get(
    "",
    summary="List the current user's projects",
    responses={200: {"model": Envelope[list[ProjectResponse]]}},
)
async def list_all(
    cursor: str | None = Query(default=None, description="Pagination cursor"),
    limit: int = Query(default=20, ge=1, le=100, description="Results per page"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List projects the current user is a member of."""
    projects, next_cursor = await list_projects(current_user, db, limit, cursor)

    return envelope(
        [p.model_dump(mode="json") for p in projects],
        meta_extra={"next_cursor": next_cursor}
    )


@router.get(
    "/{project_id}",
    summary="Get a project",
    responses={200: {"model": Envelope[ProjectResponse]}},
)
async def get_one(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Get a project's metadata. Member list is a separate call: GET /{project_id}/members."""
    project = await get_project(project_id, current_user, db)
    return envelope(project.model_dump(mode="json"))


@router.get(
    "/{project_id}/members",
    summary="List project members",
    responses={200: {"model": Envelope[list[ProjectMemberResponse]]}},
)
async def list_members(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List a project's members and their roles. Any member can view this."""
    # Membership required to view the member list
    await get_project_member(project_id, current_user, db)

    members = await list_project_members(project_id, db)
    return envelope([m.model_dump(mode="json") for m in members])


@router.post(
    "/{project_id}/members",
    status_code=201,
    summary="Invite a member to a project",
    responses={201: {"model": Envelope[ProjectMemberResponse]}},
)
async def invite_member(
    project_id: uuid.UUID,
    data: ProjectMemberInvite,
    member: ProjectMember = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Invite an existing registered user to a project by email. Requires admin role."""
    new_member = await invite_project_member(project_id, data, db)
    logger.info(
        "projects.member_invited",
        extra={"project_id": str(project_id), "invited_user_id": str(new_member.user_id), "role": data.role},
    )
    return envelope(new_member.model_dump(mode="json"), status_code=201)


@router.patch(
    "/{project_id}/members/{user_id}",
    summary="Update a project member's role",
    responses={200: {"model": Envelope[ProjectMemberResponse]}},
)
async def update_member_role(
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    data: ProjectMemberRoleUpdate,
    member: ProjectMember = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Change a project member's role (viewer/editor/admin). Requires admin role."""
    updated = await update_project_member_role(project_id, user_id, data, db)
    logger.info(
        "projects.member_role_updated",
        extra={"project_id": str(project_id), "target_user_id": str(user_id), "new_role": data.role},
    )
    return envelope(updated.model_dump(mode="json"))


@router.delete(
    "/{project_id}/members/{user_id}",
    status_code=204,
    summary="Remove a project member",
    responses={204: {"description": "Member removed"}},
)
async def remove_member(
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    member: ProjectMember = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove a member from a project. Requires admin role."""
    await remove_project_member(project_id, user_id, db)
    logger.info(
        "projects.member_removed",
        extra={"project_id": str(project_id), "target_user_id": str(user_id)},
    )


@router.patch(
    "/{project_id}",
    summary="Update a project",
    responses={200: {"model": Envelope[ProjectResponse]}},
)
async def update(
    project_id: uuid.UUID,
    data: ProjectUpdate,
    member: ProjectMember = Depends(require_role("editor")),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Update a project's name/description. Requires editor+ role."""
    project = await update_project(project_id, data, db)
    # Attach the member's role to response
    project.role = member.role
    logger.info(
        "projects.updated",
        extra={"project_id": str(project_id), "fields": list(data.model_dump(exclude_unset=True).keys())},
    )
    return envelope(project.model_dump(mode="json"))


@router.delete(
    "/{project_id}",
    status_code=204,
    summary="Delete a project",
    responses={204: {"description": "Project deleted"}},
)
async def delete(
    project_id: uuid.UUID,
    member: ProjectMember = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a project and all its members/models. Requires admin role."""
    await delete_project(project_id, db)
    logger.info("projects.deleted", extra={"project_id": str(project_id)})
