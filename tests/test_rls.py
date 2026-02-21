"""
Tests for Row-Level Security (RLS) functionality.

Verifies that:
1. RLS policies are properly enforced
2. Users can only access their own data
3. Job sharing permissions work correctly
4. Admin session functionality works
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.database import (
    list_rls_policies,
    set_current_user,
    verify_rls_enabled,
)
from shandy.database.models import Job, JobShare, User
from tests.helpers import enable_rls


@pytest.mark.asyncio
async def test_rls_enabled_on_tables(db_session: AsyncSession):
    """Verify that RLS is enabled on all expected tables."""
    # Check critical tables
    assert await verify_rls_enabled(db_session, "users")
    assert await verify_rls_enabled(db_session, "jobs")
    assert await verify_rls_enabled(db_session, "job_shares")
    assert await verify_rls_enabled(db_session, "api_keys")
    assert await verify_rls_enabled(db_session, "hypotheses")
    assert await verify_rls_enabled(db_session, "findings")


@pytest.mark.asyncio
async def test_users_cannot_see_each_others_jobs(db_session: AsyncSession):
    """Two users should only see their own jobs, not each other's."""
    # Create two users (admin role, bypasses RLS)
    alice = User(email="alice@example.com", name="Alice")
    bob = User(email="bob@example.com", name="Bob")
    db_session.add_all([alice, bob])
    await db_session.commit()

    # Create jobs for each user
    alice_job = Job(owner_id=alice.id, title="Alice's research")
    bob_job = Job(owner_id=bob.id, title="Bob's research")
    db_session.add_all([alice_job, bob_job])
    await db_session.commit()

    # Switch to app role (subject to RLS)
    await enable_rls(db_session)

    # Alice should only see her own job
    await set_current_user(db_session, alice.id)
    result = await db_session.execute(select(Job))
    alice_visible = result.scalars().all()
    assert len(alice_visible) == 1
    assert alice_visible[0].title == "Alice's research"

    # Bob should only see his own job
    await set_current_user(db_session, bob.id)
    result = await db_session.execute(select(Job))
    bob_visible = result.scalars().all()
    assert len(bob_visible) == 1
    assert bob_visible[0].title == "Bob's research"

    # Alice should not be able to fetch Bob's job by ID
    await set_current_user(db_session, alice.id)
    result = await db_session.execute(select(Job).where(Job.id == bob_job.id))
    assert result.scalar_one_or_none() is None

    # Bob should not be able to fetch Alice's job by ID
    await set_current_user(db_session, bob.id)
    result = await db_session.execute(select(Job).where(Job.id == alice_job.id))
    assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_users_cannot_see_each_others_profiles(db_session: AsyncSession):
    """Users should only see their own record in the users table."""
    alice = User(email="alice2@example.com", name="Alice")
    bob = User(email="bob2@example.com", name="Bob")
    db_session.add_all([alice, bob])
    await db_session.commit()

    await enable_rls(db_session)

    # Alice should only see herself
    await set_current_user(db_session, alice.id)
    result = await db_session.execute(select(User))
    visible = result.scalars().all()
    assert len(visible) == 1
    assert visible[0].id == alice.id

    # Bob should only see himself
    await set_current_user(db_session, bob.id)
    result = await db_session.execute(select(User))
    visible = result.scalars().all()
    assert len(visible) == 1
    assert visible[0].id == bob.id


@pytest.mark.asyncio
async def test_no_rls_context_returns_no_jobs(db_session: AsyncSession):
    """Without set_current_user, queries under RLS should return nothing."""
    user = User(email="lonely@example.com", name="Lonely")
    db_session.add(user)
    await db_session.commit()

    job = Job(owner_id=user.id, title="Hidden job")
    db_session.add(job)
    await db_session.commit()

    # Switch to app role but DON'T set a user context
    await enable_rls(db_session)

    result = await db_session.execute(select(Job))
    assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_sharing_grants_cross_user_visibility(db_session: AsyncSession):
    """Sharing a job should make it visible to the recipient without exposing other jobs."""
    alice = User(email="alice3@example.com", name="Alice")
    bob = User(email="bob3@example.com", name="Bob")
    db_session.add_all([alice, bob])
    await db_session.commit()

    shared_job = Job(owner_id=alice.id, title="Shared research")
    private_job = Job(owner_id=alice.id, title="Private research")
    db_session.add_all([shared_job, private_job])
    await db_session.commit()

    # Share only one of Alice's jobs with Bob
    share = JobShare(
        job_id=shared_job.id,
        shared_with_user_id=bob.id,
        permission_level="view",
    )
    db_session.add(share)
    await db_session.commit()

    await enable_rls(db_session)

    # Bob should see the shared job but NOT Alice's private job
    await set_current_user(db_session, bob.id)
    result = await db_session.execute(select(Job))
    bob_visible = result.scalars().all()
    visible_titles = {j.title for j in bob_visible}
    assert "Shared research" in visible_titles
    assert "Private research" not in visible_titles


