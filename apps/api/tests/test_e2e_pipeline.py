"""
End-to-end pipeline tests — API layer only.

Tests the full HTTP request/response cycle using httpx AsyncClient against
the real FastAPI ASGI app. No worker internals imported here.

Worker-internal tests (scan, bcf, webhook, error_handler, Redis events,
worker reliability) live in apps/worker/tests/test_worker_pipeline.py
because they import from app.tasks.*, which is the worker package.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

async def _register_and_login(client: AsyncClient, email: str) -> None:
    await client.post(
        "/v1/auth/register",
        json={"email": email, "password": "testpass123!", "full_name": "E2E Tester"},
    )


async def _create_project(client: AsyncClient) -> str:
    resp = await client.post(
        "/v1/projects", json={"name": f"E2E Project {uuid.uuid4().hex[:6]}"}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


async def _initiate_upload(
    client: AsyncClient,
    project_id: str,
    filename: str = "test.ifc",
    size_bytes: int = 14,
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


async def _upload_local_file(
    client: AsyncClient,
    storage_key: str,
    content: bytes = b"ISO-10303-21;",
) -> None:
    resp = await client.post(
        f"/v1/models/upload/local?storage_key={storage_key}",
        files={"file": ("test.ifc", content, "application/octet-stream")},
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Upload flow
# ---------------------------------------------------------------------------

class TestUploadFlow:
    async def test_full_upload_initiation_ifc(
        self, client: AsyncClient, unique_email: str, tmp_path
    ):
        await _register_and_login(client, unique_email)
        project_id = await _create_project(client)

        with patch("app.config.settings.STORAGE_PROVIDER", "local"), \
             patch("app.config.settings.LOCAL_STORAGE_PATH", str(tmp_path)):
            data = await _initiate_upload(client, project_id, "building.ifc", size_bytes=14)

        assert "model_id" in data
        assert "storage_key" in data

    async def test_full_upload_initiation_step(
        self, client: AsyncClient, unique_email: str, tmp_path
    ):
        await _register_and_login(client, unique_email)
        project_id = await _create_project(client)

        with patch("app.config.settings.STORAGE_PROVIDER", "local"), \
             patch("app.config.settings.LOCAL_STORAGE_PATH", str(tmp_path)):
            data = await _initiate_upload(client, project_id, "structure.step", size_bytes=20)

        assert "model_id" in data
        assert data["storage_key"].endswith("structure.step")

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
        assert resp.status_code in (400, 413, 422)

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
        assert resp.status_code == 422

    async def test_local_file_upload_stores_file(
        self, client: AsyncClient, unique_email: str, tmp_path
    ):
        await _register_and_login(client, unique_email)
        project_id = await _create_project(client)
        content = b"ISO-10303-21;\nHEADER;\nFILE_SCHEMA(('IFC4'));\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;"

        with patch("app.config.settings.STORAGE_PROVIDER", "local"), \
             patch("app.config.settings.LOCAL_STORAGE_PATH", str(tmp_path)):
            data = await _initiate_upload(client, project_id, "model.ifc", size_bytes=len(content))
            storage_key = data["storage_key"]
            await _upload_local_file(client, storage_key, content)

        dest = tmp_path / "raw" / storage_key
        assert dest.exists()
        assert dest.read_bytes() == content


# ---------------------------------------------------------------------------
# Confirm flow
# ---------------------------------------------------------------------------

class TestConfirmFlow:
    async def test_confirm_dispatches_scan(
        self, client: AsyncClient, unique_email: str, tmp_path
    ):
        await _register_and_login(client, unique_email)
        project_id = await _create_project(client)
        content = b"ISO-10303-21;"

        with patch("app.config.settings.STORAGE_PROVIDER", "local"), \
             patch("app.config.settings.LOCAL_STORAGE_PATH", str(tmp_path)), \
             patch("app.services.storage.trigger_clamav_scan") as mock_scan, \
             patch("app.services.models.dispatch_event", new_callable=AsyncMock), \
             patch("app.services.models.publish_model_event", new_callable=AsyncMock):

            data = await _initiate_upload(client, project_id, size_bytes=len(content))
            await _upload_local_file(client, data["storage_key"], content)

            resp = await client.post(f"/v1/models/{data['model_id']}/confirm")
            assert resp.status_code == 200, resp.text
            body = resp.json()["data"]
            assert body["status"] == "processing"

            # trigger_clamav_scan gates processing — scan task dispatches ifc/mesh
            # _enqueue_processing_task is a no-op stub, never called in production
            mock_scan.assert_called_once()

    async def test_confirm_fails_on_missing_file(
        self, client: AsyncClient, unique_email: str, tmp_path
    ):
        await _register_and_login(client, unique_email)
        project_id = await _create_project(client)

        with patch("app.config.settings.STORAGE_PROVIDER", "local"), \
             patch("app.config.settings.LOCAL_STORAGE_PATH", str(tmp_path)):
            data = await _initiate_upload(client, project_id)

            resp = await client.post(f"/v1/models/{data['model_id']}/confirm")
            # StorageException (file not found in local/S3 storage) returns 502
            assert resp.status_code in (400, 422, 500, 502)

    async def test_confirm_idempotency_rejects_double_confirm(
        self, client: AsyncClient, unique_email: str, tmp_path
    ):
        await _register_and_login(client, unique_email)
        project_id = await _create_project(client)
        content = b"ISO-10303-21;"

        with patch("app.config.settings.STORAGE_PROVIDER", "local"), \
             patch("app.config.settings.LOCAL_STORAGE_PATH", str(tmp_path)), \
             patch("app.services.storage.trigger_clamav_scan"), \
             patch("app.services.models.dispatch_event", new_callable=AsyncMock), \
             patch("app.services.models.publish_model_event", new_callable=AsyncMock):

            data = await _initiate_upload(client, project_id, size_bytes=len(content))
            await _upload_local_file(client, data["storage_key"], content)

            r1 = await client.post(f"/v1/models/{data['model_id']}/confirm")
            assert r1.status_code == 200

            r2 = await client.post(f"/v1/models/{data['model_id']}/confirm")
            assert r2.status_code == 422


# ---------------------------------------------------------------------------
# S3 mode (mocked boto3)
# ---------------------------------------------------------------------------

class TestS3Mode:
    async def test_presigned_url_returned_in_s3_mode(
        self, client: AsyncClient, unique_email: str
    ):
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

    async def test_local_upload_endpoint_404_in_s3_mode(
        self, client: AsyncClient, unique_email: str
    ):
        await _register_and_login(client, unique_email)
        with patch("app.config.settings.STORAGE_PROVIDER", "s3"):
            resp = await client.post(
                "/v1/models/upload/local?storage_key=a/b/c.ifc",
                files={"file": ("c.ifc", b"data", "application/octet-stream")},
            )
        assert resp.status_code == 404