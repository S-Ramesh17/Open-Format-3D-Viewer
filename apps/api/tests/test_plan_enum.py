"""
Regression test for the 'enterprise' plan_enum gap.

app/middleware/rate_limit.py, app/services/storage.py, and
app/services/models.py all branch on plan == "enterprise" with real
behavior (unlimited rate limit, 5GB upload cap). But the Postgres
plan_enum type — and the SQLAlchemy User.plan column definition mirroring
it — only defined 'free' and 'pro' until migration
f1a2b3c4d5e6_add_enterprise_to_plan_enum. Setting a user's plan to
"enterprise" and committing would raise
psycopg2.errors.InvalidTextRepresentation before that migration, making
every "enterprise" code path unreachable in practice.

This test exercises the real Postgres enum (not just Python string
comparisons), so it fails the way the original bug actually failed —
at INSERT/UPDATE time — if the enum value is ever missing again.
"""
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User

pytestmark = pytest.mark.asyncio


async def test_enterprise_is_a_valid_plan_value(db_session: AsyncSession):
    user = User(
        id=uuid.uuid4(),
        email=f"enterprise_{uuid.uuid4().hex[:8]}@example.com",
        password_hash="not-a-real-hash",
        full_name="Enterprise Tester",
        plan="enterprise",
    )
    db_session.add(user)
    # The bug only manifests at flush/commit time, when Postgres actually
    # validates the value against plan_enum — adding the object alone
    # wouldn't have caught it.
    await db_session.flush()

    await db_session.refresh(user)
    assert user.plan == "enterprise"


async def test_free_and_pro_still_valid(db_session: AsyncSession):
    """Make sure widening the enum didn't disturb the existing values."""
    for plan in ("free", "pro"):
        user = User(
            id=uuid.uuid4(),
            email=f"{plan}_{uuid.uuid4().hex[:8]}@example.com",
            password_hash="not-a-real-hash",
            full_name=f"{plan} Tester",
            plan=plan,
        )
        db_session.add(user)
        await db_session.flush()
        await db_session.refresh(user)
        assert user.plan == plan