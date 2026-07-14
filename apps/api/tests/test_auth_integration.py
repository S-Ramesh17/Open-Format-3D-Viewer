"""
Integration tests for authentication endpoints.

Covers: register, login, logout, refresh, /me, API key CRUD
Uses the actual response envelope: {"data": {"user": {...}, "access_token": "...", ...}, "meta": {...}}
"""
import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


class TestRegister:
    async def test_register_happy_path(self, client: AsyncClient, unique_email: str):
        resp = await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123", "full_name": "Test User"},
        )
        assert resp.status_code == 201
        body = resp.json()
        # AuthResponse: data.user.email, data.access_token
        assert body["data"]["user"]["email"] == unique_email
        assert "access_token" in body["data"]
        assert "request_id" in body["meta"]
        assert "access_token" in resp.cookies

    async def test_register_duplicate_email_fails(self, client: AsyncClient, unique_email: str):
        await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123"},
        )
        resp = await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123"},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "CONFLICT"

    async def test_register_short_password_fails(self, client: AsyncClient, unique_email: str):
        resp = await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "short"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    async def test_register_all_digits_password_fails(self, client: AsyncClient, unique_email: str):
        resp = await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "12345678"},
        )
        assert resp.status_code == 400

    async def test_register_email_normalized_to_lowercase(self, client: AsyncClient, unique_email: str):
        upper_email = unique_email.upper()
        resp = await client.post(
            "/v1/auth/register",
            json={"email": upper_email, "password": "testpass123"},
        )
        assert resp.status_code == 201
        assert resp.json()["data"]["user"]["email"] == unique_email.lower()


class TestLogin:
    async def test_login_happy_path(self, client: AsyncClient, unique_email: str):
        await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123"},
        )
        resp = await client.post(
            "/v1/auth/login",
            json={"email": unique_email, "password": "testpass123"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "access_token" in body["data"]
        assert body["data"]["user"]["email"] == unique_email
        assert "access_token" in resp.cookies

    async def test_login_wrong_password_fails(self, client: AsyncClient, unique_email: str):
        await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123"},
        )
        resp = await client.post(
            "/v1/auth/login",
            json={"email": unique_email, "password": "wrongpassword"},
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "UNAUTHORIZED"

    async def test_login_unknown_email_fails(self, client: AsyncClient):
        resp = await client.post(
            "/v1/auth/login",
            json={"email": "nobody@example.com", "password": "anypassword"},
        )
        assert resp.status_code == 401

    async def test_login_missing_password_fails(self, client: AsyncClient):
        resp = await client.post("/v1/auth/login", json={"email": "user@example.com"})
        assert resp.status_code == 400


class TestRefresh:
    async def test_refresh_via_cookie(self, client: AsyncClient, unique_email: str):
        await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123"},
        )
        # Cookie is set by register; refresh reads it
        resp = await client.post("/v1/auth/refresh")
        assert resp.status_code == 200
        body = resp.json()
        assert "access_token" in body["data"]
        assert "access_token" in resp.cookies

    async def test_refresh_via_body_token(self, client: AsyncClient, unique_email: str):
        reg = await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123"},
        )
        refresh_token = reg.json()["data"]["refresh_token"]
        # Use body-based refresh (API client flow, no cookies)
        resp = await client.post(
            "/v1/auth/refresh",
            json={"refresh_token": refresh_token},
        )
        assert resp.status_code == 200
        assert "access_token" in resp.json()["data"]

    async def test_refresh_without_token_fails(self, client: AsyncClient):
        resp = await client.post("/v1/auth/refresh")
        assert resp.status_code == 401

    async def test_refresh_token_cannot_be_reused(self, client: AsyncClient, unique_email: str):
        """refresh_access_token() deletes the refresh token after issuing new
        ones (rotation). Reusing the same (now-revoked) refresh token must
        fail — otherwise a leaked refresh token would be replayable forever."""
        reg = await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123"},
        )
        refresh_token = reg.json()["data"]["refresh_token"]

        first = await client.post("/v1/auth/refresh", json={"refresh_token": refresh_token})
        assert first.status_code == 200

        second = await client.post("/v1/auth/refresh", json={"refresh_token": refresh_token})
        assert second.status_code == 401


