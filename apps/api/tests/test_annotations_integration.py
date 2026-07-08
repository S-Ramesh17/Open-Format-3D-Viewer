"""
Integration tests for annotation CRUD and comment endpoints.

Covers: create, update, comment, filtering by status, authorization.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _setup_user_project_model(client: AsyncClient, email: str) -> tuple[str, str]:
    """Register, login, create project + pending model. Returns (project_id, model_id)."""
    await client.post(
        "/v1/auth/register",
        json={"email": email, "password": "testpass123", "full_name": "Test User"},
    )
    proj_resp = await client.post("/v1/projects", json={"name": "Annotation Test"})
    project_id = proj_resp.json()["data"]["id"]

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
    return project_id, model_id


VALID_ANNOTATION = {
    "title": "Crack in column",
    "body": "Visible crack near grid A3",
    "position": {"x": 1.0, "y": 2.0, "z": 3.0, "normal_x": 0.0, "normal_y": 1.0, "normal_z": 0.0},
}


# ---------------------------------------------------------------------------
# Create annotation
# ---------------------------------------------------------------------------

class TestCreateAnnotation:
    async def test_create_happy_path(self, client: AsyncClient, unique_email: str):
        _, model_id = await _setup_user_project_model(client, unique_email)

        with patch("app.services.annotations.publish_model_event", new_callable=AsyncMock), \
             patch("app.services.annotations.dispatch_event", new_callable=AsyncMock):
            resp = await client.post(
                f"/v1/models/{model_id}/annotations",
                json=VALID_ANNOTATION,
            )

        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["title"] == "Crack in column"
        assert data["status"] == "open"
        assert "id" in data

    async def test_create_requires_auth(self, client: AsyncClient):
        resp = await client.post(
            f"/v1/models/{uuid.uuid4()}/annotations",
            json=VALID_ANNOTATION,
        )
        assert resp.status_code == 401

    async def test_create_strips_html_from_title(self, client: AsyncClient, unique_email: str):
        _, model_id = await _setup_user_project_model(client, unique_email)

        with patch("app.services.annotations.publish_model_event", new_callable=AsyncMock), \
             patch("app.services.annotations.dispatch_event", new_callable=AsyncMock):
            resp = await client.post(
                f"/v1/models/{model_id}/annotations",
                json={**VALID_ANNOTATION, "title": "<script>alert(1)</script>Crack"},
            )

        assert resp.status_code == 201
        # bleach should have stripped the script tag
        title = resp.json()["data"]["title"]
        assert "<script>" not in title
        assert "Crack" in title

    async def test_create_missing_position_fails(self, client: AsyncClient, unique_email: str):
        _, model_id = await _setup_user_project_model(client, unique_email)
        resp = await client.post(
            f"/v1/models/{model_id}/annotations",
            json={"title": "No position"},
        )
        assert resp.status_code == 422

    async def test_create_nonexistent_model_returns_404(
        self, client: AsyncClient, unique_email: str
    ):
        await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123"},
        )
        resp = await client.post(
            f"/v1/models/{uuid.uuid4()}/annotations",
            json=VALID_ANNOTATION,
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Update annotation
# ---------------------------------------------------------------------------

class TestUpdateAnnotation:
    async def _create_annotation(self, client: AsyncClient, model_id: str) -> str:
        with patch("app.services.annotations.publish_model_event", new_callable=AsyncMock), \
             patch("app.services.annotations.dispatch_event", new_callable=AsyncMock):
            resp = await client.post(
                f"/v1/models/{model_id}/annotations",
                json=VALID_ANNOTATION,
            )
        return resp.json()["data"]["id"]

    async def test_update_title(self, client: AsyncClient, unique_email: str):
        _, model_id = await _setup_user_project_model(client, unique_email)
        ann_id = await self._create_annotation(client, model_id)

        with patch("app.services.annotations.publish_model_event", new_callable=AsyncMock), \
             patch("app.services.annotations.dispatch_event", new_callable=AsyncMock):
            resp = await client.patch(
                f"/v1/annotations/{ann_id}",
                json={"title": "Updated Title"},
            )

        assert resp.status_code == 200
        assert resp.json()["data"]["title"] == "Updated Title"

    async def test_update_status_to_resolved(self, client: AsyncClient, unique_email: str):
        _, model_id = await _setup_user_project_model(client, unique_email)
        ann_id = await self._create_annotation(client, model_id)

        with patch("app.services.annotations.publish_model_event", new_callable=AsyncMock), \
             patch("app.services.annotations.dispatch_event", new_callable=AsyncMock):
            resp = await client.patch(
                f"/v1/annotations/{ann_id}",
                json={"status": "resolved"},
            )

        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "resolved"

    async def test_update_invalid_status_fails(self, client: AsyncClient, unique_email: str):
        _, model_id = await _setup_user_project_model(client, unique_email)
        ann_id = await self._create_annotation(client, model_id)

        resp = await client.patch(
            f"/v1/annotations/{ann_id}",
            json={"status": "invalid_status"},
        )
        assert resp.status_code == 422

    async def test_update_requires_auth(self, client: AsyncClient):
        resp = await client.patch(
            f"/v1/annotations/{uuid.uuid4()}",
            json={"title": "X"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------

class TestAnnotationComments:
    async def _create_annotation(self, client: AsyncClient, model_id: str) -> str:
        with patch("app.services.annotations.publish_model_event", new_callable=AsyncMock), \
             patch("app.services.annotations.dispatch_event", new_callable=AsyncMock):
            resp = await client.post(
                f"/v1/models/{model_id}/annotations",
                json=VALID_ANNOTATION,
            )
        return resp.json()["data"]["id"]

    async def test_add_comment_happy_path(self, client: AsyncClient, unique_email: str):
        _, model_id = await _setup_user_project_model(client, unique_email)
        ann_id = await self._create_annotation(client, model_id)

        with patch("app.services.annotations.publish_model_event", new_callable=AsyncMock), \
             patch("app.services.annotations.dispatch_event", new_callable=AsyncMock):
            resp = await client.post(
                f"/v1/annotations/{ann_id}/comments",
                json={"body": "This needs immediate attention"},
            )

        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["body"] == "This needs immediate attention"
        assert "id" in data

    async def test_add_empty_comment_fails(self, client: AsyncClient, unique_email: str):
        _, model_id = await _setup_user_project_model(client, unique_email)
        ann_id = await self._create_annotation(client, model_id)

        resp = await client.post(
            f"/v1/annotations/{ann_id}/comments",
            json={"body": "   "},
        )
        assert resp.status_code == 422

    async def test_comment_strips_html(self, client: AsyncClient, unique_email: str):
        _, model_id = await _setup_user_project_model(client, unique_email)
        ann_id = await self._create_annotation(client, model_id)

        with patch("app.services.annotations.publish_model_event", new_callable=AsyncMock), \
             patch("app.services.annotations.dispatch_event", new_callable=AsyncMock):
            resp = await client.post(
                f"/v1/annotations/{ann_id}/comments",
                json={"body": "<b>Bold</b> and <script>xss</script>"},
            )

        assert resp.status_code == 201
        body = resp.json()["data"]["body"]
        assert "<script>" not in body
        assert "Bold" in body

    async def test_comment_requires_auth(self, client: AsyncClient):
        resp = await client.post(
            f"/v1/annotations/{uuid.uuid4()}/comments",
            json={"body": "Hello"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Filtering annotations
# ---------------------------------------------------------------------------

class TestAnnotationFiltering:
    async def test_filter_by_status_open(self, client: AsyncClient, unique_email: str):
        _, model_id = await _setup_user_project_model(client, unique_email)

        with patch("app.services.annotations.publish_model_event", new_callable=AsyncMock), \
             patch("app.services.annotations.dispatch_event", new_callable=AsyncMock):
            # Create two annotations
            await client.post(
                f"/v1/models/{model_id}/annotations", json=VALID_ANNOTATION
            )
            resp2 = await client.post(
                f"/v1/models/{model_id}/annotations",
                json={**VALID_ANNOTATION, "title": "Second issue"},
            )
            ann_id = resp2.json()["data"]["id"]

            # Resolve one
            with patch("app.services.annotations.publish_model_event", new_callable=AsyncMock), \
                 patch("app.services.annotations.dispatch_event", new_callable=AsyncMock):
                await client.patch(f"/v1/annotations/{ann_id}", json={"status": "resolved"})

        resp = await client.get(f"/v1/models/{model_id}/annotations?status=open")
        assert resp.status_code == 200
        statuses = [a["status"] for a in resp.json()["data"]]
        assert all(s == "open" for s in statuses)

    async def test_filter_by_status_resolved(self, client: AsyncClient, unique_email: str):
        _, model_id = await _setup_user_project_model(client, unique_email)

        with patch("app.services.annotations.publish_model_event", new_callable=AsyncMock), \
             patch("app.services.annotations.dispatch_event", new_callable=AsyncMock):
            r = await client.post(
                f"/v1/models/{model_id}/annotations", json=VALID_ANNOTATION
            )
            ann_id = r.json()["data"]["id"]

        with patch("app.services.annotations.publish_model_event", new_callable=AsyncMock), \
             patch("app.services.annotations.dispatch_event", new_callable=AsyncMock):
            await client.patch(f"/v1/annotations/{ann_id}", json={"status": "resolved"})

        resp = await client.get(f"/v1/models/{model_id}/annotations?status=resolved")
        assert resp.status_code == 200
        statuses = [a["status"] for a in resp.json()["data"]]
        assert all(s == "resolved" for s in statuses)


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

class TestAnnotationPagination:
    async def test_list_returns_paginated_envelope(
        self, client: AsyncClient, unique_email: str
    ):
        """List endpoint returns meta.next_cursor like projects/models/elements."""
        project_id, model_id = await _setup_user_project_model(client, unique_email)

        with patch("app.services.annotations.publish_model_event", new_callable=AsyncMock), \
             patch("app.services.annotations.dispatch_event", new_callable=AsyncMock):
            await client.post(f"/v1/models/{model_id}/annotations", json=VALID_ANNOTATION)

        resp = await client.get(f"/v1/models/{model_id}/annotations")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        assert "meta" in body
        assert "request_id" in body["meta"]
        assert "next_cursor" in body["meta"]

    async def test_list_respects_limit(self, client: AsyncClient, unique_email: str):
        project_id, model_id = await _setup_user_project_model(client, unique_email)

        with patch("app.services.annotations.publish_model_event", new_callable=AsyncMock), \
             patch("app.services.annotations.dispatch_event", new_callable=AsyncMock):
            for i in range(3):
                await client.post(
                    f"/v1/models/{model_id}/annotations",
                    json={**VALID_ANNOTATION, "title": f"Annotation {i}"},
                )

        resp = await client.get(f"/v1/models/{model_id}/annotations?limit=2")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["data"]) == 2
        assert body["meta"]["next_cursor"] is not None

    async def test_list_cursor_pagination_no_duplicates(
        self, client: AsyncClient, unique_email: str
    ):
        """Paging through with cursor returns all items exactly once."""
        project_id, model_id = await _setup_user_project_model(client, unique_email)

        with patch("app.services.annotations.publish_model_event", new_callable=AsyncMock), \
             patch("app.services.annotations.dispatch_event", new_callable=AsyncMock):
            for i in range(5):
                await client.post(
                    f"/v1/models/{model_id}/annotations",
                    json={**VALID_ANNOTATION, "title": f"Annotation {i}"},
                )

        seen_ids = set()
        cursor = None
        for _ in range(10):  # safety bound
            url = f"/v1/models/{model_id}/annotations?limit=2"
            if cursor:
                url += f"&cursor={cursor}"
            resp = await client.get(url)
            body = resp.json()
            for item in body["data"]:
                assert item["id"] not in seen_ids, "duplicate item across pages"
                seen_ids.add(item["id"])
            cursor = body["meta"]["next_cursor"]
            if cursor is None:
                break

        assert len(seen_ids) == 5

    async def test_list_no_cursor_returns_first_page(
        self, client: AsyncClient, unique_email: str
    ):
        project_id, model_id = await _setup_user_project_model(client, unique_email)
        resp = await client.get(f"/v1/models/{model_id}/annotations")
        assert resp.status_code == 200
        assert resp.json()["data"] == []
        assert resp.json()["meta"]["next_cursor"] is None
