import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def _register_and_login(client: AsyncClient, email: str) -> str:
    """Helper to create a user and get their auth token."""
    await client.post(
        "/v1/auth/register",
        json={"email": email, "password": "testpass123", "full_name": "Test User"},
    )
    resp = await client.post(
        "/v1/auth/login",
        json={"email": email, "password": "testpass123"},
    )
    return resp.json()["data"]["access_token"]


async def test_webhook_ownership_enforced(client: AsyncClient, unique_email: str):
    token_owner = await _register_and_login(client, f"owner_{unique_email}")
    token_attacker = await _register_and_login(client, f"attacker_{unique_email}")

    # 1. Owner creates a webhook
    resp = await client.post(
        "/v1/webhooks",
        json={"url": "https://example.com/hook", "events": ["model.ready"]},
        headers={"Authorization": f"Bearer {token_owner}"}
    )
    assert resp.status_code == 201
    webhook_id = resp.json()["data"]["id"]
    
    # Validate secret is present ONLY on creation
    assert "secret" in resp.json()["data"]

    # 2. Attacker lists their webhooks — should not see owner's
    resp_list = await client.get("/v1/webhooks", headers={"Authorization": f"Bearer {token_attacker}"})
    assert len(resp_list.json()["data"]) == 0

    # 3. Attacker tries to patch owner's webhook
    resp_patch = await client.patch(
        f"/v1/webhooks/{webhook_id}",
        json={"url": "https://attacker.com/hook"},
        headers={"Authorization": f"Bearer {token_attacker}"}
    )
    assert resp_patch.status_code == 404

    # 4. Attacker tries to read owner's webhook deliveries
    resp_deliveries = await client.get(
        f"/v1/webhooks/{webhook_id}/deliveries",
        headers={"Authorization": f"Bearer {token_attacker}"}
    )
    assert resp_deliveries.status_code == 404

    # 5. Attacker tries to delete owner's webhook
    resp_delete = await client.delete(
        f"/v1/webhooks/{webhook_id}",
        headers={"Authorization": f"Bearer {token_attacker}"}
    )
    assert resp_delete.status_code == 404

    # 6. Owner can successfully query their own deliveries
    resp_deliveries_owner = await client.get(
        f"/v1/webhooks/{webhook_id}/deliveries",
        headers={"Authorization": f"Bearer {token_owner}"}
    )
    assert resp_deliveries_owner.status_code == 200