class TestLogout:
    async def test_logout_returns_204(self, client: AsyncClient, unique_email: str):
        await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123"},
        )
        resp = await client.post("/v1/auth/logout")
        assert resp.status_code == 204

    async def test_me_fails_after_logout(self, client: AsyncClient, unique_email: str):
        await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123"},
        )
        await client.post("/v1/auth/logout")
        resp = await client.get("/v1/auth/me")
        assert resp.status_code == 401

    async def test_refresh_fails_after_logout(self, client: AsyncClient, unique_email: str):
        """
        logout_user() deletes the refresh token from Redis. Confirm that
        actually holds — a refresh token captured before logout must not
        still mint new access tokens afterward, or logout would be
        cosmetic (clears cookies client-side) without real server-side
        revocation.
        """
        reg = await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123"},
        )
        refresh_token = reg.json()["data"]["refresh_token"]

        await client.post("/v1/auth/logout")

        resp = await client.post("/v1/auth/refresh", json={"refresh_token": refresh_token})
        assert resp.status_code == 401


class TestMe:
    async def test_me_returns_current_user(self, client: AsyncClient, unique_email: str):
        await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123", "full_name": "Test User"},
        )
        resp = await client.get("/v1/auth/me")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["email"] == unique_email
        assert data["name"] == "Test User"
        assert "id" in data

    async def test_me_without_auth_fails(self, client: AsyncClient):
        resp = await client.get("/v1/auth/me")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "UNAUTHORIZED"

    async def test_me_with_bearer_token(self, client: AsyncClient, unique_email: str):
        reg = await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123"},
        )
        token = reg.json()["data"]["access_token"]
        # Use bearer token instead of cookie
        resp = await client.get(
            "/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["email"] == unique_email

    async def test_me_with_malformed_bearer_token_fails(self, client: AsyncClient):
        """An invalid/tampered JWT must be rejected the same way as a
        missing one (401), not raise an unhandled 500."""
        resp = await client.get(
            "/v1/auth/me",
            headers={"Authorization": "Bearer not.a.valid.jwt"},
        )
        assert resp.status_code == 401


class TestApiKeys:
    async def test_create_api_key(self, client: AsyncClient, unique_email: str):
        await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123"},
        )
        resp = await client.post("/v1/auth/keys", json={"name": "test-key"})
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert "key" in data          # raw key — shown only once
        assert "id" in data
        assert data["name"] == "test-key"

    async def test_list_api_keys(self, client: AsyncClient, unique_email: str):
        await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123"},
        )
        await client.post("/v1/auth/keys", json={"name": "key-a"})
        await client.post("/v1/auth/keys", json={"name": "key-b"})
        resp = await client.get("/v1/auth/keys")
        assert resp.status_code == 200
        names = [k["name"] for k in resp.json()["data"]]
        assert "key-a" in names
        assert "key-b" in names

    async def test_revoke_api_key(self, client: AsyncClient, unique_email: str):
        await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123"},
        )
        create_resp = await client.post("/v1/auth/keys", json={"name": "to-revoke"})
        key_id = create_resp.json()["data"]["id"]

        resp = await client.delete(f"/v1/auth/keys/{key_id}")
        assert resp.status_code == 204

        # Key no longer in list
        list_resp = await client.get("/v1/auth/keys")
        remaining_ids = [k["id"] for k in list_resp.json()["data"]]
        assert key_id not in remaining_ids

    async def test_api_key_authenticates_requests(self, client: AsyncClient, unique_email: str):
        await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123"},
        )
        create_resp = await client.post("/v1/auth/keys", json={"name": "auth-test"})
        raw_key = create_resp.json()["data"]["key"]

        resp = await client.get(
            "/v1/auth/me",
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert resp.status_code == 200

    async def test_create_key_requires_auth(self, client: AsyncClient):
        resp = await client.post("/v1/auth/keys", json={"name": "test"})
        assert resp.status_code == 401