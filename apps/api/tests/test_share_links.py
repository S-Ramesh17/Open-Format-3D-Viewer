"""
Integration tests for share link endpoints.

Covers:
  - create share link (auth + editor role required)
  - resolve share link (public, no auth)
  - revoke share link (owner only)
  - list share links for a model (member only)
  - expiry enforcement
  - revoked link rejection
  - non-owner revoke returns 403
"""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.model import Model
from app.models.model_metadata import ModelMetadata
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.share_link import ShareLink
from app.models.user import User

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def share_test_data(db_session: AsyncSession):
    """
    Direct DB setup for a complete share-link test environment.
    Returns dict with owner User, viewer User, Project, Model, and chunk metadata.
    """
    owner = User(
        id=uuid.uuid4(),
        email=f"owner_{uuid.uuid4().hex[:8]}@example.com",
        password_hash="$2b$12$notarealhash",
        name="Owner",
    )
    db_session.add(owner)

    other_user = User(
        id=uuid.uuid4(),
        email=f"other_{uuid.uuid4().hex[:8]}@example.com",
        password_hash="$2b$12$notarealhash",
        name="Other",
    )
    db_session.add(other_user)
    await db_session.flush()

    project = Project(id=uuid.uuid4(), name="Share Test Project", owner_id=owner.id)
    db_session.add(project)
    await db_session.flush()

    db_session.add(
        ProjectMember(
            id=uuid.uuid4(), project_id=project.id, user_id=owner.id, role="admin"
        )
    )

    model = Model(
        id=uuid.uuid4(),
        project_id=project.id,
        uploaded_by=owner.id,
        name="Test Model",
        original_filename="test.ifc",
        format="ifc",
        file_size_bytes=1024,
        raw_s3_key="raw/key.ifc",
        status="ready",
    )
    db_session.add(model)
    await db_session.flush()

    meta = ModelMetadata(
        model_id=model.id,
        properties={"xkt_chunks": ["processed/part0.xkt", "processed/part1.xkt"]},
        spatial_tree={},
    )
    db_session.add(meta)
    await db_session.commit()

    return {
        "owner": owner,
        "other_user": other_user,
        "project": project,
        "model": model,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _auth_header(user_id: uuid.UUID) -> dict[str, str]:
    """Create a Bearer token Authorization header for a user."""
    from app.core.security import create_access_token
    token = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Create share link
# ---------------------------------------------------------------------------

async def test_create_share_link_happy_path(
    client: AsyncClient, share_test_data: dict
):
    owner = share_test_data["owner"]
    model = share_test_data["model"]

    resp = await client.post(
        "/v1/share",
        json={"model_id": str(model.id)},
        headers=_auth_header(owner.id),
    )
    assert resp.status_code == 201
    data = resp.json()["data"]
    assert "token" in data
    assert data["model_id"] == str(model.id)
    assert data["revoked"] is False
    assert data["expires_at"] is None


async def test_create_share_link_with_expiry(
    client: AsyncClient, share_test_data: dict
):
    owner = share_test_data["owner"]
    model = share_test_data["model"]
    expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()

    resp = await client.post(
        "/v1/share",
        json={"model_id": str(model.id), "expires_at": expires},
        headers=_auth_header(owner.id),
    )
    assert resp.status_code == 201
    assert resp.json()["data"]["expires_at"] is not None


async def test_create_share_link_requires_auth(client: AsyncClient, share_test_data: dict):
    model = share_test_data["model"]
    resp = await client.post("/v1/share", json={"model_id": str(model.id)})
    assert resp.status_code == 401


async def test_create_share_link_non_member_gets_403(
    client: AsyncClient, share_test_data: dict
):
    other = share_test_data["other_user"]
    model = share_test_data["model"]

    resp = await client.post(
        "/v1/share",
        json={"model_id": str(model.id)},
        headers=_auth_header(other.id),
    )
    # other_user has no membership in this project
    assert resp.status_code in (403, 404)


async def test_create_share_link_nonexistent_model(
    client: AsyncClient, share_test_data: dict
):
    owner = share_test_data["owner"]
    resp = await client.post(
        "/v1/share",
        json={"model_id": str(uuid.uuid4())},
        headers=_auth_header(owner.id),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Resolve share link (public)
# ---------------------------------------------------------------------------

async def test_resolve_share_link_happy_path(
    client: AsyncClient, share_test_data: dict, db_session: AsyncSession
):
    model = share_test_data["model"]

    # Create link directly in DB
    link = ShareLink(
        id=uuid.uuid4(),
        model_id=model.id,
        created_by=share_test_data["owner"].id,
        token=f"test-resolve-token-{uuid.uuid4().hex}",
        revoked=False,
    )
    db_session.add(link)
    await db_session.commit()

    with patch("app.services.share.settings") as mock_settings:
        mock_settings.STORAGE_PROVIDER = "local"
        mock_settings.CDN_BASE_URL = "http://cdn.example.com"
        resp = await client.get(f"/v1/share/{link.token}")

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert "link" in body
    assert "model" in body
    assert body["model"]["id"] == str(model.id)
    # chunk_urls populated from model_metadata
    assert len(body["model"]["chunk_urls"]) == 2
    # No sensitive fields
    assert "project_id" not in body["model"]
    assert "uploaded_by" not in body["model"]


async def test_resolve_revoked_share_link(
    client: AsyncClient, share_test_data: dict, db_session: AsyncSession
):
    model = share_test_data["model"]

    link = ShareLink(
        id=uuid.uuid4(),
        model_id=model.id,
        created_by=share_test_data["owner"].id,
        token=f"revoked-token-{uuid.uuid4().hex}",
        revoked=True,
    )
    db_session.add(link)
    await db_session.commit()

    resp = await client.get(f"/v1/share/{link.token}")
    assert resp.status_code == 404


async def test_resolve_expired_share_link(
    client: AsyncClient, share_test_data: dict, db_session: AsyncSession
):
    model = share_test_data["model"]

    link = ShareLink(
        id=uuid.uuid4(),
        model_id=model.id,
        created_by=share_test_data["owner"].id,
        token=f"expired-token-{uuid.uuid4().hex}",
        revoked=False,
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db_session.add(link)
    await db_session.commit()

    resp = await client.get(f"/v1/share/{link.token}")
    assert resp.status_code == 400


async def test_resolve_nonexistent_token(client: AsyncClient):
    resp = await client.get("/v1/share/nonexistent-token-doesnt-exist")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Revoke share link
# ---------------------------------------------------------------------------

async def test_revoke_share_link_happy_path(
    client: AsyncClient, share_test_data: dict, db_session: AsyncSession
):
    owner = share_test_data["owner"]
    model = share_test_data["model"]

    link = ShareLink(
        id=uuid.uuid4(),
        model_id=model.id,
        created_by=owner.id,
        token=f"to-be-revoked-token-{uuid.uuid4().hex}",
        revoked=False,
    )
    db_session.add(link)
    await db_session.commit()

    resp = await client.delete(
        f"/v1/share/{link.id}",
        headers=_auth_header(owner.id),
    )
    assert resp.status_code == 204

    # Confirm it is now revoked
    resolve_resp = await client.get(f"/v1/share/{link.token}")
    assert resolve_resp.status_code == 404


async def test_revoke_requires_auth(
    client: AsyncClient, share_test_data: dict, db_session: AsyncSession
):
    owner = share_test_data["owner"]
    model = share_test_data["model"]

    link = ShareLink(
        id=uuid.uuid4(),
        model_id=model.id,
        created_by=owner.id,
        token=f"revoke-auth-test-token-{uuid.uuid4().hex}",
        revoked=False,
    )
    db_session.add(link)
    await db_session.commit()

    resp = await client.delete(f"/v1/share/{link.id}")
    assert resp.status_code == 401


async def test_revoke_by_non_owner_returns_403(
    client: AsyncClient, share_test_data: dict, db_session: AsyncSession
):
    owner = share_test_data["owner"]
    other = share_test_data["other_user"]
    model = share_test_data["model"]

    link = ShareLink(
        id=uuid.uuid4(),
        model_id=model.id,
        created_by=owner.id,
        token=f"non-owner-revoke-token-{uuid.uuid4().hex}",
        revoked=False,
    )
    db_session.add(link)
    await db_session.commit()

    resp = await client.delete(
        f"/v1/share/{link.id}",
        headers=_auth_header(other.id),
    )
    assert resp.status_code == 403


async def test_revoke_nonexistent_link_returns_404(
    client: AsyncClient, share_test_data: dict
):
    owner = share_test_data["owner"]
    resp = await client.delete(
        f"/v1/share/{uuid.uuid4()}",
        headers=_auth_header(owner.id),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# List share links
# ---------------------------------------------------------------------------

async def test_list_share_links_excludes_revoked(
    client: AsyncClient, share_test_data: dict, db_session: AsyncSession
):
    owner = share_test_data["owner"]
    model = share_test_data["model"]

    # Active link
    active = ShareLink(
        id=uuid.uuid4(),
        model_id=model.id,
        created_by=owner.id,
        token=f"active-list-token-{uuid.uuid4().hex}",
        revoked=False,
    )
    # Revoked link — should NOT appear in list
    revoked = ShareLink(
        id=uuid.uuid4(),
        model_id=model.id,
        created_by=owner.id,
        token=f"revoked-list-token-{uuid.uuid4().hex}",
        revoked=True,
    )
    db_session.add_all([active, revoked])
    await db_session.commit()

    resp = await client.get(
        f"/v1/share/model/{model.id}",
        headers=_auth_header(owner.id),
    )
    assert resp.status_code == 200
    tokens = [item["token"] for item in resp.json()["data"]]
    assert active.token in tokens
    assert revoked.token not in tokens


async def test_list_share_links_requires_membership(
    client: AsyncClient, share_test_data: dict
):
    other = share_test_data["other_user"]
    model = share_test_data["model"]

    resp = await client.get(
        f"/v1/share/model/{model.id}",
        headers=_auth_header(other.id),
    )
    assert resp.status_code in (403, 404)