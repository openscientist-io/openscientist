"""
HTTP endpoint tests for the REST API.

Tests actual HTTP requests to API endpoints with mocked authentication.
"""

import io
import json
import tempfile
import tracemalloc
import uuid
import zipfile
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.api.auth import hash_secret
from openscientist.database.models import Administrator, APIKey, Job, User
from openscientist.database.rls import use_app_role
from openscientist.exceptions import FileTooBigError
from tests.admin_row import reset_admin_settings_row, set_admin_settings_row
from tests.helpers import enable_rls


@pytest.fixture
def mock_user() -> User:
    """Create a mock user for testing."""
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.email = "test@example.com"
    user.name = "Test User"
    user.is_active = True
    return user


@pytest.fixture
def mock_user2() -> User:
    """Create a second mock user for testing."""
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.email = "test2@example.com"
    user.name = "Test User 2"
    user.is_active = True
    return user


@pytest_asyncio.fixture
async def test_user_db(db_session: AsyncSession) -> User:
    """Create a real user in the test database."""
    user = User(
        email="apitest@example.com",
        name="API Test User",
        is_approved=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def committed_share_owner(
    test_engine: AsyncEngine,
) -> AsyncIterator[tuple[User, Job]]:
    """A committed owner and a job they own, so the endpoint's real
    per-request sessions read the rows the way the API's do."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = User(
            email=f"api-share-owner-{uuid.uuid4().hex}@share.test",
            name="API Share Owner",
            is_approved=True,
        )
        db.add(owner)
        await db.flush()
        job = Job(owner_id=owner.id, research_question="API Shared Job", status="pending")
        db.add(job)
        await db.commit()
        await db.refresh(job)
    yield owner, job
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(Job).where(Job.id == job.id))
        await db.execute(delete(User).where(User.id == owner.id))
        await db.commit()


@pytest_asyncio.fixture
async def test_user2_db(db_session: AsyncSession) -> User:
    """Create a second real user in the test database."""
    user = User(
        email="apitest2@example.com",
        name="API Test User 2",
        is_approved=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def test_api_key_db(
    db_session: AsyncSession,
    test_user_db: User,
) -> tuple[APIKey, str]:
    """Create a real API key in the test database."""
    secret = "test_secret_for_api_tests"
    api_key = APIKey(
        user_id=test_user_db.id,
        name="test-api-key",
        key_hash=hash_secret(secret),
        is_active=True,
    )
    db_session.add(api_key)
    await db_session.commit()
    await db_session.refresh(api_key)
    return api_key, f"test-api-key:{secret}"


@pytest_asyncio.fixture
async def admin_row_reset(test_engine: AsyncEngine) -> AsyncGenerator[None, None]:
    """Restore the default admin settings row after the test."""
    try:
        yield
    finally:
        await reset_admin_settings_row(test_engine)


@pytest_asyncio.fixture
async def test_job_db(
    db_session: AsyncSession,
    test_user_db: User,
) -> Job:
    """Create a real job in the test database."""
    job = Job(
        owner_id=test_user_db.id,
        research_question="Test API Job",
        description="A job for API testing",
        status="pending",
        max_iterations=5,
        current_iteration=0,
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    return job


@pytest_asyncio.fixture
async def completed_job_db(
    db_session: AsyncSession,
    test_user_db: User,
) -> Job:
    """Create a completed job in the test database."""
    job = Job(
        owner_id=test_user_db.id,
        research_question="Completed Test Job",
        description="A completed job for testing",
        status="completed",
        max_iterations=5,
        current_iteration=5,
        result_summary="Analysis complete.",
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    return job


def _build_authenticated_app(db_session: AsyncSession, user: User):
    """Create an app with the authenticated user and a session that runs as
    openscientist_app, as get_session does."""
    from fastapi import FastAPI

    from openscientist.api.auth import get_current_user_from_api_key
    from openscientist.api.router import api_router as router
    from openscientist.database.rls import set_current_user
    from openscientist.database.session import get_session

    app = FastAPI()

    async def override_get_session():
        await use_app_role(db_session)
        await set_current_user(db_session, user.id)
        yield db_session

    async def override_get_user():
        return user

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_current_user_from_api_key] = override_get_user
    app.include_router(router)
    return app


class TestHealthEndpoint:
    """Tests for the health check endpoint."""

    @pytest.mark.asyncio
    async def test_health_check(self):
        """Health check returns ok status."""
        # Create a minimal FastAPI app for testing
        from fastapi import FastAPI

        from openscientist.api.router import api_router as router

        app = FastAPI()
        app.include_router(router)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/api/v1/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["api"] == "openscientist"


class TestAPIKeyEndpoints:
    """Tests for API key management endpoints."""

    @pytest.mark.asyncio
    async def test_create_api_key_requires_auth(self):
        """Creating an API key requires authentication."""
        from fastapi import FastAPI

        from openscientist.api.router import api_router as router

        app = FastAPI()
        app.include_router(router)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/keys",
                json={"name": "test-key"},
            )

        # Should fail without auth (401 Unauthorized)
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_create_api_key_success(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_api_key_db: tuple[APIKey, str],
    ):
        """Create a new API key successfully."""
        from fastapi import FastAPI

        from openscientist.api.auth import get_current_user_from_api_key
        from openscientist.api.router import api_router as router
        from openscientist.database.session import get_session

        _, full_key = test_api_key_db

        app = FastAPI()

        # Override dependencies
        async def override_get_session():
            yield db_session

        async def override_get_user():
            return test_user_db

        app.dependency_overrides[get_session] = override_get_session
        app.dependency_overrides[get_current_user_from_api_key] = override_get_user
        app.include_router(router)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/keys",
                json={"name": "new-test-key"},
                headers={"Authorization": f"Bearer {full_key}"},
            )

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "new-test-key"
        assert "key" in data  # Full key is returned on creation
        assert data["key"].startswith("new-test-key:")
        assert data["is_active"] is True

    @pytest.mark.asyncio
    async def test_list_api_keys(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_api_key_db: tuple[APIKey, str],
    ):
        """List API keys for authenticated user."""
        from fastapi import FastAPI

        from openscientist.api.auth import get_current_user_from_api_key
        from openscientist.api.router import api_router as router
        from openscientist.database.session import get_session

        _api_key, full_key = test_api_key_db

        app = FastAPI()

        async def override_get_session():
            yield db_session

        async def override_get_user():
            return test_user_db

        app.dependency_overrides[get_session] = override_get_session
        app.dependency_overrides[get_current_user_from_api_key] = override_get_user
        app.include_router(router)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/api/v1/keys",
                headers={"Authorization": f"Bearer {full_key}"},
            )

        assert response.status_code == 200
        data = response.json()
        assert "keys" in data
        # Should have at least our test key
        assert len(data["keys"]) >= 1
        # Keys should not include the secret
        for key in data["keys"]:
            assert "key" not in key or ":" not in key.get("key", "")

    @pytest.mark.asyncio
    async def test_revoke_api_key(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_api_key_db: tuple[APIKey, str],
    ):
        """Revoke an API key."""
        from fastapi import FastAPI

        from openscientist.api.auth import get_current_user_from_api_key
        from openscientist.api.router import api_router as router
        from openscientist.database.session import get_session

        _api_key, full_key = test_api_key_db

        # Create another key to revoke (can't revoke the one we're using)
        key_to_revoke = APIKey(
            user_id=test_user_db.id,
            name="key-to-revoke",
            key_hash=hash_secret("revoke-secret"),
            is_active=True,
        )
        db_session.add(key_to_revoke)
        await db_session.commit()
        await db_session.refresh(key_to_revoke)

        app = FastAPI()

        async def override_get_session():
            yield db_session

        async def override_get_user():
            return test_user_db

        app.dependency_overrides[get_session] = override_get_session
        app.dependency_overrides[get_current_user_from_api_key] = override_get_user
        app.include_router(router)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.delete(
                f"/api/v1/keys/{key_to_revoke.id}",
                headers={"Authorization": f"Bearer {full_key}"},
            )

        assert response.status_code == 204

        # Verify key is now inactive
        await db_session.refresh(key_to_revoke)
        assert key_to_revoke.is_active is False

    @pytest.mark.asyncio
    async def test_duplicate_key_name_rejected(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_api_key_db: tuple[APIKey, str],
    ):
        """Duplicate API key name for same user is rejected."""
        from fastapi import FastAPI

        from openscientist.api.auth import get_current_user_from_api_key
        from openscientist.api.router import api_router as router
        from openscientist.database.session import get_session

        _api_key, full_key = test_api_key_db

        app = FastAPI()

        async def override_get_session():
            yield db_session

        async def override_get_user():
            return test_user_db

        app.dependency_overrides[get_session] = override_get_session
        app.dependency_overrides[get_current_user_from_api_key] = override_get_user
        app.include_router(router)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            # Try to create key with same name as existing
            response = await client.post(
                "/api/v1/keys",
                json={"name": "test-api-key"},  # Same as test_api_key_db
                headers={"Authorization": f"Bearer {full_key}"},
            )

        assert response.status_code == 409
        assert "already exists" in response.json()["detail"]


class TestJobEndpoints:
    """Tests for job management endpoints."""

    def test_jobs_endpoint_docstrings_are_domain_agnostic(self):
        """Jobs endpoint docs should not be restricted to crystallography wording."""
        from openscientist.api.endpoints import jobs as jobs_endpoints

        module_doc = (jobs_endpoints.__doc__ or "").lower()
        create_doc = (jobs_endpoints.create_job.__doc__ or "").lower()

        assert "crystallography" not in module_doc
        assert "crystallography" not in create_doc

    @pytest.mark.asyncio
    async def test_list_jobs(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_api_key_db: tuple[APIKey, str],
        test_job_db: Job,
    ):
        """List jobs for authenticated user."""
        _ = test_job_db
        from fastapi import FastAPI

        from openscientist.api.auth import get_current_user_from_api_key
        from openscientist.api.router import api_router as router
        from openscientist.database.rls import set_current_user
        from openscientist.database.session import get_session

        _, full_key = test_api_key_db

        app = FastAPI()

        async def override_get_session():
            # Set RLS context for the user
            await set_current_user(db_session, test_user_db.id)
            yield db_session

        async def override_get_user():
            return test_user_db

        app.dependency_overrides[get_session] = override_get_session
        app.dependency_overrides[get_current_user_from_api_key] = override_get_user
        app.include_router(router)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/api/v1/jobs",
                headers={"Authorization": f"Bearer {full_key}"},
            )

        assert response.status_code == 200
        data = response.json()
        assert "jobs" in data
        assert "total" in data
        assert data["total"] >= 1

    @pytest.mark.asyncio
    async def test_get_job_detail(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_api_key_db: tuple[APIKey, str],
        test_job_db: Job,
    ):
        """Get job details."""
        from fastapi import FastAPI

        from openscientist.api.auth import get_current_user_from_api_key
        from openscientist.api.router import api_router as router
        from openscientist.database.rls import set_current_user
        from openscientist.database.session import get_session

        _, full_key = test_api_key_db

        app = FastAPI()

        async def override_get_session():
            await set_current_user(db_session, test_user_db.id)
            yield db_session

        async def override_get_user():
            return test_user_db

        app.dependency_overrides[get_session] = override_get_session
        app.dependency_overrides[get_current_user_from_api_key] = override_get_user
        app.include_router(router)

        test_job_db.stop_note = "Job cost limit of $5.00 reached: the job has spent $5.00."
        await db_session.commit()

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                f"/api/v1/jobs/{test_job_db.id}",
                headers={"Authorization": f"Bearer {full_key}"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(test_job_db.id)
        assert data["research_question"] == "Test API Job"
        assert data["status"] == "pending"
        assert data["stop_note"] == "Job cost limit of $5.00 reached: the job has spent $5.00."

    @pytest.mark.asyncio
    async def test_get_job_detail_reads_research_metadata_from_database(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_api_key_db: tuple[APIKey, str],
        test_job_db: Job,
    ):
        """Job detail should use DB fields for research metadata."""
        from fastapi import FastAPI

        from openscientist.api.auth import get_current_user_from_api_key
        from openscientist.api.router import api_router as router
        from openscientist.database.rls import set_current_user
        from openscientist.database.session import get_session

        _, full_key = test_api_key_db
        test_job_db.version_info = {"openscientist_commit": "abc123def456", "agent_harness": "omp"}
        await db_session.commit()

        app = FastAPI()

        async def override_get_session():
            await set_current_user(db_session, test_user_db.id)
            yield db_session

        async def override_get_user():
            return test_user_db

        app.dependency_overrides[get_session] = override_get_session
        app.dependency_overrides[get_current_user_from_api_key] = override_get_user
        app.include_router(router)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                f"/api/v1/jobs/{test_job_db.id}",
                headers={"Authorization": f"Bearer {full_key}"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["research_question"] == "Test API Job"
        assert data["investigation_mode"] == test_job_db.investigation_mode
        assert data["version_info"] == {
            "openscientist_commit": "abc123def456",
            "agent_harness": "omp",
        }

    @pytest.mark.asyncio
    async def test_get_job_status(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_api_key_db: tuple[APIKey, str],
        test_job_db: Job,
    ):
        """Get job status (lightweight endpoint)."""
        from fastapi import FastAPI

        from openscientist.api.auth import get_current_user_from_api_key
        from openscientist.api.router import api_router as router
        from openscientist.database.rls import set_current_user
        from openscientist.database.session import get_session

        _, full_key = test_api_key_db

        app = FastAPI()

        async def override_get_session():
            await set_current_user(db_session, test_user_db.id)
            yield db_session

        async def override_get_user():
            return test_user_db

        app.dependency_overrides[get_session] = override_get_session
        app.dependency_overrides[get_current_user_from_api_key] = override_get_user
        app.include_router(router)

        test_job_db.stop_note = "Project spend total limit of $100.00 reached."
        await db_session.commit()

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                f"/api/v1/jobs/{test_job_db.id}/status",
                headers={"Authorization": f"Bearer {full_key}"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(test_job_db.id)
        assert data["status"] == "pending"
        assert data["current_iteration"] == 0
        assert data["max_iterations"] == 5
        assert data["stop_note"] == "Project spend total limit of $100.00 reached."

    @pytest.mark.asyncio
    async def test_get_nonexistent_job_returns_404(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_api_key_db: tuple[APIKey, str],
    ):
        """Getting a non-existent job returns 404."""
        from fastapi import FastAPI

        from openscientist.api.auth import get_current_user_from_api_key
        from openscientist.api.router import api_router as router
        from openscientist.database.rls import set_current_user
        from openscientist.database.session import get_session

        _, full_key = test_api_key_db

        app = FastAPI()

        async def override_get_session():
            await set_current_user(db_session, test_user_db.id)
            yield db_session

        async def override_get_user():
            return test_user_db

        app.dependency_overrides[get_session] = override_get_session
        app.dependency_overrides[get_current_user_from_api_key] = override_get_user
        app.include_router(router)

        fake_job_id = str(uuid.uuid4())

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                f"/api/v1/jobs/{fake_job_id}",
                headers={"Authorization": f"Bearer {full_key}"},
            )

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_job_invalid_uuid_returns_400(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_api_key_db: tuple[APIKey, str],
    ):
        """Malformed job IDs should return client errors, not 500s."""
        from fastapi import FastAPI

        from openscientist.api.auth import get_current_user_from_api_key
        from openscientist.api.router import api_router as router
        from openscientist.database.rls import set_current_user
        from openscientist.database.session import get_session

        _, full_key = test_api_key_db

        app = FastAPI()

        async def override_get_session():
            await set_current_user(db_session, test_user_db.id)
            yield db_session

        async def override_get_user():
            return test_user_db

        app.dependency_overrides[get_session] = override_get_session
        app.dependency_overrides[get_current_user_from_api_key] = override_get_user
        app.include_router(router)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/api/v1/jobs/not-a-uuid",
                headers={"Authorization": f"Bearer {full_key}"},
            )

        assert response.status_code == 400
        assert "Invalid job_id format" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_cannot_access_other_users_job(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_user2_db: User,
        test_api_key_db: tuple[APIKey, str],
    ):
        """Users cannot access jobs they don't own."""
        from fastapi import FastAPI

        from openscientist.api.auth import get_current_user_from_api_key
        from openscientist.api.router import api_router as router
        from openscientist.database.rls import set_current_user
        from openscientist.database.session import get_session

        _, full_key = test_api_key_db

        # Create job for user2
        other_job = Job(
            owner_id=test_user2_db.id,
            research_question="Other User's Job",
            description="Belongs to user2",
            status="pending",
        )
        db_session.add(other_job)
        await db_session.commit()
        await db_session.refresh(other_job)

        # Enable RLS before setting user context (superuser bypasses RLS)
        await enable_rls(db_session)

        app = FastAPI()

        async def override_get_session():
            # Set RLS context to user1 (who shouldn't have access)
            await set_current_user(db_session, test_user_db.id)
            yield db_session

        async def override_get_user():
            return test_user_db  # Authenticated as user1

        app.dependency_overrides[get_session] = override_get_session
        app.dependency_overrides[get_current_user_from_api_key] = override_get_user
        app.include_router(router)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                f"/api/v1/jobs/{other_job.id}",
                headers={"Authorization": f"Bearer {full_key}"},
            )

        # RLS should block access
        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.parametrize("job_status", ["running", "pending"])
    async def test_cancel_active_job(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_api_key_db: tuple[APIKey, str],
        job_status: str,
    ):
        """The cancel endpoint hands the job's id to the job manager and does not write the row's status itself."""
        _, full_key = test_api_key_db

        job = Job(
            owner_id=test_user_db.id,
            research_question="Job to cancel",
            description=f"Cancel while {job_status}",
            status=job_status,
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

        app = _build_authenticated_app(db_session, test_user_db)
        mock_job_manager = MagicMock()
        mock_job_manager.cancel_job = AsyncMock()

        with patch(
            "openscientist.api.endpoints.jobs._get_job_manager", return_value=mock_job_manager
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    f"/api/v1/jobs/{job.id}/cancel",
                    headers={"Authorization": f"Bearer {full_key}"},
                )

        assert response.status_code == 204
        mock_job_manager.cancel_job.assert_awaited_once_with(str(job.id))
        await db_session.refresh(job)
        assert job.status == job_status

    @pytest.mark.asyncio
    async def test_cannot_cancel_completed_job(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_api_key_db: tuple[APIKey, str],
        completed_job_db: Job,
    ):
        """Cannot cancel a completed job."""
        _, full_key = test_api_key_db

        app = _build_authenticated_app(db_session, test_user_db)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                f"/api/v1/jobs/{completed_job_db.id}/cancel",
                headers={"Authorization": f"Bearer {full_key}"},
            )

        assert response.status_code == 400
        assert "Cannot cancel" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_create_job(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_api_key_db: tuple[APIKey, str],
    ):
        """Create a new job via API."""
        from datetime import datetime
        from types import SimpleNamespace

        from fastapi import FastAPI

        from openscientist.api.auth import get_current_user_from_api_key
        from openscientist.api.router import api_router as router
        from openscientist.database.rls import set_current_user
        from openscientist.database.session import get_session

        _, full_key = test_api_key_db

        app = FastAPI()

        async def override_get_session():
            await use_app_role(db_session)
            await set_current_user(db_session, test_user_db.id)
            yield db_session

        async def override_get_user():
            return test_user_db

        # Mock job manager
        mock_job_manager = MagicMock()
        mock_job_manager.create_job = AsyncMock()
        mock_loaded_job = SimpleNamespace(
            id=uuid.uuid4(),
            research_question="What is the structure of protein X?",
            short_title="API Created Job",
            description="Created via REST API",
            status="pending",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            max_iterations=5,
            current_iteration=0,
            pdb_code=None,
            space_group=None,
            queue_reason=None,
            stop_note=None,
            is_public=False,
        )

        app.dependency_overrides[get_session] = override_get_session
        app.dependency_overrides[get_current_user_from_api_key] = override_get_user
        app.include_router(router)

        with (
            patch(
                "openscientist.api.endpoints.jobs._get_job_manager", return_value=mock_job_manager
            ),
            patch(
                "openscientist.api.endpoints.jobs.get_job_by_id", new_callable=AsyncMock
            ) as mock_get_job,
        ):
            mock_get_job.return_value = mock_loaded_job
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/api/v1/jobs",
                    json={
                        "short_title": "API Created Job",
                        "description": "Created via REST API",
                        "research_question": "What is the structure of protein X?",
                        "max_iterations": 5,
                        "investigation_mode": "autonomous",
                    },
                    headers={"Authorization": f"Bearer {full_key}"},
                )

        assert response.status_code == 201
        data = response.json()
        assert data["research_question"] == "What is the structure of protein X?"
        assert data["short_title"] == "API Created Job"
        assert data["status"] == "pending"
        mock_job_manager.create_job.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_job_accepts_multipart_with_uploaded_files(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_api_key_db: tuple[APIKey, str],
    ):
        """Create job via multipart/form-data and forward uploaded files."""
        from datetime import datetime
        from types import SimpleNamespace

        from fastapi import FastAPI

        from openscientist.api.auth import get_current_user_from_api_key
        from openscientist.api.router import api_router as router
        from openscientist.database.rls import set_current_user
        from openscientist.database.session import get_session

        _, full_key = test_api_key_db

        app = FastAPI()

        async def override_get_session():
            await use_app_role(db_session)
            await set_current_user(db_session, test_user_db.id)
            yield db_session

        async def override_get_user():
            return test_user_db

        captured_uploads: dict[str, list[str]] = {"names": [], "contents": []}

        def capture_create_job(*args, **kwargs):
            _ = args
            uploaded = kwargs["data_files"]
            captured_uploads["names"] = [p.name for p in uploaded]
            captured_uploads["contents"] = [p.read_text(encoding="utf-8") for p in uploaded]

        mock_job_manager = MagicMock()
        mock_job_manager.create_job = AsyncMock(side_effect=capture_create_job)
        mock_loaded_job = SimpleNamespace(
            id=uuid.uuid4(),
            research_question="Multipart Job",
            short_title=None,
            description="Created via multipart API",
            status="pending",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            max_iterations=7,
            current_iteration=0,
            pdb_code=None,
            space_group=None,
            queue_reason=None,
            stop_note=None,
            is_public=False,
        )

        app.dependency_overrides[get_session] = override_get_session
        app.dependency_overrides[get_current_user_from_api_key] = override_get_user
        app.include_router(router)

        with (
            patch(
                "openscientist.api.endpoints.jobs._get_job_manager", return_value=mock_job_manager
            ),
            patch(
                "openscientist.api.endpoints.jobs.get_job_by_id", new_callable=AsyncMock
            ) as mock_get_job,
        ):
            mock_get_job.return_value = mock_loaded_job
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/api/v1/jobs",
                    data={
                        "short_title": "Multipart Job",
                        "description": "Created via multipart API",
                        "research_question": "How does uploaded data affect output?",
                        "max_iterations": "7",
                        "use_hypotheses": "true",
                        "investigation_mode": "coinvestigate",
                    },
                    files=[
                        ("data_files", ("dataset.csv", b"a,b\n1,2\n", "text/csv")),
                        ("data_files", ("notes.txt", b"hello\n", "text/plain")),
                    ],
                    headers={"Authorization": f"Bearer {full_key}"},
                )

        assert response.status_code == 201
        mock_job_manager.create_job.assert_called_once()
        called_kwargs = mock_job_manager.create_job.call_args.kwargs
        assert called_kwargs["max_iterations"] == 7
        assert called_kwargs["use_hypotheses"] is True
        assert called_kwargs["investigation_mode"] == "coinvestigate"
        assert captured_uploads["names"] == ["dataset.csv", "notes.txt"]
        assert captured_uploads["contents"] == ["a,b\n1,2\n", "hello\n"]

    @pytest.mark.asyncio
    async def test_create_job_uploads_with_duplicate_names_get_unique_temp_paths(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_api_key_db: tuple[APIKey, str],
    ):
        """Duplicate multipart upload names should not overwrite each other."""
        from datetime import datetime
        from types import SimpleNamespace

        from fastapi import FastAPI

        from openscientist.api.auth import get_current_user_from_api_key
        from openscientist.api.router import api_router as router
        from openscientist.database.rls import set_current_user
        from openscientist.database.session import get_session

        _, full_key = test_api_key_db

        app = FastAPI()

        async def override_get_session():
            await use_app_role(db_session)
            await set_current_user(db_session, test_user_db.id)
            yield db_session

        async def override_get_user():
            return test_user_db

        captured_names: list[str] = []

        def capture_create_job(*args, **kwargs):
            _ = args
            captured_names.extend([p.name for p in kwargs["data_files"]])

        mock_job_manager = MagicMock()
        mock_job_manager.create_job = AsyncMock(side_effect=capture_create_job)
        mock_loaded_job = SimpleNamespace(
            id=uuid.uuid4(),
            research_question="Duplicate Uploads",
            short_title=None,
            description="Upload integration test",
            status="pending",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            max_iterations=5,
            current_iteration=0,
            pdb_code=None,
            space_group=None,
            queue_reason=None,
            stop_note=None,
            is_public=False,
        )

        app.dependency_overrides[get_session] = override_get_session
        app.dependency_overrides[get_current_user_from_api_key] = override_get_user
        app.include_router(router)

        with (
            patch(
                "openscientist.api.endpoints.jobs._get_job_manager", return_value=mock_job_manager
            ),
            patch(
                "openscientist.api.endpoints.jobs.get_job_by_id", new_callable=AsyncMock
            ) as mock_get_job,
        ):
            mock_get_job.return_value = mock_loaded_job
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/api/v1/jobs",
                    data={
                        "short_title": "Duplicate Uploads",
                        "research_question": "Do duplicate upload names collide?",
                    },
                    files=[
                        ("data_files", ("duplicate.csv", b"a,b\n1,2\n", "text/csv")),
                        ("data_files", ("duplicate.csv", b"a,b\n3,4\n", "text/csv")),
                    ],
                    headers={"Authorization": f"Bearer {full_key}"},
                )

        assert response.status_code == 201
        assert captured_names == ["duplicate.csv", "duplicate_1.csv"]

    @pytest.mark.asyncio
    async def test_create_job_rejects_file_one_chunk_over_the_limit(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_api_key_db: tuple[APIKey, str],
        test_engine: AsyncEngine,
        admin_row_reset: None,
    ):
        """A file one chunk over the limit stops with 413 and creates no job."""
        _, full_key = test_api_key_db
        await set_admin_settings_row(test_engine, max_file_size_mb=1)
        app = _build_authenticated_app(db_session, test_user_db)

        mock_job_manager = MagicMock()
        mock_job_manager.create_job = AsyncMock()

        with patch(
            "openscientist.api.endpoints.jobs._get_job_manager", return_value=mock_job_manager
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                # 1 MiB limit plus one more row of four bytes.
                content = b"a,b\n" + b"1,2\n" * 262144
                response = await client.post(
                    "/api/v1/jobs",
                    data={
                        "research_question": "Question",
                        "max_iterations": "5",
                        "investigation_mode": "autonomous",
                    },
                    files=[("data_files", ("big.csv", content, "text/csv"))],
                    headers={"Authorization": f"Bearer {full_key}"},
                )

        assert response.status_code == 413
        mock_job_manager.create_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_job_rejects_files_over_the_total_limit(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_api_key_db: tuple[APIKey, str],
        test_engine: AsyncEngine,
        admin_row_reset: None,
    ):
        """Files whose total exceeds the limit stop with 413 before any more is written."""
        _, full_key = test_api_key_db
        await set_admin_settings_row(test_engine, max_file_size_mb=1)
        app = _build_authenticated_app(db_session, test_user_db)

        mock_job_manager = MagicMock()
        mock_job_manager.create_job = AsyncMock()

        with patch(
            "openscientist.api.endpoints.jobs._get_job_manager", return_value=mock_job_manager
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                # Two files under the per-file limit, over it in total.
                response = await client.post(
                    "/api/v1/jobs",
                    data={
                        "research_question": "Question",
                        "max_iterations": "5",
                        "investigation_mode": "autonomous",
                    },
                    files=[
                        (
                            "data_files",
                            ("part1.csv", b"a,b\n1,2\n" * 87_500, "text/csv"),
                        ),
                        (
                            "data_files",
                            ("part2.csv", b"c,d\n3,4\n" * 62_500, "text/csv"),
                        ),
                    ],
                    headers={"Authorization": f"Bearer {full_key}"},
                )

        assert response.status_code == 413
        mock_job_manager.create_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_job_rejects_too_many_files(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_api_key_db: tuple[APIKey, str],
    ):
        """More files than the per-request cap stop with 413 and create no job."""
        from openscientist.api.endpoints.jobs import MAX_UPLOAD_FILES_PER_REQUEST

        _, full_key = test_api_key_db
        app = _build_authenticated_app(db_session, test_user_db)

        mock_job_manager = MagicMock()
        mock_job_manager.create_job = AsyncMock()

        with patch(
            "openscientist.api.endpoints.jobs._get_job_manager", return_value=mock_job_manager
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/api/v1/jobs",
                    data={
                        "research_question": "Question",
                        "max_iterations": "5",
                        "investigation_mode": "autonomous",
                    },
                    files=[
                        (
                            "data_files",
                            (f"file_{index:03d}.csv", b"a,b\n1,2\n", "text/csv"),
                        )
                        for index in range(MAX_UPLOAD_FILES_PER_REQUEST + 1)
                    ],
                    headers={"Authorization": f"Bearer {full_key}"},
                )

        assert response.status_code == 413
        mock_job_manager.create_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_job_returns_400_for_job_manager_value_error(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_api_key_db: tuple[APIKey, str],
    ):
        """User-correctable create errors are returned as 400 responses."""
        _, full_key = test_api_key_db
        app = _build_authenticated_app(db_session, test_user_db)

        mock_job_manager = MagicMock()
        mock_job_manager.create_job = MagicMock(side_effect=ValueError("Cannot create job: limit"))

        with patch(
            "openscientist.api.endpoints.jobs._get_job_manager", return_value=mock_job_manager
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/api/v1/jobs",
                    json={
                        "short_title": "Blocked",
                        "description": "Blocked by validation",
                        "research_question": "Question",
                        "max_iterations": 10,
                        "investigation_mode": "autonomous",
                    },
                    headers={"Authorization": f"Bearer {full_key}"},
                )

        assert response.status_code == 400
        assert "Cannot create job" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_create_job_rejected_for_unapproved_user(
        self,
        db_session: AsyncSession,
        test_api_key_db: tuple[APIKey, str],
    ):
        """Unapproved users cannot start jobs via the API."""
        from fastapi import FastAPI

        from openscientist.api.auth import get_current_user_from_api_key
        from openscientist.api.router import api_router as router
        from openscientist.database.session import get_session

        _, full_key = test_api_key_db

        app = FastAPI()

        async def override_get_session():
            await use_app_role(db_session)
            yield db_session

        unapproved_user = MagicMock(spec=User)
        unapproved_user.id = uuid.uuid4()
        unapproved_user.email = "pending@example.com"
        unapproved_user.name = "Pending User"
        unapproved_user.is_active = True
        unapproved_user.is_approved = False

        async def override_get_user():
            return unapproved_user

        app.dependency_overrides[get_session] = override_get_session
        app.dependency_overrides[get_current_user_from_api_key] = override_get_user
        app.include_router(router)

        with patch("openscientist.api.endpoints.jobs._get_job_manager") as mock_get_job_manager:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/api/v1/jobs",
                    json={
                        "short_title": "Blocked Job",
                        "description": "Should not be created",
                        "research_question": "Will this run before approval?",
                        "max_iterations": 5,
                        "investigation_mode": "autonomous",
                    },
                    headers={"Authorization": f"Bearer {full_key}"},
                )

        assert response.status_code == 403
        assert "pending administrator approval" in response.json()["detail"]
        mock_get_job_manager.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("approved", "admin", "allowed"),
        [(True, False, True), (False, True, True), (False, False, False)],
    )
    async def test_create_job_follows_the_database_start_rule(
        self, test_engine: AsyncEngine, approved: bool, admin: bool, allowed: bool
    ):
        """POST /jobs admits whoever the jobs INSERT policy admits: approved users
        and administrators, the approval flag alone deciding nothing for admins."""
        from fastapi import FastAPI

        from openscientist.api.auth import get_current_user_from_api_key
        from openscientist.api.router import api_router as router
        from openscientist.database.session import get_session

        async with AsyncSession(test_engine, expire_on_commit=False) as setup:
            user = User(email=f"starter-{uuid.uuid4().hex}@example.com", name="Starter")
            user.is_approved = approved
            setup.add(user)
            await setup.flush()
            if admin:
                setup.add(Administrator(user_id=user.id))
            await setup.commit()

        async def app_session():
            async with AsyncSession(test_engine, expire_on_commit=False) as session:
                await use_app_role(session)
                yield session

        async def current_user():
            return user

        app = FastAPI()
        app.dependency_overrides[get_session] = app_session
        app.dependency_overrides[get_current_user_from_api_key] = current_user
        app.include_router(router)
        job_manager = MagicMock()
        job_manager.create_job = MagicMock(side_effect=ValueError("stopped after the gate"))
        try:
            with patch(
                "openscientist.api.endpoints.jobs._get_job_manager", return_value=job_manager
            ):
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    response = await client.post(
                        "/api/v1/jobs",
                        json={"research_question": "May I start?", "max_iterations": 1},
                    )
        finally:
            async with AsyncSession(test_engine) as cleanup:
                await cleanup.execute(delete(Administrator).where(Administrator.user_id == user.id))
                await cleanup.execute(delete(User).where(User.id == user.id))
                await cleanup.commit()

        assert (response.status_code != 403) is allowed
        assert job_manager.create_job.called is allowed

    @pytest.mark.asyncio
    async def test_filter_jobs_by_status(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_api_key_db: tuple[APIKey, str],
        test_job_db: Job,
        completed_job_db: Job,
    ):
        """Filter jobs by status."""
        _ = (test_job_db, completed_job_db)
        from fastapi import FastAPI

        from openscientist.api.auth import get_current_user_from_api_key
        from openscientist.api.router import api_router as router
        from openscientist.database.rls import set_current_user
        from openscientist.database.session import get_session

        _, full_key = test_api_key_db

        app = FastAPI()

        async def override_get_session():
            await set_current_user(db_session, test_user_db.id)
            yield db_session

        async def override_get_user():
            return test_user_db

        app.dependency_overrides[get_session] = override_get_session
        app.dependency_overrides[get_current_user_from_api_key] = override_get_user
        app.include_router(router)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/api/v1/jobs?status=completed",
                headers={"Authorization": f"Bearer {full_key}"},
            )

        assert response.status_code == 200
        data = response.json()
        # All returned jobs should be completed
        for job in data["jobs"]:
            assert job["status"] == "completed"

    @pytest.mark.asyncio
    async def test_get_job_report_requires_completed_job(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_api_key_db: tuple[APIKey, str],
        test_job_db: Job,
    ):
        """Report endpoint requires a completed job."""
        from fastapi import FastAPI

        from openscientist.api.auth import get_current_user_from_api_key
        from openscientist.api.router import api_router as router
        from openscientist.database.rls import set_current_user
        from openscientist.database.session import get_session

        _, full_key = test_api_key_db

        app = FastAPI()

        async def override_get_session():
            await set_current_user(db_session, test_user_db.id)
            yield db_session

        async def override_get_user():
            return test_user_db

        app.dependency_overrides[get_session] = override_get_session
        app.dependency_overrides[get_current_user_from_api_key] = override_get_user
        app.include_router(router)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                f"/api/v1/jobs/{test_job_db.id}/report",
                headers={"Authorization": f"Bearer {full_key}"},
            )

        # Pending job should not have a report
        assert response.status_code == 400
        assert "completed" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_get_job_report_completed_job(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_api_key_db: tuple[APIKey, str],
        completed_job_db: Job,
        tmp_path,
    ):
        """Report endpoint returns report for completed job."""
        from fastapi import FastAPI

        from openscientist.api.auth import get_current_user_from_api_key
        from openscientist.api.router import api_router as router
        from openscientist.database.rls import set_current_user
        from openscientist.database.session import get_session

        _, full_key = test_api_key_db

        # Create job directory with report file
        job_dir = tmp_path / "jobs" / str(completed_job_db.id)
        job_dir.mkdir(parents=True)
        report_file = job_dir / "final_report.md"
        report_file.write_text("# Test Report\n\nThis is a test report.")

        app = FastAPI()

        async def override_get_session():
            await set_current_user(db_session, test_user_db.id)
            yield db_session

        async def override_get_user():
            return test_user_db

        app.dependency_overrides[get_session] = override_get_session
        app.dependency_overrides[get_current_user_from_api_key] = override_get_user
        app.include_router(router)

        with patch(
            "openscientist.api.endpoints.jobs._get_jobs_dir", return_value=tmp_path / "jobs"
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.get(
                    f"/api/v1/jobs/{completed_job_db.id}/report",
                    headers={"Authorization": f"Bearer {full_key}"},
                )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_get_job_report_uses_job_manager_jobs_dir(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_api_key_db: tuple[APIKey, str],
        completed_job_db: Job,
        tmp_path,
    ):
        """Report endpoint should read artifacts from configured JobManager jobs_dir."""
        from fastapi import FastAPI

        from openscientist.api.auth import get_current_user_from_api_key
        from openscientist.api.router import api_router as router
        from openscientist.database.rls import set_current_user
        from openscientist.database.session import get_session

        _, full_key = test_api_key_db

        custom_jobs_dir = tmp_path / "custom-jobs"
        job_dir = custom_jobs_dir / str(completed_job_db.id)
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "final_report.md").write_text("# Report\n\nCustom dir report", encoding="utf-8")

        app = FastAPI()

        async def override_get_session():
            await set_current_user(db_session, test_user_db.id)
            yield db_session

        async def override_get_user():
            return test_user_db

        mock_job_manager = MagicMock()
        mock_job_manager.jobs_dir = custom_jobs_dir

        app.dependency_overrides[get_session] = override_get_session
        app.dependency_overrides[get_current_user_from_api_key] = override_get_user
        app.include_router(router)

        with patch(
            "openscientist.api.endpoints.jobs._get_job_manager", return_value=mock_job_manager
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.get(
                    f"/api/v1/jobs/{completed_job_db.id}/report",
                    headers={"Authorization": f"Bearer {full_key}"},
                )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_get_job_artifacts_not_found(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_api_key_db: tuple[APIKey, str],
        test_job_db: Job,
    ):
        """Artifacts endpoint returns 404 if job directory doesn't exist."""
        _, full_key = test_api_key_db

        app = _build_authenticated_app(db_session, test_user_db)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                f"/api/v1/jobs/{test_job_db.id}/artifacts",
                headers={"Authorization": f"Bearer {full_key}"},
            )

        # Job directory doesn't exist
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_job_artifacts_success(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_api_key_db: tuple[APIKey, str],
        test_job_db: Job,
        tmp_path,
    ):
        """Artifacts endpoint returns ZIP archive."""
        _, full_key = test_api_key_db

        # Create job directory with some files
        job_dir = tmp_path / "jobs" / str(test_job_db.id)
        job_dir.mkdir(parents=True)
        (job_dir / "plot.png").write_bytes(b"fake png data")
        (job_dir / "data.csv").write_text("a,b,c\n1,2,3\n")
        test_job_db.version_info = {"openscientist_commit": "abc123def456"}
        await db_session.commit()

        app = _build_authenticated_app(db_session, test_user_db)

        with patch(
            "openscientist.api.endpoints.jobs._get_jobs_dir", return_value=tmp_path / "jobs"
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.get(
                    f"/api/v1/jobs/{test_job_db.id}/artifacts",
                    headers={"Authorization": f"Bearer {full_key}"},
                )

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert response.headers.get("accept-ranges") == "bytes"
        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            assert json.loads(zf.read("provenance.json")) == {
                "openscientist_commit": "abc123def456"
            }

    @pytest.mark.asyncio
    async def test_get_job_artifacts_excludes_internal_runtime_files(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_api_key_db: tuple[APIKey, str],
        test_job_db: Job,
        tmp_path,
    ):
        """Artifacts endpoint should not include internal runtime files."""
        _, full_key = test_api_key_db

        job_dir = tmp_path / "jobs" / str(test_job_db.id)
        job_dir.mkdir(parents=True)
        (job_dir / "config.json").write_text('{"legacy": true}', encoding="utf-8")
        (job_dir / ".codex").mkdir()
        (job_dir / ".codex" / "config.toml").write_text(
            'OPENSCIENTIST_SECRET_KEY = "placeholder"', encoding="utf-8"
        )
        (job_dir / ".codex" / "auth.json").write_text('{"tokens": "placeholder"}', encoding="utf-8")
        (job_dir / "report.md").write_text("# Report", encoding="utf-8")

        app = _build_authenticated_app(db_session, test_user_db)

        with patch(
            "openscientist.api.endpoints.jobs._get_jobs_dir", return_value=tmp_path / "jobs"
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.get(
                    f"/api/v1/jobs/{test_job_db.id}/artifacts",
                    headers={"Authorization": f"Bearer {full_key}"},
                )

        assert response.status_code == 200

        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            names = zf.namelist()
            assert "report.md" in names
            assert "config.json" not in names
            assert not any(name == ".codex" or name.startswith(".codex/") for name in names)

    @pytest.mark.asyncio
    async def test_get_job_artifacts_uses_job_manager_jobs_dir(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_api_key_db: tuple[APIKey, str],
        test_job_db: Job,
        tmp_path,
    ):
        """Artifacts endpoint should zip files from configured JobManager jobs_dir."""
        _, full_key = test_api_key_db

        custom_jobs_dir = tmp_path / "custom-jobs"
        job_dir = custom_jobs_dir / str(test_job_db.id)
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "plot.png").write_bytes(b"fake png data")
        (job_dir / "data.csv").write_text("a,b,c\n1,2,3\n", encoding="utf-8")

        app = _build_authenticated_app(db_session, test_user_db)
        mock_job_manager = MagicMock()
        mock_job_manager.jobs_dir = custom_jobs_dir

        with patch(
            "openscientist.api.endpoints.jobs._get_job_manager", return_value=mock_job_manager
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.get(
                    f"/api/v1/jobs/{test_job_db.id}/artifacts",
                    headers={"Authorization": f"Bearer {full_key}"},
                )

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert response.headers.get("accept-ranges") == "bytes"

    @pytest.mark.asyncio
    async def test_get_job_artifacts_offloads_zip_build_to_threadpool(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_api_key_db: tuple[APIKey, str],
        test_job_db: Job,
        tmp_path,
    ):
        _, full_key = test_api_key_db

        job_dir = tmp_path / "jobs" / str(test_job_db.id)
        job_dir.mkdir(parents=True)
        (job_dir / "report.md").write_text("# Report")

        app = _build_authenticated_app(db_session, test_user_db)

        with (
            patch("openscientist.api.endpoints.jobs._get_jobs_dir", return_value=tmp_path / "jobs"),
            patch(
                "openscientist.api.endpoints.jobs.run_in_threadpool",
                wraps=lambda func, **kwargs: func(**kwargs),
            ) as mock_run_in_threadpool,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.get(
                    f"/api/v1/jobs/{test_job_db.id}/artifacts",
                    headers={"Authorization": f"Bearer {full_key}"},
                )

        assert response.status_code == 200
        mock_run_in_threadpool.assert_called_once()


class TestJobSharingEndpoints:
    """Tests for job sharing endpoints."""

    @pytest.mark.asyncio
    async def test_share_job_with_user(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_user2_db: User,
        test_api_key_db: tuple[APIKey, str],
        test_job_db: Job,
    ):
        """Share a job with another user."""
        from contextlib import asynccontextmanager
        from unittest.mock import patch

        from fastapi import FastAPI

        from openscientist.api.auth import get_current_user_from_api_key
        from openscientist.api.router import api_router as router
        from openscientist.database.rls import set_current_user
        from openscientist.database.session import get_session

        _, full_key = test_api_key_db

        app = FastAPI()

        async def override_get_session():
            await set_current_user(db_session, test_user_db.id)
            yield db_session

        async def override_get_user():
            return test_user_db

        @asynccontextmanager
        async def mock_get_admin_session():
            """Mock admin session that uses test db_session."""
            yield db_session

        app.dependency_overrides[get_session] = override_get_session
        app.dependency_overrides[get_current_user_from_api_key] = override_get_user
        app.include_router(router)

        with patch("openscientist.api.endpoints.shares.get_admin_session", mock_get_admin_session):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/api/v1/shares",
                    json={
                        "job_id": str(test_job_db.id),
                        "shared_with_email": test_user2_db.email,
                        "permission_level": "view",
                    },
                    headers={"Authorization": f"Bearer {full_key}"},
                )

        assert response.status_code == 201, (
            f"Expected 201, got {response.status_code}: {response.json()}"
        )
        data = response.json()
        assert data["job_id"] == str(test_job_db.id)
        assert data["permission_level"] == "view"

    @staticmethod
    def _lookup_app(
        db_session: AsyncSession,
        test_user_db: User,
    ):
        """An app wired so the RLS session runs as openscientist_app for the owner."""
        from fastapi import FastAPI

        from openscientist.api.auth import get_current_user_from_api_key
        from openscientist.api.router import api_router as router
        from openscientist.database.rls import set_current_user
        from openscientist.database.session import get_session

        app = FastAPI()

        async def override_get_session():
            await db_session.execute(text("SET ROLE openscientist_app"))
            await set_current_user(db_session, test_user_db.id)
            try:
                yield db_session
            finally:
                await db_session.execute(text("RESET ROLE"))

        async def override_get_user():
            return test_user_db

        app.dependency_overrides[get_session] = override_get_session
        app.dependency_overrides[get_current_user_from_api_key] = override_get_user
        app.include_router(router)
        return app

    @pytest.mark.asyncio
    async def test_lookup_share_target_exact_email_returns_id_and_name_only(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_user2_db: User,
        test_api_key_db: tuple[APIKey, str],
    ):
        """An exact email in any case, with surrounding spaces, returns that one user."""
        _, full_key = test_api_key_db
        app = self._lookup_app(db_session, test_user_db)

        @asynccontextmanager
        async def mock_get_admin_session():
            await db_session.execute(text("SET ROLE openscientist_admin"))
            try:
                yield db_session
            finally:
                await db_session.execute(text("RESET ROLE"))

        with patch("openscientist.api.endpoints.shares.get_admin_session", mock_get_admin_session):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.get(
                    "/api/v1/shares/lookup",
                    params={"email": f"  {test_user2_db.email.upper()}  "},
                    headers={"Authorization": f"Bearer {full_key}"},
                )

        assert response.status_code == 200, response.text
        assert response.json() == {
            "target": {"id": str(test_user2_db.id), "name": test_user2_db.name}
        }

    @pytest.mark.asyncio
    async def test_lookup_share_target_rejects_wildcards_and_partial_strings(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_user2_db: User,
        test_api_key_db: tuple[APIKey, str],
    ):
        """LIKE wildcards and partial strings match nobody, in either order."""
        _, full_key = test_api_key_db
        app = self._lookup_app(db_session, test_user_db)

        @asynccontextmanager
        async def mock_get_admin_session():
            await db_session.execute(text("SET ROLE openscientist_admin"))
            try:
                yield db_session
            finally:
                await db_session.execute(text("RESET ROLE"))

        emails = (
            "%%",
            "%",
            "_",
            "%_",
            test_user2_db.email[:5],
            f"%{test_user2_db.email}",
            f"{test_user2_db.email}%",
        )
        with patch("openscientist.api.endpoints.shares.get_admin_session", mock_get_admin_session):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                for email in emails:
                    response = await client.get(
                        "/api/v1/shares/lookup",
                        params={"email": email},
                        headers={"Authorization": f"Bearer {full_key}"},
                    )
                    assert response.status_code == 200, response.text
                    assert response.json() == {"target": None}

    @pytest.mark.asyncio
    async def test_lookup_share_target_no_match_looks_like_invalid_input(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_user2_db: User,
        test_api_key_db: tuple[APIKey, str],
    ):
        """A missing email and a malformed one return the same empty result."""
        _, full_key = test_api_key_db
        app = self._lookup_app(db_session, test_user_db)

        @asynccontextmanager
        async def mock_get_admin_session():
            await db_session.execute(text("SET ROLE openscientist_admin"))
            try:
                yield db_session
            finally:
                await db_session.execute(text("RESET ROLE"))

        with patch("openscientist.api.endpoints.shares.get_admin_session", mock_get_admin_session):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                missing = await client.get(
                    "/api/v1/shares/lookup",
                    params={"email": "nobody@example.invalid"},
                    headers={"Authorization": f"Bearer {full_key}"},
                )
                invalid = await client.get(
                    "/api/v1/shares/lookup",
                    params={"email": "%%"},
                    headers={"Authorization": f"Bearer {full_key}"},
                )

        assert missing.status_code == invalid.status_code == 200
        assert missing.json() == invalid.json() == {"target": None}

    @pytest.mark.asyncio
    async def test_lookup_share_target_refuses_unapproved_caller(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_user2_db: User,
        test_api_key_db: tuple[APIKey, str],
    ):
        """A signed-in account without approval or admin status gets 403."""
        _, full_key = test_api_key_db
        test_user_db.is_approved = False
        await db_session.commit()
        app = self._lookup_app(db_session, test_user_db)

        @asynccontextmanager
        async def mock_get_admin_session():
            await db_session.execute(text("SET ROLE openscientist_admin"))
            try:
                yield db_session
            finally:
                await db_session.execute(text("RESET ROLE"))

        with patch("openscientist.api.endpoints.shares.get_admin_session", mock_get_admin_session):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.get(
                    "/api/v1/shares/lookup",
                    params={"email": test_user2_db.email},
                    headers={"Authorization": f"Bearer {full_key}"},
                )

        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_share_job_with_inactive_user_returns_404(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_user2_db: User,
        test_api_key_db: tuple[APIKey, str],
        test_job_db: Job,
    ):
        """Sharing to an inactive user should be rejected as not found."""
        from contextlib import asynccontextmanager
        from unittest.mock import patch

        from fastapi import FastAPI
        from sqlalchemy import select

        from openscientist.api.auth import get_current_user_from_api_key
        from openscientist.api.router import api_router as router
        from openscientist.database.models import JobShare
        from openscientist.database.rls import set_current_user
        from openscientist.database.session import get_session

        _, full_key = test_api_key_db

        test_user2_db.is_active = False
        await db_session.commit()
        await db_session.refresh(test_user2_db)

        app = FastAPI()

        async def override_get_session():
            await set_current_user(db_session, test_user_db.id)
            yield db_session

        async def override_get_user():
            return test_user_db

        @asynccontextmanager
        async def mock_get_admin_session():
            yield db_session

        app.dependency_overrides[get_session] = override_get_session
        app.dependency_overrides[get_current_user_from_api_key] = override_get_user
        app.include_router(router)

        with patch("openscientist.api.endpoints.shares.get_admin_session", mock_get_admin_session):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/api/v1/shares",
                    json={
                        "job_id": str(test_job_db.id),
                        "shared_with_email": test_user2_db.email,
                        "permission_level": "view",
                    },
                    headers={"Authorization": f"Bearer {full_key}"},
                )

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

        share_result = await db_session.execute(
            select(JobShare).where(JobShare.job_id == test_job_db.id)
        )
        assert share_result.scalar_one_or_none() is None

    @pytest.mark.asyncio
    async def test_list_job_shares(
        self, test_engine: AsyncEngine, committed_share_owner: tuple[User, Job]
    ):
        """List shares for a job."""

        from fastapi import FastAPI

        from openscientist.api.auth import get_current_user_from_api_key
        from openscientist.api.router import api_router as router
        from openscientist.database.models import JobShare

        owner, job = committed_share_owner

        # The endpoint resolves shared-with users through its own admin
        # session, so the target must be committed, like a real user row.
        async with AsyncSession(test_engine, expire_on_commit=False) as target_db:
            target = User(email=f"share-list-{uuid.uuid4().hex}@share.test", name="Share Target")
            target_db.add(target)
            await target_db.commit()
            await target_db.refresh(target)

        try:
            async with AsyncSession(test_engine, expire_on_commit=False) as share_db:
                share_db.add(
                    JobShare(job_id=job.id, shared_with_user_id=target.id, permission_level="view")
                )
                await share_db.commit()

            app = FastAPI()

            async def override_get_user():
                return owner

            app.dependency_overrides[get_current_user_from_api_key] = override_get_user
            app.include_router(router)

            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.get(
                    f"/api/v1/shares/job/{job.id}",
                    headers={"Authorization": "Bearer test"},
                )

            assert response.status_code == 200
            data = response.json()
            assert "shares" in data
            assert data["total"] >= 1
            # Verify the share we created is in the list
            share_emails = [s["shared_with_email"] for s in data["shares"]]
            assert target.email in share_emails
        finally:
            async with AsyncSession(test_engine, expire_on_commit=False) as target_db:
                await target_db.execute(delete(User).where(User.id == target.id))
                await target_db.commit()

    @pytest.mark.asyncio
    async def test_list_job_shares_not_owner_forbidden(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_user2_db: User,
        test_api_key_db: tuple[APIKey, str],
    ):
        """Non-owner cannot list shares for a job."""
        from fastapi import FastAPI

        from openscientist.api.auth import get_current_user_from_api_key
        from openscientist.api.router import api_router as router
        from openscientist.database.rls import set_current_user
        from openscientist.database.session import get_session

        _, full_key = test_api_key_db

        # Create a job owned by user2
        other_job = Job(
            owner_id=test_user2_db.id,
            research_question="Other User's Job",
            status="pending",
        )
        db_session.add(other_job)
        await db_session.commit()
        await db_session.refresh(other_job)

        # Enable RLS
        await enable_rls(db_session)

        app = FastAPI()

        async def override_get_session():
            await set_current_user(db_session, test_user_db.id)
            yield db_session

        async def override_get_user():
            return test_user_db  # Authenticated as user1

        app.dependency_overrides[get_session] = override_get_session
        app.dependency_overrides[get_current_user_from_api_key] = override_get_user
        app.include_router(router)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                f"/api/v1/shares/job/{other_job.id}",
                headers={"Authorization": f"Bearer {full_key}"},
            )

        # Should be 403 or 404 (can't see job or forbidden to list shares)
        assert response.status_code in [403, 404]

    @pytest.mark.asyncio
    async def test_revoke_share(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_user2_db: User,
        test_api_key_db: tuple[APIKey, str],
        test_job_db: Job,
    ):
        """Revoke a job share."""
        from fastapi import FastAPI

        from openscientist.api.auth import get_current_user_from_api_key
        from openscientist.api.router import api_router as router
        from openscientist.database.models import JobShare
        from openscientist.database.rls import set_current_user
        from openscientist.database.session import get_session

        _, full_key = test_api_key_db

        # Create a share to revoke
        share = JobShare(
            job_id=test_job_db.id,
            shared_with_user_id=test_user2_db.id,
            permission_level="view",
        )
        db_session.add(share)
        await db_session.commit()
        await db_session.refresh(share)
        share_id = share.id

        app = FastAPI()

        async def override_get_session():
            await set_current_user(db_session, test_user_db.id)
            yield db_session

        async def override_get_user():
            return test_user_db

        app.dependency_overrides[get_session] = override_get_session
        app.dependency_overrides[get_current_user_from_api_key] = override_get_user
        app.include_router(router)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.delete(
                f"/api/v1/shares/{share_id}",
                headers={"Authorization": f"Bearer {full_key}"},
            )

        assert response.status_code == 204

    @pytest.mark.asyncio
    async def test_revoke_share_not_owner_forbidden(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_user2_db: User,
        test_api_key_db: tuple[APIKey, str],
    ):
        """Non-owner cannot revoke a share."""
        from fastapi import FastAPI

        from openscientist.api.auth import get_current_user_from_api_key
        from openscientist.api.router import api_router as router
        from openscientist.database.models import JobShare
        from openscientist.database.rls import set_current_user
        from openscientist.database.session import get_session

        _, full_key = test_api_key_db

        # Create a job owned by user2
        other_job = Job(
            owner_id=test_user2_db.id,
            research_question="Other User's Job",
            status="pending",
        )
        db_session.add(other_job)
        await db_session.commit()
        await db_session.refresh(other_job)

        # Create a share on that job
        share = JobShare(
            job_id=other_job.id,
            shared_with_user_id=test_user_db.id,  # Shared with user1
            permission_level="view",
        )
        db_session.add(share)
        await db_session.commit()
        await db_session.refresh(share)

        app = FastAPI()

        async def override_get_session():
            await set_current_user(db_session, test_user_db.id)
            yield db_session

        async def override_get_user():
            return test_user_db  # User1 trying to revoke

        app.dependency_overrides[get_session] = override_get_session
        app.dependency_overrides[get_current_user_from_api_key] = override_get_user
        app.include_router(router)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.delete(
                f"/api/v1/shares/{share.id}",
                headers={"Authorization": f"Bearer {full_key}"},
            )

        # Should be 403 forbidden (can see the share but can't revoke it)
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_revoke_share_invalid_uuid_returns_400(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_api_key_db: tuple[APIKey, str],
    ):
        """Malformed share IDs should return client errors, not 500s."""
        from fastapi import FastAPI

        from openscientist.api.auth import get_current_user_from_api_key
        from openscientist.api.router import api_router as router
        from openscientist.database.rls import set_current_user
        from openscientist.database.session import get_session

        _, full_key = test_api_key_db

        app = FastAPI()

        async def override_get_session():
            await set_current_user(db_session, test_user_db.id)
            yield db_session

        async def override_get_user():
            return test_user_db

        app.dependency_overrides[get_session] = override_get_session
        app.dependency_overrides[get_current_user_from_api_key] = override_get_user
        app.include_router(router)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.delete(
                "/api/v1/shares/not-a-uuid",
                headers={"Authorization": f"Bearer {full_key}"},
            )

        assert response.status_code == 400
        assert "Invalid share_id format" in response.json()["detail"]


