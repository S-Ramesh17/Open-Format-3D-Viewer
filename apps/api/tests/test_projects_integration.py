"""
Integration tests for project CRUD endpoints.

Covers: create, list, update, delete, authorization enforcement.
All tests run against the real FastAPI ASGI app via httpx AsyncClient.
"""

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _register_and_login(client: AsyncClient, email: str) -> AsyncClient:
    """Register + login a user; returns a client with auth cookies set."""
    await client.post(
        "/v1/auth/register",
        json={"email": email, "password": "testpass123", "full_name": "Test User"},
    )
    return client


# ---------------------------------------------------------------------------
# Create project
# ---------------------------------------------------------------------------

class TestCreateProject:
    async def test_create_happy_path(self, client: AsyncClient, unique_email: str):
        await _register_and_login(client, unique_email)
        resp = await client.post(
            "/v1/projects",
            json={"name": "My Project", "description": "A test project"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["data"]["name"] == "My Project"
        assert body["data"]["description"] == "A test project"
        assert body["data"]["role"] == "admin"
        assert "id" in body["data"]
        assert "request_id" in body["meta"]

    async def test_create_without_description(self, client: AsyncClient, unique_email: str):
        await _register_and_login(client, unique_email)
        resp = await client.post("/v1/projects", json={"name": "Minimal Project"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["data"]["name"] == "Minimal Project"

    async def test_create_requires_auth(self, client: AsyncClient):
        resp = await client.post("/v1/projects", json={"name": "Unauthorized"})
        assert resp.status_code == 401

    async def test_create_missing_name_fails(self, client: AsyncClient, unique_email: str):
        await _register_and_login(client, unique_email)
        resp = await client.post("/v1/projects", json={"description": "No name"})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# List projects
# ---------------------------------------------------------------------------

class TestListProjects:
    async def test_list_returns_own_projects(self, client: AsyncClient, unique_email: str):
        await _register_and_login(client, unique_email)
        await client.post("/v1/projects", json={"name": "Project Alpha"})
        await client.post("/v1/projects", json={"name": "Project Beta"})

        resp = await client.get("/v1/projects")
        assert resp.status_code == 200
        body = resp.json()
        names = [p["name"] for p in body["data"]]
        assert "Project Alpha" in names
        assert "Project Beta" in names

    async def test_list_requires_auth(self, client: AsyncClient):
        resp = await client.get("/v1/projects")
        assert resp.status_code == 401

    async def test_list_pagination_cursor(self, client: AsyncClient, unique_email: str):
        await _register_and_login(client, unique_email)
        # Create 3 projects
        for i in range(3):
            await client.post("/v1/projects", json={"name": f"Paginated {i}"})

        resp = await client.get("/v1/projects?limit=2")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["data"]) <= 2

    async def test_list_no_cross_user_leakage(self, client: AsyncClient, unique_email: str):
        """Projects created by user A should not appear in user B's list."""
        email_a = unique_email
        email_b = f"b_{unique_email}"

        await _register_and_login(client, email_a)
        await client.post("/v1/projects", json={"name": "User A Only"})

        # Log out / switch to user B (new client via re-register)
        await client.post(
            "/v1/auth/register",
            json={"email": email_b, "password": "testpass123"},
        )
        await client.post(
            "/v1/auth/login",
            json={"email": email_b, "password": "testpass123"},
        )

        resp = await client.get("/v1/projects")
        assert resp.status_code == 200
        names = [p["name"] for p in resp.json()["data"]]
        assert "User A Only" not in names

    async def test_list_empty_for_new_user(self, client: AsyncClient, unique_email: str):
        await _register_and_login(client, unique_email)
        resp = await client.get("/v1/projects")
        assert resp.status_code == 200
        assert resp.json()["data"] == []


# ---------------------------------------------------------------------------
# Update project
# ---------------------------------------------------------------------------

class TestUpdateProject:
    async def test_update_name_and_description(self, client: AsyncClient, unique_email: str):
        await _register_and_login(client, unique_email)
        create_resp = await client.post(
            "/v1/projects", json={"name": "Original Name"}
        )
        project_id = create_resp.json()["data"]["id"]

        resp = await client.patch(
            f"/v1/projects/{project_id}",
            json={"name": "Updated Name", "description": "New desc"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["name"] == "Updated Name"
        assert body["data"]["description"] == "New desc"

    async def test_update_requires_auth(self, client: AsyncClient):
        import uuid
        resp = await client.patch(
            f"/v1/projects/{uuid.uuid4()}", json={"name": "X"}
        )
        assert resp.status_code == 401

    async def test_update_nonexistent_project_returns_404(
        self, client: AsyncClient, unique_email: str
    ):
        await _register_and_login(client, unique_email)
        import uuid
        resp = await client.patch(
            f"/v1/projects/{uuid.uuid4()}", json={"name": "Ghost"}
        )
        assert resp.status_code in (403, 404)  # not a member → 403; nonexistent → 404


# ---------------------------------------------------------------------------
# Delete project
# ---------------------------------------------------------------------------

class TestDeleteProject:
    async def test_delete_happy_path(self, client: AsyncClient, unique_email: str):
        await _register_and_login(client, unique_email)
        create_resp = await client.post("/v1/projects", json={"name": "To Delete"})
        project_id = create_resp.json()["data"]["id"]

        del_resp = await client.delete(f"/v1/projects/{project_id}")
        assert del_resp.status_code == 204

        # Verify it's gone
        get_resp = await client.get(f"/v1/projects/{project_id}")
        assert get_resp.status_code == 404

    async def test_delete_requires_auth(self, client: AsyncClient):
        import uuid
        resp = await client.delete(f"/v1/projects/{uuid.uuid4()}")
        assert resp.status_code == 401

    async def test_delete_nonexistent_returns_error(
        self, client: AsyncClient, unique_email: str
    ):
        await _register_and_login(client, unique_email)
        import uuid
        resp = await client.delete(f"/v1/projects/{uuid.uuid4()}")
        assert resp.status_code in (403, 404)


# ---------------------------------------------------------------------------
# Authorization enforcement
# ---------------------------------------------------------------------------

class TestProjectAuthorization:
    async def test_get_project_requires_membership(self, client: AsyncClient, unique_email: str):
        """User A creates a project; User B cannot fetch it."""
        email_a = unique_email
        email_b = f"other_{unique_email}"

        await _register_and_login(client, email_a)
        create_resp = await client.post("/v1/projects", json={"name": "Private"})
        project_id = create_resp.json()["data"]["id"]

        # Switch to user B
        await client.post(
            "/v1/auth/register",
            json={"email": email_b, "password": "testpass123"},
        )
        await client.post(
            "/v1/auth/login",
            json={"email": email_b, "password": "testpass123"},
        )

        resp = await client.get(f"/v1/projects/{project_id}")
        assert resp.status_code == 404  # membership enforced at query level
