"""The SECURITY DEFINER helpers that RLS policies call resolve their tables in
``public`` only, so a session cannot shadow them with temporary tables."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.database.models import APIKey, Job, JobShare, User
from openscientist.database.rls import set_current_user
from tests.helpers import enable_rls


@dataclass(frozen=True)
class _Tenants:
    alice: UUID
    bob_key: UUID
    bob_job: UUID
    bob_share: UUID


@pytest_asyncio.fixture
async def tenants(db_session: AsyncSession) -> _Tenants:
    alice = User(email=f"alice-{uuid4().hex}@definer.test", name="Alice")
    bob = User(email=f"bob-{uuid4().hex}@definer.test", name="Bob")
    carol = User(email=f"carol-{uuid4().hex}@definer.test", name="Carol")
    db_session.add_all([alice, bob, carol])
    await db_session.flush()
    key = APIKey(user_id=bob.id, name="bob", key_hash=uuid4().hex)
    job = Job(owner_id=bob.id, research_question="Bob's")
    db_session.add_all([key, job])
    await db_session.flush()
    share = JobShare(job_id=job.id, shared_with_user_id=carol.id, permission_level="view")
    db_session.add(share)
    await db_session.flush()
    return _Tenants(alice=alice.id, bob_key=key.id, bob_job=job.id, bob_share=share.id)


async def _as_alice(session: AsyncSession, tenants: _Tenants) -> None:
    await enable_rls(session)
    await set_current_user(session, tenants.alice)


@pytest.mark.asyncio
async def test_a_temporary_administrators_table_does_not_make_a_user_admin(
    db_session: AsyncSession, tenants: _Tenants
) -> None:
    await _as_alice(db_session, tenants)
    await db_session.execute(text("CREATE TEMP TABLE administrators (user_id uuid)"))
    await db_session.execute(
        text("INSERT INTO pg_temp.administrators VALUES (:id)"), {"id": tenants.alice}
    )
    # The helpers' owner may read it, as an attacker would allow.
    await db_session.execute(text("GRANT SELECT ON pg_temp.administrators TO PUBLIC"))
    keys = await db_session.execute(
        text("SELECT id FROM api_keys WHERE id = :id"), {"id": tenants.bob_key}
    )
    assert keys.first() is None


@pytest.mark.asyncio
async def test_a_temporary_jobs_table_does_not_make_a_user_the_owner(
    db_session: AsyncSession, tenants: _Tenants
) -> None:
    await _as_alice(db_session, tenants)
    await db_session.execute(text("CREATE TEMP TABLE jobs (id uuid, owner_id uuid)"))
    await db_session.execute(
        text("INSERT INTO pg_temp.jobs VALUES (:job, :alice)"),
        {"job": tenants.bob_job, "alice": tenants.alice},
    )
    await db_session.execute(text("GRANT SELECT ON pg_temp.jobs TO PUBLIC"))
    shares = await db_session.execute(
        text("SELECT id FROM public.job_shares WHERE id = :id"), {"id": tenants.bob_share}
    )
    assert shares.first() is None
