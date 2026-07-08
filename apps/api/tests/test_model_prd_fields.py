"""
Tests for PRD-required model fields: name, element_count, bounds_min_xyz, bounds_max_xyz.
"""

import uuid
from unittest.mock import patch

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def _setup_user_and_project(client: AsyncClient, email: str) -> str:
    await client.post(
        "/v1/auth/register",
        json={"email": email, "password": "testpass123"},
    )
    resp = await client.post("/v1/projects", json={"name": "PRD Fields Test"})
    return resp.json()["data"]["id"]


class TestModelNameField:
    async def test_name_defaults_to_filename(self, client: AsyncClient, unique_email: str):
        with patch("app.services.models.generate_presigned_upload_url") as m:
            m.return_value = "https://s3.example.com/url"
            project_id = await _setup_user_and_project(client, unique_email)
            resp = await client.post(
                "/v1/models/upload",
                json={
                    "project_id": project_id,
                    "filename": "tower.ifc",
                    "content_type": "application/octet-stream",
                    "size_bytes": 1024,
                },
            )
        model_id = resp.json()["data"]["model_id"]
        get_resp = await client.get(f"/v1/models/{model_id}")
        assert get_resp.json()["data"]["name"] == "tower.ifc"

    async def test_explicit_name_overrides_filename(self, client: AsyncClient, unique_email: str):
        with patch("app.services.models.generate_presigned_upload_url") as m:
            m.return_value = "https://s3.example.com/url"
            project_id = await _setup_user_and_project(client, unique_email)
            resp = await client.post(
                "/v1/models/upload",
                json={
                    "project_id": project_id,
                    "filename": "tower.ifc",
                    "name": "Main Tower — Structural",
                    "content_type": "application/octet-stream",
                    "size_bytes": 1024,
                },
            )
        model_id = resp.json()["data"]["model_id"]
        get_resp = await client.get(f"/v1/models/{model_id}")
        assert get_resp.json()["data"]["name"] == "Main Tower — Structural"


class TestModelPrdFieldsDefaultNull:
    async def test_element_count_and_bounds_null_before_processing(
        self, client: AsyncClient, unique_email: str
    ):
        with patch("app.services.models.generate_presigned_upload_url") as m:
            m.return_value = "https://s3.example.com/url"
            project_id = await _setup_user_and_project(client, unique_email)
            resp = await client.post(
                "/v1/models/upload",
                json={
                    "project_id": project_id,
                    "filename": "x.ifc",
                    "content_type": "application/octet-stream",
                    "size_bytes": 1024,
                },
            )
        model_id = resp.json()["data"]["model_id"]
        get_resp = await client.get(f"/v1/models/{model_id}")
        data = get_resp.json()["data"]
        assert data["element_count"] is None
        assert data["bounds_min_xyz"] is None
        assert data["bounds_max_xyz"] is None