class TestAuthenticationFlow:
    """Tests for full authentication flow."""

    @pytest.mark.asyncio
    async def test_invalid_api_key_format(
        self,
        db_session: AsyncSession,
    ):
        """Invalid API key format returns 401."""
        from fastapi import FastAPI

        from openscientist.api.router import api_router as router
        from openscientist.database.session import get_session

        app = FastAPI()

        async def override_get_session():
            yield db_session

        app.dependency_overrides[get_session] = override_get_session
        app.include_router(router)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/api/v1/jobs",
                headers={"Authorization": "Bearer invalid-no-colon"},
            )

        assert response.status_code == 401
        assert "Invalid API key format" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_nonexistent_api_key(
        self,
        db_session: AsyncSession,
    ):
        """Non-existent API key returns 401."""
        from fastapi import FastAPI

        from openscientist.api import auth as api_auth
        from openscientist.api.router import api_router as router
        from openscientist.database.session import get_session

        app = FastAPI()

        async def override_get_session():
            yield db_session

        @asynccontextmanager
        async def override_get_admin_session():
            yield db_session

        app.dependency_overrides[get_session] = override_get_session
        app.include_router(router)
        with patch.object(api_auth, "get_admin_session", override_get_admin_session):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.get(
                    "/api/v1/jobs",
                    headers={"Authorization": "Bearer nonexistent:wrongsecret"},
                )

        assert response.status_code == 401


