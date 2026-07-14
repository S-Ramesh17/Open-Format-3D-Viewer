"""
Integration tests for the local-storage direct upload endpoint.

Covers: POST /v1/models/upload/local
  - happy path (full 3-step local workflow: upload → upload/local → confirm)
  - 404 when STORAGE_PROVIDER != "local"
  - path traversal rejection
  - empty file rejection
  - auth requirement
"""

import os
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def _setup_user_and_project(client: AsyncClient, email: str) -> str:
    await client.post(
        "/v1/auth/register",
        json={"email": email, "password": "testpass123", "name": "Test User"},
    )
    resp = await client.post("/v1/projects", json={"name": "Local Upload Test"})
    return resp.json()["data"]["id"]


class TestLocalUploadEndpoint:
    async def test_requires_auth(self, client: AsyncClient):
        resp = await client.post(
            "/v1/models/upload/local?storage_key=a/b/c.ifc",
            files={"file": ("c.ifc", b"ISO-10303-21;", "application/octet-stream")},
        )
        assert resp.status_code == 401

    async def test_returns_404_when_not_local_mode(
        self, client: AsyncClient, unique_email: str, tmp_path
    ):
        await _setup_user_and_project(client, unique_email)
        with patch("app.config.settings.STORAGE_PROVIDER", "s3"):
            resp = await client.post(
                "/v1/models/upload/local?storage_key=a/b/c.ifc",
                files={"file": ("c.ifc", b"ISO-10303-21;", "application/octet-stream")},
            )
        assert resp.status_code == 404

    async def test_rejects_path_traversal(
        self, client: AsyncClient, unique_email: str, tmp_path
    ):
        await _setup_user_and_project(client, unique_email)
        with patch("app.config.settings.STORAGE_PROVIDER", "local"), \
             patch("app.config.settings.LOCAL_STORAGE_PATH", str(tmp_path)):
            resp = await client.post(
                "/v1/models/upload/local?storage_key=../../etc/passwd",
                files={"file": ("x", b"data", "application/octet-stream")},
            )
        assert resp.status_code == 400

    async def test_rejects_empty_file(
        self, client: AsyncClient, unique_email: str, tmp_path
    ):
        await _setup_user_and_project(client, unique_email)
        with patch("app.config.settings.STORAGE_PROVIDER", "local"), \
             patch("app.config.settings.LOCAL_STORAGE_PATH", str(tmp_path)):
            resp = await client.post(
                "/v1/models/upload/local?storage_key=u/m/empty.ifc",
                files={"file": ("empty.ifc", b"", "application/octet-stream")},
            )
        assert resp.status_code == 400

    async def test_full_local_workflow_writes_file_to_disk(
        self, client: AsyncClient, unique_email: str, tmp_path
    ):
        """
        End-to-end: POST /upload → POST /upload/local → file exists at
        LOCAL_STORAGE_PATH/raw/{storage_key} with correct bytes.
        """
        project_id = await _setup_user_and_project(client, unique_email)

        with patch("app.config.settings.STORAGE_PROVIDER", "local"), \
             patch("app.config.settings.LOCAL_STORAGE_PATH", str(tmp_path)):

            upload_resp = await client.post(
                "/v1/models/upload",
                json={
                    "project_id": project_id,
                    "filename": "test.ifc",
                    "content_type": "application/octet-stream",
                    "size_bytes": 14,
                },
            )
            assert upload_resp.status_code == 201
            storage_key = upload_resp.json()["data"]["storage_key"]
            assert upload_resp.json()["data"]["upload_url"] == f"local://{storage_key}"

            file_bytes = b"ISO-10303-21;"
            local_resp = await client.post(
                f"/v1/models/upload/local?storage_key={storage_key}",
                files={"file": ("test.ifc", file_bytes, "application/octet-stream")},
            )
            assert local_resp.status_code == 200
            body = local_resp.json()["data"]
            assert body["status"] == "stored"
            assert body["size_bytes"] == len(file_bytes)

            expected_path = os.path.join(str(tmp_path), "raw", storage_key)
            assert os.path.exists(expected_path)
            with open(expected_path, "rb") as f:
                assert f.read() == file_bytes

    async def test_confirm_succeeds_after_local_upload(
        self, client: AsyncClient, unique_email: str, tmp_path
    ):
        """Full 3-step flow: confirm should succeed once the file is on disk."""
        project_id = await _setup_user_and_project(client, unique_email)

        with patch("app.config.settings.STORAGE_PROVIDER", "local"), \
             patch("app.config.settings.LOCAL_STORAGE_PATH", str(tmp_path)):

            file_bytes = b"ISO-10303-21;HEADER;FILE_DESCRIPTION" + b"\x00" * 50
            upload_resp = await client.post(
                "/v1/models/upload",
                json={
                    "project_id": project_id,
                    "filename": "test.ifc",
                    "content_type": "application/octet-stream",
                    "size_bytes": len(file_bytes),
                },
            )
            model_id = upload_resp.json()["data"]["model_id"]
            storage_key = upload_resp.json()["data"]["storage_key"]

            await client.post(
                f"/v1/models/upload/local?storage_key={storage_key}",
                files={"file": ("test.ifc", file_bytes, "application/octet-stream")},
            )

            with patch("app.services.storage.trigger_clamav_scan"), \
                 patch("app.core.redis.publish_model_event", new_callable=AsyncMock), \
                 patch("app.services.webhooks.dispatch_event", new_callable=AsyncMock):
                confirm_resp = await client.post(f"/v1/models/{model_id}/confirm")

            assert confirm_resp.status_code == 200
            assert confirm_resp.json()["data"]["status"] == "processing"