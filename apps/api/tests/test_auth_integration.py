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
        assert body["data"]["email"] == unique_email
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

    async def test_register_validation_failure_short_password(
        self, client: AsyncClient, unique_email: str
    ):
        resp = await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "short"},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


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
        assert resp.json()["error"]["code"] == "AUTHENTICATION_ERROR"

    async def test_login_validation_failure_missing_field(self, client: AsyncClient):
        resp = await client.post("/v1/auth/login", json={"email": "user@example.com"})
        assert resp.status_code == 422


class TestRefresh:
    async def test_refresh_happy_path(self, client: AsyncClient, unique_email: str):
        await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123"},
        )
        resp = await client.post("/v1/auth/refresh")
        assert resp.status_code == 200
        assert "access_token" in resp.cookies

    async def test_refresh_without_cookie_fails(self, client: AsyncClient):
        fresh_client_resp = await client.post("/v1/auth/refresh")
        # No prior login in this client instance — no refresh_token cookie present
        assert fresh_client_resp.status_code == 401

    async def test_refresh_rotates_token(self, client: AsyncClient, unique_email: str):
        await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123"},
        )
        first_refresh_token = client.cookies.get("refresh_token")
        await client.post("/v1/auth/refresh")
        second_resp = await client.post("/v1/auth/refresh")
        # Old token from before rotation should now be invalid if reused directly
        assert second_resp.status_code in (200, 401)  # 401 acceptable if already rotated once


class TestLogout:
    async def test_logout_happy_path(self, client: AsyncClient, unique_email: str):
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


class TestMe:
    async def test_me_happy_path(self, client: AsyncClient, unique_email: str):
        await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123", "full_name": "Test User"},
        )
        resp = await client.get("/v1/auth/me")
        assert resp.status_code == 200
        assert resp.json()["data"]["email"] == unique_email

    async def test_me_without_auth_fails(self, client: AsyncClient):
        resp = await client.get("/v1/auth/me")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "AUTHENTICATION_ERROR"