@pytest.mark.asyncio
async def test_persist_uploaded_files_stays_at_chunk_size_in_memory(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """A large upload under the cap holds at most its chunks in memory and lands byte-identical."""
    from starlette.datastructures import UploadFile

    from openscientist.api.endpoints.jobs import _persist_uploaded_files

    source = tmp_path / "source.csv"
    source.write_bytes(b"a,b\n" + b"1,2\n" * 8 * 1024 * 1024)
    upload = UploadFile(file=source.open("rb"), filename="source.csv")
    with tempfile.TemporaryDirectory() as temp_dir:
        tracemalloc.start()
        persisted = await _persist_uploaded_files([upload], Path(temp_dir), 1000 * 1024 * 1024)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert len(persisted) == 1
        assert persisted[0].read_bytes() == source.read_bytes()
    assert peak < source.stat().st_size // 2


@pytest.mark.asyncio
async def test_persist_uploaded_files_deletes_the_partial_file(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """An upload that stops over its limit raises FileTooBigError and leaves no file behind."""
    from starlette.datastructures import UploadFile

    from openscientist.api.endpoints.jobs import _persist_uploaded_files

    source = tmp_path / "source.bin"
    source.write_bytes(b"x" * (1024 * 1024 + 16))
    upload = UploadFile(file=source.open("rb"), filename="big.csv")
    with pytest.raises(FileTooBigError):
        await _persist_uploaded_files([upload], tmp_path, 1024 * 1024)

    assert not (tmp_path / "big.csv").exists()
    assert source.exists()
