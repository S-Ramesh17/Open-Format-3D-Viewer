import base64
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictException, NotFoundException
from app.models.project import Project
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


# ── Cursor helpers ───────────────────────────────────────────────────────────

def _encode_cursor(created_at: datetime, project_id: uuid.UUID) -> str:
    """Encode a pagination cursor from timestamp + UUID."""
    raw = f"{created_at.isoformat()}|{project_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """Decode a pagination cursor. Raises ValueError if malformed."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts_str, id_str = raw.split("|", 1)
        return datetime.fromisoformat(ts_str), uuid.UUID(id_str)
    except Exception:
        from app.core.exceptions import ValidationException
        raise ValidationException("Invalid pagination cursor")


# ── Service functions ────────────────────────────────────────────────────────

async def create_project(
    data: ProjectCreate,
    owner: User,
    db: AsyncSession,
) -> ProjectResponse:
    """
    Create a new project and automatically add the owner as admin member.
    Returns ProjectResponse with role="admin".
    """
    project = Project(
        id=uuid.uuid4(),
        owner_id=owner.id,
        name=data.name,
        description=data.description,
    )
    db.add(project)
    await db.flush()  # get project.id before adding member

    # Owner is always admin
    member = ProjectMember(
        id=uuid.uuid4(),
        project_id=project.id,
        user_id=owner.id,
        role="admin",
    )
    db.add(member)
    await db.commit()
    await db.refresh(project)

    return _to_response(project, role="admin")


async def list_projects(
    user: User,
    db: AsyncSession,
    limit: int = 20,
    cursor: str | None = None,
) -> tuple[list[ProjectResponse], str | None]:
    """
    List all projects where the user is a member.
    Returns (projects, next_cursor).
    next_cursor is None when no more results exist.
    Ordered by created_at DESC, project.id DESC for stable pagination.
    """
    query = (
        select(Project, ProjectMember.role)
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(ProjectMember.user_id == user.id)
    )

    # Apply cursor if provided
    if cursor:
        cursor_ts, cursor_id = _decode_cursor(cursor)
        query = query.where(
            (Project.created_at < cursor_ts)
            | (
                (Project.created_at == cursor_ts)
                & (Project.id < cursor_id)
            )
        )

    query = query.order_by(
        Project.created_at.desc(),
        Project.id.desc(),
    ).limit(limit + 1)  # fetch one extra to detect next page

    result = await db.execute(query)
    rows = result.all()

    has_more = len(rows) > limit
    rows = rows[:limit]

    projects = [_to_response(row.Project, role=row.role) for row in rows]

    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = _encode_cursor(last.Project.created_at, last.Project.id)

    return projects, next_cursor


async def list_project_members(
    project_id: uuid.UUID,
    db: AsyncSession,
) -> list[ProjectMemberResponse]:
    """
    List all members of a project with their roles.
    Caller must already be verified as a project member (enforced at router level).
    """
    result = await db.execute(
        select(ProjectMember, User.email, User.full_name)
        .join(User, User.id == ProjectMember.user_id)
        .where(ProjectMember.project_id == project_id)
    )
    rows = result.all()
    return [
        ProjectMemberResponse(
            user_id=member.user_id,
            role=member.role,
            email=email,
            full_name=full_name,
        )
        for member, email, full_name in rows
    ]


async def invite_project_member(
    project_id: uuid.UUID,
    data: ProjectMemberInvite,
    db: AsyncSession,
) -> ProjectMemberResponse:
    """
    Add an existing registered user (looked up by email) to a project.

    This adds a user who already has an OpenFormat account — it does not
    send an email or create an invitation for a not-yet-registered address.
    Raises NotFoundException if no user with that email exists.
    Raises ConflictException if the user is already a member of the project.
    """
    result = await db.execute(select(User).where(User.email == data.email))
    user = result.scalar_one_or_none()
    if not user:
        raise NotFoundException(f"No user found with email '{data.email}'")

    result = await db.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user.id,
        )
    )
    if result.scalar_one_or_none():
        raise ConflictException("This user is already a member of the project")

    member = ProjectMember(
        id=uuid.uuid4(),
        project_id=project_id,
        user_id=user.id,
        role=data.role,
    )
    db.add(member)
    await db.commit()
    await db.refresh(member)

    return ProjectMemberResponse(
        user_id=member.user_id,
        role=member.role,
        email=user.email,
        full_name=user.full_name,
    )


