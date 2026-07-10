"""
Integration tests for model upload, confirm, list, get, and chunks endpoints.

Covers: upload initiation, confirm (mocked S3), list with project_id,
        get single model, chunks endpoint.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _setup_user_and_project(client: AsyncClient, email: str) -> str:
    """Register, login, create a project. Returns project_id."""
    await client.post(
        "/v1/auth/register",
        json={"email": email, "password": "testpass123", "full_name": "Test User"},
    )
    resp = await client.post("/v1/projects", json={"name": "Model Test Project"})
    return resp.json()["data"]["id"]


VALID_UPLOAD_PAYLOAD = {
    "filename": "test_model.ifc",
    "content_type": "application/octet-stream",
    "size_bytes": 1024 * 100,  # 100 KB
}


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

class TestModelUpload:
    async def test_upload_requires_auth(self, client: AsyncClient):
        import uuid as u
        resp = await client.post(
            "/v1/models/upload",
            json={**VALID_UPLOAD_PAYLOAD, "project_id": str(u.uuid4())},
        )
        assert resp.status_code == 401

    async def test_upload_missing_project_fails(self, client: AsyncClient, unique_email: str):
        await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123"},
        )
        resp = await client.post(
            "/v1/models/upload",
            json={**VALID_UPLOAD_PAYLOAD, "project_id": str(uuid.uuid4())},
        )
        assert resp.status_code in (403, 404)

    @patch("app.services.models.generate_presigned_upload_url")
    async def test_upload_happy_path(
        self, mock_url, client: AsyncClient, unique_email: str
    ):
        mock_url.return_value = "https://s3.example.com/presigned-url"

        project_id = await _setup_user_and_project(client, unique_email)

        resp = await client.post(
            "/v1/models/upload",
            json={**VALID_UPLOAD_PAYLOAD, "project_id": project_id},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert "model_id" in body["data"]
        assert "upload_url" in body["data"]
        assert "storage_key" in body["data"]

    @patch("app.services.models.generate_presigned_upload_url")
    async def test_upload_invalid_extension_fails(
        self, mock_url, client: AsyncClient, unique_email: str
    ):
        mock_url.return_value = "https://s3.example.com/presigned-url"
        project_id = await _setup_user_and_project(client, unique_email)

        resp = await client.post(
            "/v1/models/upload",
            json={
                "project_id": project_id,
                "filename": "malware.exe",
                "content_type": "application/octet-stream",
                "size_bytes": 1024,
            },
        )
        assert resp.status_code == 400

    @patch("app.services.models.generate_presigned_upload_url")
    async def test_upload_exceeds_500mb_fails(
        self, mock_url, client: AsyncClient, unique_email: str
    ):
        project_id = await _setup_user_and_project(client, unique_email)

        resp = await client.post(
            "/v1/models/upload",
            json={
                "project_id": project_id,
                "filename": "huge.ifc",
                "content_type": "application/octet-stream",
                "size_bytes": 600 * 1024 * 1024,  # 600 MB
            },
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Confirm
# ---------------------------------------------------------------------------

class TestModelConfirm:
    async def test_confirm_transitions_to_processing(
        self,
        client: AsyncClient,
        unique_email: str,
    ):
        with patch("app.services.models.generate_presigned_upload_url", return_value="https://s3.example.com/url"), \
             patch("app.services.models.verify_object_exists", return_value={"size_bytes": 1024 * 100, "content_type": "application/octet-stream"}), \
             patch("app.services.models.fetch_object_header_bytes", return_value=b"\x00" * 256), \
             patch("app.services.models.validate_mime_type_from_bytes", return_value="application/octet-stream"), \
             patch("app.services.storage.trigger_clamav_scan"), \
             patch("app.services.models.publish_model_event", new_callable=AsyncMock):

            project_id = await _setup_user_and_project(client, unique_email)

            upload_resp = await client.post(
                "/v1/models/upload",
                json={**VALID_UPLOAD_PAYLOAD, "project_id": project_id},
            )
            model_id = upload_resp.json()["data"]["model_id"]

            confirm_resp = await client.post(f"/v1/models/{model_id}/confirm")

        assert confirm_resp.status_code == 200
        assert confirm_resp.json()["data"]["status"] == "processing"

    async def test_confirm_model_not_found(self, client: AsyncClient, unique_email: str):
        await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123"},
        )
        resp = await client.post(f"/v1/models/{uuid.uuid4()}/confirm")
        assert resp.status_code == 404

    async def test_confirm_requires_auth(self, client: AsyncClient):
        resp = await client.post(f"/v1/models/{uuid.uuid4()}/confirm")
        assert resp.status_code == 401

    async def test_confirm_calls_clamav_with_model_id_and_key(
        self,
        client: AsyncClient,
        unique_email: str,
    ):
        """Regression test: scan task receives both model_id AND storage_key."""
        with patch("app.services.models.generate_presigned_upload_url", return_value="https://s3.example.com/url"), \
             patch("app.services.models.verify_object_exists", return_value={"size_bytes": 1024 * 100, "content_type": "application/octet-stream"}), \
             patch("app.services.models.fetch_object_header_bytes", return_value=b"\x00" * 256), \
             patch("app.services.models.validate_mime_type_from_bytes", return_value="application/octet-stream"), \
             patch("app.services.storage.trigger_clamav_scan") as mock_scan, \
             patch("app.services.models.publish_model_event", new_callable=AsyncMock):

            project_id = await _setup_user_and_project(client, unique_email)

            upload_resp = await client.post(
                "/v1/models/upload",
                json={**VALID_UPLOAD_PAYLOAD, "project_id": project_id},
            )
            model_id = upload_resp.json()["data"]["model_id"]
            storage_key = upload_resp.json()["data"]["storage_key"]

            await client.post(f"/v1/models/{model_id}/confirm")

            mock_scan.assert_called_once_with(model_id, storage_key)
# ---------------------------------------------------------------------------
# List models
# ---------------------------------------------------------------------------

class TestListModels:
    async def test_list_requires_project_id(self, client: AsyncClient, unique_email: str):
        await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123"},
        )
        resp = await client.get("/v1/models")
        assert resp.status_code == 400  # project_id required

    async def test_list_requires_auth(self, client: AsyncClient):
        resp = await client.get(f"/v1/models?project_id={uuid.uuid4()}")
        assert resp.status_code == 401

    async def test_list_enforces_project_membership(
        self, client: AsyncClient, unique_email: str
    ):
        """Non-member cannot list models in another user's project."""
        email_a = unique_email
        email_b = f"b_{unique_email}"

        await client.post(
            "/v1/auth/register",
            json={"email": email_a, "password": "testpass123"},
        )
        create_resp = await client.post("/v1/projects", json={"name": "Private"})
        project_id = create_resp.json()["data"]["id"]

        # Switch to B
        await client.post(
            "/v1/auth/register",
            json={"email": email_b, "password": "testpass123"},
        )
        await client.post(
            "/v1/auth/login",
            json={"email": email_b, "password": "testpass123"},
        )

        resp = await client.get(f"/v1/models?project_id={project_id}")
        assert resp.status_code in (403, 404)

    @patch("app.services.models.generate_presigned_upload_url")
    async def test_list_returns_project_models(
        self, mock_url, client: AsyncClient, unique_email: str
    ):
        mock_url.return_value = "https://s3.example.com/url"
        project_id = await _setup_user_and_project(client, unique_email)

        # Upload a model
        await client.post(
            "/v1/models/upload",
            json={**VALID_UPLOAD_PAYLOAD, "project_id": project_id},
        )

        resp = await client.get(f"/v1/models?project_id={project_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body["data"], list)
        assert len(body["data"]) >= 1
        assert "next_cursor" in body["meta"]


# ---------------------------------------------------------------------------
# Get single model
# ---------------------------------------------------------------------------

class TestGetModel:
    async def test_get_requires_auth(self, client: AsyncClient):
        resp = await client.get(f"/v1/models/{uuid.uuid4()}")
        assert resp.status_code == 401

    async def test_get_nonexistent_returns_404(self, client: AsyncClient, unique_email: str):
        await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123"},
        )
        resp = await client.get(f"/v1/models/{uuid.uuid4()}")
        assert resp.status_code == 404

    @patch("app.services.models.generate_presigned_upload_url")
    async def test_get_returns_model_fields(
        self, mock_url, client: AsyncClient, unique_email: str
    ):
        mock_url.return_value = "https://s3.example.com/url"
        project_id = await _setup_user_and_project(client, unique_email)

        upload_resp = await client.post(
            "/v1/models/upload",
            json={**VALID_UPLOAD_PAYLOAD, "project_id": project_id},
        )
        model_id = upload_resp.json()["data"]["model_id"]

        resp = await client.get(f"/v1/models/{model_id}")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["id"] == model_id
        assert data["status"] == "pending"
        assert data["file_format"] == "ifc"


# ---------------------------------------------------------------------------
# Chunks endpoint
# ---------------------------------------------------------------------------

class TestModelChunks:
    async def test_chunks_requires_auth(self, client: AsyncClient):
        resp = await client.get(f"/v1/models/{uuid.uuid4()}/chunks")
        assert resp.status_code == 401

    async def test_chunks_pending_model_returns_empty(
        self, client: AsyncClient, unique_email: str
    ):
        with patch("app.services.models.generate_presigned_upload_url") as mock_url:
            mock_url.return_value = "https://s3.example.com/url"
            project_id = await _setup_user_and_project(client, unique_email)
            upload_resp = await client.post(
                "/v1/models/upload",
                json={**VALID_UPLOAD_PAYLOAD, "project_id": project_id},
            )
            model_id = upload_resp.json()["data"]["model_id"]

        resp = await client.get(f"/v1/models/{model_id}/chunks")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["chunks"] == []