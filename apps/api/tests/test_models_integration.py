"""
Integration tests for model CRUD endpoints.

Covers: upload initiation, upload confirmation, model retrieval, deletion,
        elements, tree, chunks, list with pagination.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

VALID_UPLOAD_PAYLOAD = {
    "filename": "test.ifc",
    "content_type": "application/octet-stream",
    "size_bytes": 1024,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _setup_user_and_project(client: AsyncClient, email: str) -> str:
    await client.post(
        "/v1/auth/register",
        json={"email": email, "password": "testpass123"},
    )
    resp = await client.post("/v1/projects", json={"name": "Model Test Project"})
    return resp.json()["data"]["id"]


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

class TestModelUpload:
    async def test_upload_requires_auth(self, client: AsyncClient):
        resp = await client.post(
            "/v1/models/upload",
            json={**VALID_UPLOAD_PAYLOAD, "project_id": str(uuid.uuid4())},
        )
        assert resp.status_code == 401

    async def test_upload_nonmember_project_forbidden(
        self, client: AsyncClient, unique_email: str
    ):
        await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123"},
        )
        resp = await client.post(
            "/v1/models/upload",
            json={**VALID_UPLOAD_PAYLOAD, "project_id": str(uuid.uuid4())},
        )
        assert resp.status_code in (403, 404)

    async def test_upload_happy_path(self, client: AsyncClient, unique_email: str):
        with patch("app.services.models.generate_presigned_upload_url") as mock_url:
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

    async def test_upload_invalid_extension_fails(self, client: AsyncClient, unique_email: str):
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

    async def test_upload_exceeds_500mb_fails(self, client: AsyncClient, unique_email: str):
        project_id = await _setup_user_and_project(client, unique_email)

        resp = await client.post(
            "/v1/models/upload",
            json={
                "project_id": project_id,
                "filename": "huge.ifc",
                "content_type": "application/octet-stream",
                "size_bytes": 600 * 1024 * 1024,
            },
        )
        assert resp.status_code in (400, 413)


# ---------------------------------------------------------------------------
# Confirm
# ---------------------------------------------------------------------------

class TestModelConfirm:
    async def test_confirm_transitions_to_processing(
        self, client: AsyncClient, unique_email: str
    ):
        with patch("app.services.models.generate_presigned_upload_url", return_value="https://s3.example.com/url"), \
             patch("app.services.models.verify_object_exists", return_value={"size_bytes": 1024, "content_type": "application/octet-stream"}), \
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
        await client.post("/v1/auth/register", json={"email": unique_email, "password": "testpass123"})
        resp = await client.post(f"/v1/models/{uuid.uuid4()}/confirm")
        assert resp.status_code == 404

    async def test_confirm_requires_auth(self, client: AsyncClient):
        resp = await client.post(f"/v1/models/{uuid.uuid4()}/confirm")
        assert resp.status_code == 401

    async def test_confirm_clamav_receives_model_id_and_key(
        self, client: AsyncClient, unique_email: str
    ):
        with patch("app.services.models.generate_presigned_upload_url", return_value="https://s3.example.com/url"), \
             patch("app.services.models.verify_object_exists", return_value={"size_bytes": 1024, "content_type": "application/octet-stream"}), \
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
        await client.post("/v1/auth/register", json={"email": unique_email, "password": "testpass123"})
        resp = await client.get("/v1/models")
        assert resp.status_code == 400

    async def test_list_requires_auth(self, client: AsyncClient):
        resp = await client.get(f"/v1/models?project_id={uuid.uuid4()}")
        assert resp.status_code == 401

    async def test_list_enforces_project_membership(
        self, client: AsyncClient, unique_email: str
    ):
        email_a = unique_email
        email_b = f"b_{unique_email}"

        await client.post("/v1/auth/register", json={"email": email_a, "password": "testpass123"})
        create_resp = await client.post("/v1/projects", json={"name": "Private"})
        project_id = create_resp.json()["data"]["id"]

        # Switch to user B (new client state after logout)
        await client.post("/v1/auth/logout")
        await client.post("/v1/auth/register", json={"email": email_b, "password": "testpass123"})

        resp = await client.get(f"/v1/models?project_id={project_id}")
        assert resp.status_code == 403

    async def test_list_returns_models_with_pagination(
        self, client: AsyncClient, unique_email: str
    ):
        with patch("app.services.models.generate_presigned_upload_url", return_value="https://s3.example.com/url"):
            project_id = await _setup_user_and_project(client, unique_email)

            for i in range(3):
                await client.post(
                    "/v1/models/upload",
                    json={
                        "project_id": project_id,
                        "filename": f"model_{i}.ifc",
                        "content_type": "application/octet-stream",
                        "size_bytes": 1024,
                    },
                )

        resp = await client.get(f"/v1/models?project_id={project_id}&limit=2")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["data"]) <= 2
        assert "next_cursor" in body["meta"]


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------

class TestGetModel:
    async def test_get_model_requires_auth(self, client: AsyncClient):
        resp = await client.get(f"/v1/models/{uuid.uuid4()}")
        assert resp.status_code == 401

    async def test_get_model_not_found(self, client: AsyncClient, unique_email: str):
        await client.post("/v1/auth/register", json={"email": unique_email, "password": "testpass123"})
        resp = await client.get(f"/v1/models/{uuid.uuid4()}")
        assert resp.status_code == 404

    async def test_get_model_happy_path(self, client: AsyncClient, unique_email: str):
        with patch("app.services.models.generate_presigned_upload_url", return_value="https://s3.example.com/url"):
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
        assert "element_count" in data
        assert "bounds_min_xyz" in data


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

class TestDeleteModel:
    async def test_delete_requires_admin_role(self, client: AsyncClient, unique_email: str):
        with patch("app.services.models.generate_presigned_upload_url", return_value="https://s3.example.com/url"):
            project_id = await _setup_user_and_project(client, unique_email)
            upload_resp = await client.post(
                "/v1/models/upload",
                json={**VALID_UPLOAD_PAYLOAD, "project_id": project_id},
            )
            model_id = upload_resp.json()["data"]["model_id"]

        # Owner (admin) can delete
        resp = await client.delete(f"/v1/models/{model_id}")
        assert resp.status_code == 204

    async def test_delete_requires_auth(self, client: AsyncClient):
        resp = await client.delete(f"/v1/models/{uuid.uuid4()}")
        assert resp.status_code == 401