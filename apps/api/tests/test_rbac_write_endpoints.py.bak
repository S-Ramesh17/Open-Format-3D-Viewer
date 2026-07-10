"""
Integration tests for Phase 5 (RBAC): every write endpoint must enforce
the project role hierarchy. A viewer must receive 403 on all of them;
an editor (or the project-creating admin) must succeed.

These tests add a second user directly as a `viewer` ProjectMember via
db_session (there is currently no HTTP invite-member endpoint), then
authenticate as that user and hit each write endpoint.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.models.project_member import ProjectMember

pytestmark = pytest.mark.asyncio


async def _register_and_create_project(client: AsyncClient, email: str) -> tuple[str, str]:
    """Register (becomes project admin), create a project. Returns (user_id, project_id)."""
    reg = await client.post(
        "/v1/auth/register",
        json={"email": email, "password": "testpass123", "full_name": "Owner"},
    )
    user_id = reg.json()["data"]["user"]["id"]

    proj_resp = await client.post("/v1/projects", json={"name": "RBAC Test Project"})
    project_id = proj_resp.json()["data"]["id"]
    return user_id, project_id


async def _make_viewer_client(db_session: AsyncSession, project_id: str, email: str) -> AsyncClient:
    """Register a second user, add them as `viewer` on project_id, return an authenticated client."""
    transport = ASGITransport(app=app)
    viewer_client = AsyncClient(transport=transport, base_url="https://testserver")

    reg = await viewer_client.post(
        "/v1/auth/register",
        json={"email": email, "password": "testpass123", "full_name": "Viewer"},
    )
    viewer_user_id = reg.json()["data"]["user"]["id"]

    db_session.add(
        ProjectMember(
            id=uuid.uuid4(),
            project_id=uuid.UUID(project_id),
            user_id=uuid.UUID(viewer_user_id),
            role="viewer",
        )
    )
    await db_session.commit()

    return viewer_client


class TestViewerCannotWrite:
    async def test_viewer_403_on_model_upload(
        self, client: AsyncClient, db_session: AsyncSession, unique_email: str
    ):
        _, project_id = await _register_and_create_project(client, unique_email)
        viewer = await _make_viewer_client(db_session, project_id, f"viewer_{unique_email}")

        resp = await viewer.post(
            "/v1/models/upload",
            json={
                "project_id": project_id,
                "filename": "test.ifc",
                "content_type": "application/octet-stream",
                "size_bytes": 1024,
            },
        )
        assert resp.status_code == 403
        await viewer.aclose()

    async def test_viewer_403_on_annotation_create(
        self, client: AsyncClient, db_session: AsyncSession, unique_email: str
    ):
        _, project_id = await _register_and_create_project(client, unique_email)

        with patch("app.services.models.generate_presigned_upload_url") as m:
            m.return_value = "https://s3.example.com/url"
            model_resp = await client.post(
                "/v1/models/upload",
                json={
                    "project_id": project_id,
                    "filename": "test.ifc",
                    "content_type": "application/octet-stream",
                    "size_bytes": 1024,
                },
            )
        model_id = model_resp.json()["data"]["model_id"]

        viewer = await _make_viewer_client(db_session, project_id, f"viewer_{unique_email}")
        resp = await viewer.post(
            f"/v1/models/{model_id}/annotations",
            json={
                "title": "t",
                "body": "b",
                "position": {
                    "x": 0, "y": 0, "z": 0,
                    "normal_x": 0, "normal_y": 1, "normal_z": 0,
                },
            },
        )
        assert resp.status_code == 403
        await viewer.aclose()

    async def test_viewer_403_on_share_link_create(
        self, client: AsyncClient, db_session: AsyncSession, unique_email: str
    ):
        _, project_id = await _register_and_create_project(client, unique_email)

        with patch("app.services.models.generate_presigned_upload_url") as m:
            m.return_value = "https://s3.example.com/url"
            model_resp = await client.post(
                "/v1/models/upload",
                json={
                    "project_id": project_id,
                    "filename": "test.ifc",
                    "content_type": "application/octet-stream",
                    "size_bytes": 1024,
                },
            )
        model_id = model_resp.json()["data"]["model_id"]

        viewer = await _make_viewer_client(db_session, project_id, f"viewer_{unique_email}")
        resp = await viewer.post("/v1/share", json={"model_id": model_id})
        assert resp.status_code == 403
        await viewer.aclose()


class TestEditorAndAdminCanWrite:
    async def test_admin_owner_can_upload(self, client: AsyncClient, unique_email: str):
        _, project_id = await _register_and_create_project(client, unique_email)

        with patch("app.services.models.generate_presigned_upload_url") as m:
            m.return_value = "https://s3.example.com/url"
            resp = await client.post(
                "/v1/models/upload",
                json={
                    "project_id": project_id,
                    "filename": "test.ifc",
                    "content_type": "application/octet-stream",
                    "size_bytes": 1024,
                },
            )
        assert resp.status_code == 201