async def update_project_member_role(
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    data: ProjectMemberRoleUpdate,
    db: AsyncSession,
) -> ProjectMemberResponse:
    """
    Change a member's role.

    Raises NotFoundException if the user is not a member of the project.
    Raises ConflictException if this would demote the project's last admin
    (every project must always retain at least one admin).
    """
    result = await db.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
    )
    member = result.scalar_one_or_none()
    if not member:
        raise NotFoundException("This user is not a member of the project")

    if member.role == "admin" and data.role != "admin":
        await _ensure_not_last_admin(project_id, user_id, db)

    member.role = data.role
    await db.commit()
    await db.refresh(member)

    user_result = await db.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one()

    return ProjectMemberResponse(
        user_id=member.user_id,
        role=member.role,
        email=user.email,
        full_name=user.full_name,
    )


async def remove_project_member(
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    db: AsyncSession,
) -> None:
    """
    Remove a member from a project.

    Raises NotFoundException if the user is not a member of the project.
    Raises ConflictException if the user is the project owner (ownership
    must be transferred or the project deleted — not handled by this
    endpoint) or the project's last remaining admin.
    """
    result = await db.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
    )
    member = result.scalar_one_or_none()
    if not member:
        raise NotFoundException("This user is not a member of the project")

    result = await db.execute(
        select(Project.owner_id).where(Project.id == project_id)
    )
    owner_id = result.scalar_one_or_none()
    if owner_id == user_id:
        raise ConflictException("The project owner cannot be removed as a member")

    if member.role == "admin":
        await _ensure_not_last_admin(project_id, user_id, db)

    await db.delete(member)
    await db.commit()


async def _ensure_not_last_admin(
    project_id: uuid.UUID,
    excluding_user_id: uuid.UUID,
    db: AsyncSession,
) -> None:
    """Raises ConflictException if excluding this user leaves zero admins."""
    result = await db.execute(
        select(ProjectMember.id).where(
            ProjectMember.project_id == project_id,
            ProjectMember.role == "admin",
            ProjectMember.user_id != excluding_user_id,
        )
    )
    if result.first() is None:
        raise ConflictException(
            "Cannot remove the project's last admin — "
            "promote another member to admin first"
        )


async def get_project(
    project_id: uuid.UUID,
    user: User,
    db: AsyncSession,
) -> ProjectResponse:
    """
    Fetch a single project.
    Enforces membership at query level — not accessible if not a member.
    """
    result = await db.execute(
        select(Project, ProjectMember.role)
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(
            Project.id == project_id,
            ProjectMember.user_id == user.id,
        )
    )
    row = result.first()
    if not row:
        raise NotFoundException("Project not found")

    return _to_response(row.Project, role=row.role)


async def update_project(
    project_id: uuid.UUID,
    data: ProjectUpdate,
    db: AsyncSession,
) -> ProjectResponse:
    """
    Update project name/description.
    Authorization (editor+ role) is enforced by the router dependency.
    """
    result = await db.execute(
        select(Project).where(Project.id == project_id)
    )
    project = result.scalar_one_or_none()
    if not project:
        raise NotFoundException("Project not found")

    if data.name is not None:
        project.name = data.name
    if data.description is not None:
        project.description = data.description

    project.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(project)

    # Fetch current user's role for response
    result = await db.execute(
        select(ProjectMember.role).where(
            ProjectMember.project_id == project_id,
        )
    )
    # Return without role in update — caller has it from dependency
    return _to_response(project, role=None)


async def delete_project(
    project_id: uuid.UUID,
    db: AsyncSession,
) -> None:
    """
    Delete a project and all cascaded records.
    Authorization (admin role) is enforced by the router dependency.
    """
    result = await db.execute(
        select(Project).where(Project.id == project_id)
    )
    project = result.scalar_one_or_none()
    if not project:
        raise NotFoundException("Project not found")

    await db.delete(project)
    await db.commit()


# ── Internal helpers ─────────────────────────────────────────────────────────

def _to_response(project: Project, role: str | None) -> ProjectResponse:
    return ProjectResponse(
        id=project.id,
        owner_id=project.owner_id,
        name=project.name,
        description=project.description,
        created_at=project.created_at,
        updated_at=project.updated_at,
        role=role,
    )