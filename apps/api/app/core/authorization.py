import uuid

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_user
from app.core.exceptions import AuthorizationException, NotFoundException
from app.db.engine import get_db
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.user import User

# Role hierarchy — higher index = more permissions
ROLE_HIERARCHY = {"viewer": 0, "editor": 1, "admin": 2}


async def get_project_member(
    project_id: uuid.UUID,
    user: User,
    db: AsyncSession,
) -> ProjectMember:
    """
    Verify the user is a member of the project.
    Returns the ProjectMember record.
    Raises NotFoundException if project doesn't exist.
    Raises AuthorizationException if user is not a member.
    """
    # Verify project exists
    result = await db.execute(
        select(Project).where(Project.id == project_id)
    )
    project = result.scalar_one_or_none()
    if not project:
        raise NotFoundException("Project not found")

    # Verify membership
    result = await db.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user.id,
        )
    )
    member = result.scalar_one_or_none()
    if not member:
        raise AuthorizationException("You are not a member of this project")

    return member


def require_role(required_role: str):
    """
    Returns a FastAPI dependency that verifies the user has
    at least the required role in the given project.

    Usage:
        @router.patch("/{project_id}")
        async def update_project(
            project_id: uuid.UUID,
            member: ProjectMember = Depends(require_role("editor")),
        ):

    The dependency injects the ProjectMember record so the route
    can access member.role if needed.
    """
    async def dependency(
        project_id: uuid.UUID,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> ProjectMember:
        member = await get_project_member(project_id, current_user, db)

        user_level = ROLE_HIERARCHY.get(member.role, -1)
        required_level = ROLE_HIERARCHY.get(required_role, 0)

        if user_level < required_level:
            raise AuthorizationException(
                f"This action requires '{required_role}' role. "
                f"Your role is '{member.role}'."
            )

        return member

    return dependency