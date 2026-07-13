"""
Integration tests for project CRUD endpoints.

Covers: create, list, update, delete, authorization enforcement.
All tests run against the real FastAPI ASGI app via httpx AsyncClient.
"""

import pytest
from httpx import ASGITransport, AsyncClient
import uuid
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


async def _register_only(email: str) -> None:
    """
    Registers a user via a throwaway, independent AsyncClient so the
    caller's own `client` fixture session/cookies are left untouched.
    Used for setting up a second user (e.g. an invitee) while staying
    logged in as the first.
    """
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as ac:
        await ac.post(
            "/v1/auth/register",
            json={"email": email, "password": "testpass123", "full_name": "Invited User"},
        )


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
        assert resp.status_code == 400


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


# ---------------------------------------------------------------------------
# Project members
# ---------------------------------------------------------------------------

class TestProjectMembers:
    async def test_list_members_returns_owner(self, client: AsyncClient, unique_email: str):
        await _register_and_login(client, unique_email)
        create_resp = await client.post("/v1/projects", json={"name": "Team Project"})
        project_id = create_resp.json()["data"]["id"]

        resp = await client.get(f"/v1/projects/{project_id}/members")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body["data"], list)
        assert len(body["data"]) == 1
        assert body["data"][0]["role"] == "admin"
        assert "user_id" in body["data"][0]

    async def test_list_members_requires_auth(self, client: AsyncClient):
        resp = await client.get(f"/v1/projects/{uuid.uuid4()}/members")
        assert resp.status_code == 401

    async def test_list_members_requires_membership(self, client: AsyncClient, unique_email: str):
        """User B cannot view User A's project members."""
        email_a = unique_email
        email_b = f"other_{unique_email}"

        await _register_and_login(client, email_a)
        create_resp = await client.post("/v1/projects", json={"name": "Private Team"})
        project_id = create_resp.json()["data"]["id"]

        await client.post(
            "/v1/auth/register",
            json={"email": email_b, "password": "testpass123"},
        )
        await client.post(
            "/v1/auth/login",
            json={"email": email_b, "password": "testpass123"},
        )

        resp = await client.get(f"/v1/projects/{project_id}/members")
        assert resp.status_code in (403, 404)

    async def test_list_members_envelope_shape(self, client: AsyncClient, unique_email: str):
        """Members endpoint uses standard envelope (data + meta.request_id)."""
        await _register_and_login(client, unique_email)
        create_resp = await client.post("/v1/projects", json={"name": "Envelope Check"})
        project_id = create_resp.json()["data"]["id"]

        resp = await client.get(f"/v1/projects/{project_id}/members")
        body = resp.json()
        assert "data" in body
        assert "meta" in body
        assert "request_id" in body["meta"]

    # -- Invite member ---------------------------------------------------

    async def test_invite_member_happy_path(self, client: AsyncClient, unique_email: str):
        email_b = f"invitee_{unique_email}"
        await _register_only(email_b)

        await _register_and_login(client, unique_email)
        create_resp = await client.post("/v1/projects", json={"name": "Invite Team"})
        project_id = create_resp.json()["data"]["id"]

        resp = await client.post(
            f"/v1/projects/{project_id}/members",
            json={"email": email_b, "role": "editor"},
        )
        assert resp.status_code == 201
        assert resp.json()["data"]["role"] == "editor"

        list_resp = await client.get(f"/v1/projects/{project_id}/members")
        assert len(list_resp.json()["data"]) == 2

    async def test_invite_member_unknown_email_404(self, client: AsyncClient, unique_email: str):
        await _register_and_login(client, unique_email)
        create_resp = await client.post("/v1/projects", json={"name": "Invite 404"})
        project_id = create_resp.json()["data"]["id"]

        resp = await client.post(
            f"/v1/projects/{project_id}/members",
            json={"email": f"nobody_{unique_email}", "role": "viewer"},
        )
        assert resp.status_code == 404

    async def test_invite_member_already_a_member_409(self, client: AsyncClient, unique_email: str):
        email_b = f"dupe_{unique_email}"
        await _register_only(email_b)

        await _register_and_login(client, unique_email)
        create_resp = await client.post("/v1/projects", json={"name": "Invite Dupe"})
        project_id = create_resp.json()["data"]["id"]

        await client.post(
            f"/v1/projects/{project_id}/members", json={"email": email_b, "role": "viewer"}
        )
        resp = await client.post(
            f"/v1/projects/{project_id}/members", json={"email": email_b, "role": "viewer"}
        )
        assert resp.status_code == 409

    async def test_invite_member_requires_admin(self, client: AsyncClient, unique_email: str):
        email_owner = unique_email
        email_viewer = f"viewer_{unique_email}"
        email_target = f"target_{unique_email}"
        await _register_only(email_viewer)
        await _register_only(email_target)

        await _register_and_login(client, email_owner)
        create_resp = await client.post("/v1/projects", json={"name": "Invite Perms"})
        project_id = create_resp.json()["data"]["id"]
        await client.post(
            f"/v1/projects/{project_id}/members",
            json={"email": email_viewer, "role": "viewer"},
        )

        await client.post("/v1/auth/login", json={"email": email_viewer, "password": "testpass123"})
        resp = await client.post(
            f"/v1/projects/{project_id}/members",
            json={"email": email_target, "role": "viewer"},
        )
        assert resp.status_code == 403

    # -- Update member role ------------------------------------------------

    async def test_update_member_role_happy_path(self, client: AsyncClient, unique_email: str):
        email_b = f"promote_{unique_email}"
        await _register_only(email_b)

        await _register_and_login(client, unique_email)
        create_resp = await client.post("/v1/projects", json={"name": "Promote Team"})
        project_id = create_resp.json()["data"]["id"]
        invite_resp = await client.post(
            f"/v1/projects/{project_id}/members", json={"email": email_b, "role": "viewer"}
        )
        user_id = invite_resp.json()["data"]["user_id"]

        resp = await client.patch(
            f"/v1/projects/{project_id}/members/{user_id}", json={"role": "editor"}
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["role"] == "editor"

    async def test_demote_last_admin_409(self, client: AsyncClient, unique_email: str):
        await _register_and_login(client, unique_email)
        create_resp = await client.post("/v1/projects", json={"name": "Sole Admin"})
        project_id = create_resp.json()["data"]["id"]

        members = (await client.get(f"/v1/projects/{project_id}/members")).json()["data"]
        owner_user_id = members[0]["user_id"]

        resp = await client.patch(
            f"/v1/projects/{project_id}/members/{owner_user_id}", json={"role": "viewer"}
        )
        assert resp.status_code == 409

    # -- Remove member -------------------------------------------------------

    async def test_remove_member_happy_path(self, client: AsyncClient, unique_email: str):
        email_b = f"removeme_{unique_email}"
        await _register_only(email_b)

        await _register_and_login(client, unique_email)
        create_resp = await client.post("/v1/projects", json={"name": "Remove Team"})
        project_id = create_resp.json()["data"]["id"]
        invite_resp = await client.post(
            f"/v1/projects/{project_id}/members", json={"email": email_b, "role": "viewer"}
        )
        user_id = invite_resp.json()["data"]["user_id"]

        resp = await client.delete(f"/v1/projects/{project_id}/members/{user_id}")
        assert resp.status_code == 204

        list_resp = await client.get(f"/v1/projects/{project_id}/members")
        assert len(list_resp.json()["data"]) == 1

    async def test_remove_owner_409(self, client: AsyncClient, unique_email: str):
        await _register_and_login(client, unique_email)
        create_resp = await client.post("/v1/projects", json={"name": "Owner Protect"})
        project_id = create_resp.json()["data"]["id"]

        members = (await client.get(f"/v1/projects/{project_id}/members")).json()["data"]
        owner_user_id = members[0]["user_id"]

        resp = await client.delete(f"/v1/projects/{project_id}/members/{owner_user_id}")
        assert resp.status_code == 409

    async def test_remove_member_requires_admin(self, client: AsyncClient, unique_email: str):
        email_owner = unique_email
        email_viewer = f"vwr_{unique_email}"
        await _register_only(email_viewer)

        await _register_and_login(client, email_owner)
        create_resp = await client.post("/v1/projects", json={"name": "Remove Perms"})
        project_id = create_resp.json()["data"]["id"]
        invite_resp = await client.post(
            f"/v1/projects/{project_id}/members",
            json={"email": email_viewer, "role": "viewer"},
        )
        viewer_user_id = invite_resp.json()["data"]["user_id"]

        await client.post("/v1/auth/login", json={"email": email_viewer, "password": "testpass123"})
        resp = await client.delete(f"/v1/projects/{project_id}/members/{viewer_user_id}")
        assert resp.status_code == 403