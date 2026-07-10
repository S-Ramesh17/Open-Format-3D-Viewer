"""
End-to-end pipeline tests — API layer only.

Tests the full HTTP upload → confirm → status lifecycle using the
FastAPI ASGI app directly (no real S3, no real worker).
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

VALID_IFC_BYTES = b"ISO-10303-21;\nHEADER;\nFILE_SCHEMA(('IFC4'));\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

async def _register_and_login(client: AsyncClient, email: str) -> None:
    await client.post(
        "/v1/auth/register",
        json={"email": email, "password": "testpass123!", "full_name": "E2E Tester"},
    )


async def _create_project(client: AsyncClient) -> str:
    resp = await client.post("/v1/projects", json={"name": f"E2E Project {uuid.uuid4().hex[:6]}"})
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


async def _initiate_upload(
    client: AsyncClient,
    project_id: str,
    filename: str = "test.ifc",
    size_bytes: int = len(VALID_IFC_BYTES),
) -> dict:
    resp = await client.post(
        "/v1/models/upload",
        json={
            "project_id": project_id,
            "filename": filename,
            "content_type": "application/octet-stream",
            "size_bytes": size_bytes,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


async def _upload_local(client: AsyncClient, storage_key: str, content: bytes = VALID_IFC_BYTES) -> None:
    resp = await client.post(
        f"/v1/models/upload/local?storage_key={storage_key}",
        files={"file": ("test.ifc", content, "application/octet-stream")},
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Upload flow
# ---------------------------------------------------------------------------

class TestUploadFlow:
    async def test_upload_initiation_returns_model_id_and_url(
        self, client: AsyncClient, unique_email: str, tmp_path
    ):
        await _register_and_login(client, unique_email)
        project_id = await _create_project(client)

        with patch("app.config.settings.STORAGE_PROVIDER", "local"), \
             patch("app.config.settings.LOCAL_STORAGE_PATH", str(tmp_path)):
            data = await _initiate_upload(client, project_id)

        assert "model_id" in data
        assert "storage_key" in data
        assert "upload_url" in data

    async def test_upload_rejects_oversized_file(self, client: AsyncClient, unique_email: str):
        await _register_and_login(client, unique_email)
        project_id = await _create_project(client)

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

    async def test_upload_rejects_invalid_extension(self, client: AsyncClient, unique_email: str):
        await _register_and_login(client, unique_email)
        project_id = await _create_project(client)

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

    async def test_local_upload_stores_bytes_on_disk(
        self, client: AsyncClient, unique_email: str, tmp_path
    ):
        await _register_and_login(client, unique_email)
        project_id = await _create_project(client)

        with patch("app.config.settings.STORAGE_PROVIDER", "local"), \
             patch("app.config.settings.LOCAL_STORAGE_PATH", str(tmp_path)):
            data = await _initiate_upload(client, project_id, size_bytes=len(VALID_IFC_BYTES))
            await _upload_local(client, data["storage_key"])

        dest = tmp_path / "raw" / data["storage_key"]
        assert dest.exists()
        assert dest.read_bytes() == VALID_IFC_BYTES

    async def test_local_upload_404_in_s3_mode(self, client: AsyncClient, unique_email: str):
        await _register_and_login(client, unique_email)
        with patch("app.config.settings.STORAGE_PROVIDER", "s3"):
            resp = await client.post(
                "/v1/models/upload/local?storage_key=a/b/c.ifc",
                files={"file": ("c.ifc", b"data", "application/octet-stream")},
            )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Confirm flow
# ---------------------------------------------------------------------------

class TestConfirmFlow:
    async def test_confirm_transitions_to_processing(
        self, client: AsyncClient, unique_email: str, tmp_path
    ):
        await _register_and_login(client, unique_email)
        project_id = await _create_project(client)

        with patch("app.config.settings.STORAGE_PROVIDER", "local"), \
             patch("app.config.settings.LOCAL_STORAGE_PATH", str(tmp_path)), \
             patch("app.services.storage.trigger_clamav_scan"), \
             patch("app.services.models.publish_model_event", new_callable=AsyncMock):

            data = await _initiate_upload(client, project_id, size_bytes=len(VALID_IFC_BYTES))
            await _upload_local(client, data["storage_key"])

            resp = await client.post(f"/v1/models/{data['model_id']}/confirm")

        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["status"] == "processing"

    async def test_confirm_missing_file_returns_error(
        self, client: AsyncClient, unique_email: str, tmp_path
    ):
        await _register_and_login(client, unique_email)
        project_id = await _create_project(client)

        with patch("app.config.settings.STORAGE_PROVIDER", "local"), \
             patch("app.config.settings.LOCAL_STORAGE_PATH", str(tmp_path)):
            data = await _initiate_upload(client, project_id)
            # Do NOT upload the file
            resp = await client.post(f"/v1/models/{data['model_id']}/confirm")

        # StorageException (file not found) → 502
        assert resp.status_code in (400, 422, 500, 502)

    async def test_confirm_idempotency_rejects_double_confirm(
        self, client: AsyncClient, unique_email: str, tmp_path
    ):
        await _register_and_login(client, unique_email)
        project_id = await _create_project(client)

        with patch("app.config.settings.STORAGE_PROVIDER", "local"), \
             patch("app.config.settings.LOCAL_STORAGE_PATH", str(tmp_path)), \
             patch("app.services.storage.trigger_clamav_scan"), \
             patch("app.services.models.publish_model_event", new_callable=AsyncMock):

            data = await _initiate_upload(client, project_id, size_bytes=len(VALID_IFC_BYTES))
            await _upload_local(client, data["storage_key"])

            r1 = await client.post(f"/v1/models/{data['model_id']}/confirm")
            assert r1.status_code == 200

            r2 = await client.post(f"/v1/models/{data['model_id']}/confirm")
            assert r2.status_code == 400  # ValidationException: not in pending state

    async def test_confirm_dispatches_clamav_scan(
        self, client: AsyncClient, unique_email: str, tmp_path
    ):
        await _register_and_login(client, unique_email)
        project_id = await _create_project(client)

        with patch("app.config.settings.STORAGE_PROVIDER", "local"), \
             patch("app.config.settings.LOCAL_STORAGE_PATH", str(tmp_path)), \
             patch("app.services.storage.trigger_clamav_scan") as mock_scan, \
             patch("app.services.models.publish_model_event", new_callable=AsyncMock):

            data = await _initiate_upload(client, project_id, size_bytes=len(VALID_IFC_BYTES))
            await _upload_local(client, data["storage_key"])

            await client.post(f"/v1/models/{data['model_id']}/confirm")

            mock_scan.assert_called_once_with(data["model_id"], data["storage_key"])


# ---------------------------------------------------------------------------
# S3 mode (mocked boto3)
# ---------------------------------------------------------------------------

class TestS3Mode:
    async def test_presigned_url_in_s3_mode(self, client: AsyncClient, unique_email: str):
        await _register_and_login(client, unique_email)
        project_id = await _create_project(client)

        fake_url = "https://s3.amazonaws.com/bucket/key?signature=abc"

        with patch("app.config.settings.STORAGE_PROVIDER", "s3"), \
             patch("app.services.storage._get_s3_client") as mock_client:
            mock_client.return_value.generate_presigned_url.return_value = fake_url

            resp = await client.post(
                "/v1/models/upload",
                json={
                    "project_id": project_id,
                    "filename": "model.ifc",
                    "content_type": "application/octet-stream",
                    "size_bytes": 1024,
                },
            )

        assert resp.status_code == 201
        assert resp.json()["data"]["upload_url"] == fake_url

    async def test_local_endpoint_unavailable_in_s3_mode(
        self, client: AsyncClient, unique_email: str
    ):
        await _register_and_login(client, unique_email)
        with patch("app.config.settings.STORAGE_PROVIDER", "s3"):
            resp = await client.post(
                "/v1/models/upload/local?storage_key=a/b/c.ifc",
                files={"file": ("c.ifc", b"data", "application/octet-stream")},
            )
        assert resp.status_code == 404