@pytest.mark.asyncio
async def test_job_sharing_view_permission(db_session: AsyncSession):
    """Test that view permission grants read-only access."""
    # Create two users and a job (tests run as superuser, bypassing RLS)
    owner = User(email="owner@example.com", name="Owner")
    viewer = User(email="viewer@example.com", name="Viewer")
    db_session.add_all([owner, viewer])
    await db_session.commit()

    job = Job(
        owner_id=owner.id,
        title="Shared Job",
        description="Job shared with viewer",
    )
    db_session.add(job)
    await db_session.commit()

    # Share the job with view permission
    share = JobShare(
        job_id=job.id,
        shared_with_user_id=viewer.id,
        permission_level="view",
    )
    db_session.add(share)
    await db_session.commit()

    # Enable RLS before setting user context (superuser bypasses RLS)
    await enable_rls(db_session)

    # Viewer should be able to read the job
    await set_current_user(db_session, viewer.id)
    result = await db_session.execute(select(Job).where(Job.id == job.id))
    viewed_job = result.scalar_one_or_none()
    assert viewed_job is not None
    assert viewed_job.title == "Shared Job"


@pytest.mark.asyncio
async def test_job_sharing_edit_permission(db_session: AsyncSession):
    """Test that edit permission grants full access."""
    # Create two users and a job
    owner = User(email="owner2@example.com", name="Owner 2")
    editor = User(email="editor@example.com", name="Editor")
    db_session.add_all([owner, editor])
    await db_session.commit()

    job = Job(
        owner_id=owner.id,
        title="Editable Job",
        description="Job shared with editor",
    )
    db_session.add(job)
    await db_session.commit()

    # Share the job with edit permission
    share = JobShare(
        job_id=job.id,
        shared_with_user_id=editor.id,
        permission_level="edit",
    )
    db_session.add(share)
    await db_session.commit()

    # Enable RLS before setting user context (superuser bypasses RLS)
    await enable_rls(db_session)

    # Editor should be able to read and update the job
    await set_current_user(db_session, editor.id)
    result = await db_session.execute(select(Job).where(Job.id == job.id))
    edited_job = result.scalar_one()

    # Update the job
    edited_job.title = "Updated by Editor"
    db_session.add(edited_job)
    await db_session.commit()

    # Verify the update persisted
    result = await db_session.execute(select(Job).where(Job.id == job.id))
    job_check = result.scalar_one()
    assert job_check.title == "Updated by Editor"


@pytest.mark.asyncio
async def test_admin_session_access(db_session: AsyncSession):
    """Test that admin session (superuser) allows access to all rows.

    Since db_session runs as superuser by default (bypassing RLS),
    this tests the behavior when no user context is set.
    """
    # Create data for multiple users
    user1 = User(email="user_a@example.com", name="User A")
    user2 = User(email="user_b@example.com", name="User B")
    db_session.add_all([user1, user2])
    await db_session.commit()

    job1 = Job(owner_id=user1.id, title="Job A")
    job2 = Job(owner_id=user2.id, title="Job B")
    db_session.add_all([job1, job2])
    await db_session.commit()

    # Without user context set, superuser should see all jobs
    result = await db_session.execute(select(Job))
    all_jobs = result.scalars().all()
    assert len(all_jobs) == 2


@pytest.mark.asyncio
async def test_orphaned_jobs_visible(db_session: AsyncSession):
    """Test that orphaned jobs (owner_id=NULL) are visible to all users."""
    # Create orphaned job
    orphaned = Job(owner_id=None, title="Orphaned Job")
    db_session.add(orphaned)
    await db_session.commit()

    user = User(email="viewer@example.com", name="Viewer")
    db_session.add(user)
    await db_session.commit()

    # Enable RLS before setting user context (superuser bypasses RLS)
    await enable_rls(db_session)

    # User should be able to see orphaned job
    await set_current_user(db_session, user.id)
    result = await db_session.execute(select(Job))
    jobs = result.scalars().all()
    assert len(jobs) == 1
    assert jobs[0].title == "Orphaned Job"


