"""
Integration tests: RBAC enforcement on all write endpoints.

Every write operation must check the caller's project role. A `viewer`
must receive 403; an `editor` (or admin) must succeed.

Uses db_session to insert a ProjectMember row directly (there is no
HTTP invite-member endpoint). The viewer user is registered via HTTP
so they receive a valid auth cookie.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.models.project_member import ProjectMember

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _register_and_create_project(
    client: AsyncClient, email: str
) -> tuple[str, str]:
    """
    Register a new user (who becomes project owner/admin) and create a project.
    Returns (user_id, project_id).
    """
    reg = await client.post(
        "/v1/auth/register",
        json={"email": email, "password": "testpass123", "name": "Owner"},
    )
    assert reg.status_code == 201, f"Register failed: {reg.text}"
    user_id = reg.json()["data"]["user"]["id"]

    proj_resp = await client.post("/v1/projects", json={"name": "RBAC Test Project"})
    assert proj_resp.status_code == 201, f"Project creation failed: {proj_resp.text}"
    project_id = proj_resp.json()["data"]["id"]
    return user_id, project_id


async def _make_viewer_client(
    db_session: AsyncSession,
    project_id: str,
    email: str,
) -> AsyncClient:
    """
    Register a second user, add them as `viewer` on the project via direct DB
    insert (no HTTP invite endpoint exists), and return an authenticated client.
    """
    transport = ASGITransport(app=app)
    viewer_client = AsyncClient(transport=transport, base_url="https://testserver")

    async def _attach_csrf_header(request):
        csrf_value = viewer_client.cookies.get("csrf_token")
        if csrf_value:
            request.headers["X-CSRF-Token"] = csrf_value

    viewer_client.event_hooks["request"] = [_attach_csrf_header]

    reg = await viewer_client.post(
        "/v1/auth/register",
        json={"email": email, "password": "testpass123", "name": "Viewer"},
    )
    assert reg.status_code == 201, f"Viewer register failed: {reg.text}"
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


async def _upload_model(client: AsyncClient, project_id: str) -> str:
    """Upload a pending model and return model_id."""
    with patch("app.services.models.generate_presigned_upload_url", return_value="https://s3.example.com/url"):
        resp = await client.post(
            "/v1/models/upload",
            json={
                "project_id": project_id,
                "filename": "test.ifc",
                "content_type": "application/octet-stream",
                "size_bytes": 1024,
            },
        )
    assert resp.status_code == 201, f"Upload failed: {resp.text}"
    return resp.json()["data"]["model_id"]


# ---------------------------------------------------------------------------
# Viewer cannot write
# ---------------------------------------------------------------------------

class TestViewerCannotWrite:
    async def test_viewer_403_on_model_upload(
        self, client: AsyncClient, db_session: AsyncSession, unique_email: str
    ):
        _, project_id = await _register_and_create_project(client, unique_email)
        viewer = await _make_viewer_client(db_session, project_id, f"viewer_{unique_email}")

        with patch("app.services.models.generate_presigned_upload_url", return_value="https://s3.example.com/url"):
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
        assert resp.json()["error"]["code"] == "FORBIDDEN"
        await viewer.aclose()

    async def test_viewer_403_on_model_confirm(
        self, client: AsyncClient, db_session: AsyncSession, unique_email: str
    ):
        _, project_id = await _register_and_create_project(client, unique_email)
        model_id = await _upload_model(client, project_id)
        viewer = await _make_viewer_client(db_session, project_id, f"viewer_{unique_email}")

        resp = await viewer.post(f"/v1/models/{model_id}/confirm")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "FORBIDDEN"
        await viewer.aclose()

    async def test_viewer_403_on_annotation_create(
        self, client: AsyncClient, db_session: AsyncSession, unique_email: str
    ):
        _, project_id = await _register_and_create_project(client, unique_email)
        model_id = await _upload_model(client, project_id)
        viewer = await _make_viewer_client(db_session, project_id, f"viewer_{unique_email}")

        with patch("app.services.annotations.publish_model_event", new_callable=AsyncMock), \
             patch("app.services.annotations.dispatch_event", new_callable=AsyncMock):
            resp = await viewer.post(
                f"/v1/models/{model_id}/annotations",
                json={
                    "title": "Test annotation",
                    "position": {"x": 0.0, "y": 0.0, "z": 0.0, "normal_x": 0.0, "normal_y": 1.0, "normal_z": 0.0},
                },
            )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "FORBIDDEN"
        await viewer.aclose()

    async def test_viewer_403_on_annotation_update(
        self, client: AsyncClient, db_session: AsyncSession, unique_email: str
    ):
        _, project_id = await _register_and_create_project(client, unique_email)
        model_id = await _upload_model(client, project_id)

        # Owner creates the annotation
        with patch("app.services.annotations.publish_model_event", new_callable=AsyncMock), \
             patch("app.services.annotations.dispatch_event", new_callable=AsyncMock):
            ann_resp = await client.post(
                f"/v1/models/{model_id}/annotations",
                json={
                    "title": "Original",
                    "position": {"x": 0.0, "y": 0.0, "z": 0.0, "normal_x": 0.0, "normal_y": 1.0, "normal_z": 0.0},
                },
            )
        assert ann_resp.status_code == 201
        annotation_id = ann_resp.json()["data"]["id"]

        viewer = await _make_viewer_client(db_session, project_id, f"viewer_{unique_email}")

        with patch("app.services.annotations.publish_model_event", new_callable=AsyncMock):
            resp = await viewer.patch(
                f"/v1/annotations/{annotation_id}",
                json={"status": "resolved"},
            )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "FORBIDDEN"
        await viewer.aclose()

    async def test_viewer_403_on_comment_create(
        self, client: AsyncClient, db_session: AsyncSession, unique_email: str
    ):
        _, project_id = await _register_and_create_project(client, unique_email)
        model_id = await _upload_model(client, project_id)

        with patch("app.services.annotations.publish_model_event", new_callable=AsyncMock), \
             patch("app.services.annotations.dispatch_event", new_callable=AsyncMock):
            ann_resp = await client.post(
                f"/v1/models/{model_id}/annotations",
                json={
                    "title": "Original",
                    "position": {"x": 0.0, "y": 0.0, "z": 0.0, "normal_x": 0.0, "normal_y": 1.0, "normal_z": 0.0},
                },
            )
        annotation_id = ann_resp.json()["data"]["id"]

        viewer = await _make_viewer_client(db_session, project_id, f"viewer_{unique_email}")
        resp = await viewer.post(
            f"/v1/annotations/{annotation_id}/comments",
            json={"body": "Viewer comment attempt"},
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "FORBIDDEN"
        await viewer.aclose()

    async def test_viewer_403_on_share_link_create(
        self, client: AsyncClient, db_session: AsyncSession, unique_email: str
    ):
        _, project_id = await _register_and_create_project(client, unique_email)
        model_id = await _upload_model(client, project_id)
        viewer = await _make_viewer_client(db_session, project_id, f"viewer_{unique_email}")

        resp = await viewer.post(
            "/v1/share",
            json={"model_id": model_id},
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "FORBIDDEN"
        await viewer.aclose()

    async def test_viewer_403_on_model_delete(
        self, client: AsyncClient, db_session: AsyncSession, unique_email: str
    ):
        _, project_id = await _register_and_create_project(client, unique_email)
        model_id = await _upload_model(client, project_id)
        viewer = await _make_viewer_client(db_session, project_id, f"viewer_{unique_email}")

        resp = await viewer.delete(f"/v1/models/{model_id}")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "FORBIDDEN"
        await viewer.aclose()


# ---------------------------------------------------------------------------
# Editor CAN write (positive cases)
# ---------------------------------------------------------------------------

class TestEditorCanWrite:
    async def _make_editor_client(
        self, db_session: AsyncSession, project_id: str, email: str
    ) -> AsyncClient:
        transport = ASGITransport(app=app)
        editor_client = AsyncClient(transport=transport, base_url="https://testserver")

        async def _attach_csrf_header(request):
            csrf_value = editor_client.cookies.get("csrf_token")
            if csrf_value:
                request.headers["X-CSRF-Token"] = csrf_value

        editor_client.event_hooks["request"] = [_attach_csrf_header]

        reg = await editor_client.post(
            "/v1/auth/register",
            json={"email": email, "password": "testpass123", "name": "Editor"},
        )
        editor_user_id = reg.json()["data"]["user"]["id"]

        db_session.add(
            ProjectMember(
                id=uuid.uuid4(),
                project_id=uuid.UUID(project_id),
                user_id=uuid.UUID(editor_user_id),
                role="editor",
            )
        )
        await db_session.commit()
        return editor_client

    async def test_editor_can_upload_model(
        self, client: AsyncClient, db_session: AsyncSession, unique_email: str
    ):
        _, project_id = await _register_and_create_project(client, unique_email)
        editor = await self._make_editor_client(db_session, project_id, f"editor_{unique_email}")

        with patch("app.services.models.generate_presigned_upload_url", return_value="https://s3.example.com/url"):
            resp = await editor.post(
                "/v1/models/upload",
                json={
                    "project_id": project_id,
                    "filename": "test.ifc",
                    "content_type": "application/octet-stream",
                    "size_bytes": 1024,
                },
            )
        assert resp.status_code == 201
        await editor.aclose()

    async def test_editor_can_create_annotation(
        self, client: AsyncClient, db_session: AsyncSession, unique_email: str
    ):
        _, project_id = await _register_and_create_project(client, unique_email)
        model_id = await _upload_model(client, project_id)
        editor = await self._make_editor_client(db_session, project_id, f"editor_{unique_email}")

        with patch("app.services.annotations.publish_model_event", new_callable=AsyncMock), \
             patch("app.services.annotations.dispatch_event", new_callable=AsyncMock):
            resp = await editor.post(
                f"/v1/models/{model_id}/annotations",
                json={
                    "title": "Editor annotation",
                    "position": {"x": 0.0, "y": 0.0, "z": 0.0, "normal_x": 0.0, "normal_y": 1.0, "normal_z": 0.0},
                },
            )
        assert resp.status_code == 201
        await editor.aclose()

    async def test_editor_cannot_delete_model(
        self, client: AsyncClient, db_session: AsyncSession, unique_email: str
    ):
        """Delete requires admin; editors should receive 403."""
        _, project_id = await _register_and_create_project(client, unique_email)
        model_id = await _upload_model(client, project_id)
        editor = await self._make_editor_client(db_session, project_id, f"editor_{unique_email}")

        resp = await editor.delete(f"/v1/models/{model_id}")
        assert resp.status_code == 403
        await editor.aclose()