@pytest.mark.asyncio
async def test_get_session_enforces_rls(test_engine):
    """get_session() must enforce RLS via SET ROLE shandy_app.

    Verifies that the standard session factory (get_session) drops privileges
    to shandy_app so that RLS policies are actually enforced. Without this,
    a superuser or BYPASSRLS connection silently skips all RLS policies.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from shandy.database.session import _set_app_role

    # Create test data using admin session (bypasses RLS)
    session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as admin_session:
        await admin_session.execute(text("SET ROLE shandy_admin"))

        alice = User(email="alice_gs@example.com", name="Alice")
        bob = User(email="bob_gs@example.com", name="Bob")
        admin_session.add_all([alice, bob])
        await admin_session.commit()

        alice_job = Job(owner_id=alice.id, title="Alice get_session job")
        bob_job = Job(owner_id=bob.id, title="Bob get_session job")
        admin_session.add_all([alice_job, bob_job])
        await admin_session.commit()

        alice_id = alice.id

    # Now simulate get_session() behavior: SET ROLE shandy_app + set_current_user
    async with session_factory() as user_session:
        await _set_app_role(user_session)  # This is what get_session() now does
        await set_current_user(user_session, alice_id)

        result = await user_session.execute(select(Job))
        visible = result.scalars().all()

        # With RLS enforced, Alice should only see her own job
        assert len(visible) == 1, (
            f"RLS NOT ENFORCED: Alice sees {len(visible)} jobs instead of 1. "
            f"get_session() must SET ROLE shandy_app for RLS to work."
        )
        assert visible[0].title == "Alice get_session job"

        # Also verify Bob's job is NOT visible by direct ID lookup
        result = await user_session.execute(select(Job).where(Job.id == bob_job.id))
        assert result.scalar_one_or_none() is None, (
            "Alice can see Bob's job by direct ID — RLS is not enforced"
        )


@pytest.mark.asyncio
async def test_bypassrls_role_sees_all_jobs(test_engine):
    """Verify that admin/BYPASSRLS role can see all jobs (sanity check).

    Without SET ROLE shandy_app, the session role bypasses RLS even when
    set_current_user() is called. This test documents that behavior.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as admin_session:
        await admin_session.execute(text("SET ROLE shandy_admin"))

        alice = User(email="alice_bypass@example.com", name="Alice")
        bob = User(email="bob_bypass@example.com", name="Bob")
        admin_session.add_all([alice, bob])
        await admin_session.commit()

        alice_job = Job(owner_id=alice.id, title="Alice bypass job")
        bob_job = Job(owner_id=bob.id, title="Bob bypass job")
        admin_session.add_all([alice_job, bob_job])
        await admin_session.commit()

        alice_id = alice.id

    # Use admin role (BYPASSRLS) — RLS is NOT enforced
    async with session_factory() as admin_session:
        await admin_session.execute(text("SET ROLE shandy_admin"))
        await set_current_user(admin_session, alice_id)

        result = await admin_session.execute(select(Job))
        visible = result.scalars().all()

        # Admin bypasses RLS — sees all jobs regardless of set_current_user
        assert len(visible) >= 2


@pytest.mark.asyncio
async def test_list_rls_policies(db_session: AsyncSession):
    """Test listing RLS policies."""
    policies = await list_rls_policies(db_session, "jobs")
    assert len(policies) > 0
    # Should have policies for jobs table
    policy_names = [p["name"] for p in policies]
    assert any("jobs" in name for name in policy_names)


# =============================================================================
# Regression tests: ensure auth routes and middleware use admin sessions,
# and that get_session() enforces RLS via SET ROLE.
# =============================================================================


def test_oauth_callback_uses_admin_session():
    """OAuth callback must use get_admin_session, not get_session.

    User creation/update during OAuth login is a cross-tenant operation
    (no user context exists yet). Using get_session() would fail because
    SET ROLE shandy_app + no set_current_user = zero rows visible.
    """
    import inspect

    from shandy.auth.fastapi_routes import oauth_callback

    source = inspect.getsource(oauth_callback)
    assert "get_admin_session" in source, (
        "oauth_callback must use get_admin_session(), not get_session(). "
        "User creation during OAuth is a cross-tenant operation."
    )
    assert "get_session()" not in source, (
        "oauth_callback must NOT use get_session() — it enforces RLS, "
        "but there is no user context during login."
    )


def test_auth_middleware_uses_admin_session():
    """Session validation middleware must use get_admin_session.

    The validate_session function looks up sessions and users before any
    user context is known. Using get_session() would enforce RLS with no
    user set, returning zero rows and breaking all authentication.
    """
    import inspect

    from shandy.auth.middleware import validate_session

    source = inspect.getsource(validate_session)
    assert "get_admin_session" in source, (
        "validate_session must use get_admin_session(), not get_session(). "
        "Session lookup happens before user context is established."
    )


def test_get_session_sets_app_role():
    """get_session() must call _set_app_role to enforce RLS.

    Without SET ROLE shandy_app, the main database user (typically a
    superuser) bypasses all RLS policies silently.
    """
    import inspect

    from shandy.database.session import get_session

    source = inspect.getsource(get_session)
    assert "_set_app_role" in source, (
        "get_session() must call _set_app_role() to SET ROLE shandy_app. "
        "Without this, superuser connections bypass all RLS policies."
    )


def test_mock_login_uses_admin_session():
    """Mock login routes must use get_admin_session.

    Same rationale as OAuth — user creation is cross-tenant.
    """
    import inspect

    from shandy.auth.fastapi_routes import mock_admin_oauth_login, mock_oauth_login

    for fn in [mock_oauth_login, mock_admin_oauth_login]:
        source = inspect.getsource(fn)
        assert "get_admin_session" in source, (
            f"{fn.__name__} must use get_admin_session(), not get_session(). "
            f"User creation during login is a cross-tenant operation."
        )


def test_logout_uses_admin_session():
    """Logout must use get_admin_session to delete sessions.

    Session deletion is a cross-tenant operation — the session record
    needs to be found and deleted regardless of RLS context.
    """
    import inspect

    from shandy.auth.fastapi_routes import logout

    source = inspect.getsource(logout)
    assert "get_admin_session" in source, (
        "logout must use get_admin_session(), not get_session(). "
        "Session deletion requires cross-tenant access."
    )
