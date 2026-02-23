"""
HTTP endpoint tests for the REST API.

Tests actual HTTP requests to API endpoints with mocked authentication.
"""

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.api.auth import hash_secret
from shandy.database.models import APIKey, Job, User
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
async def test_job_db(
    db_session: AsyncSession,
    test_user_db: User,
) -> Job:
    """Create a real job in the test database."""
    job = Job(
        owner_id=test_user_db.id,
        title="Test API Job",
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
        title="Completed Test Job",
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


class TestHealthEndpoint:
    """Tests for the health check endpoint."""

    @pytest.mark.asyncio
    async def test_health_check(self):
        """Health check returns ok status."""
        # Create a minimal FastAPI app for testing
        from fastapi import FastAPI

        from shandy.api.router import api_router as router

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
        assert data["api"] == "shandy"


class TestAPIKeyEndpoints:
    """Tests for API key management endpoints."""

    @pytest.mark.asyncio
    async def test_create_api_key_requires_auth(self):
        """Creating an API key requires authentication."""
        from fastapi import FastAPI

        from shandy.api.router import api_router as router

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

        from shandy.api.auth import get_current_user_from_api_key
        from shandy.api.router import api_router as router
        from shandy.database.session import get_session

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

        from shandy.api.auth import get_current_user_from_api_key
        from shandy.api.router import api_router as router
        from shandy.database.session import get_session

        api_key, full_key = test_api_key_db

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

        from shandy.api.auth import get_current_user_from_api_key
        from shandy.api.router import api_router as router
        from shandy.database.session import get_session

        api_key, full_key = test_api_key_db

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

        from shandy.api.auth import get_current_user_from_api_key
        from shandy.api.router import api_router as router
        from shandy.database.session import get_session

        api_key, full_key = test_api_key_db

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

    @pytest.mark.asyncio
    async def test_list_jobs(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_api_key_db: tuple[APIKey, str],
        test_job_db: Job,
    ):
        """List jobs for authenticated user."""
        from fastapi import FastAPI

        from shandy.api.auth import get_current_user_from_api_key
        from shandy.api.router import api_router as router
        from shandy.database.rls import set_current_user
        from shandy.database.session import get_session

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

        from shandy.api.auth import get_current_user_from_api_key
        from shandy.api.router import api_router as router
        from shandy.database.rls import set_current_user
        from shandy.database.session import get_session

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
                f"/api/v1/jobs/{test_job_db.id}",
                headers={"Authorization": f"Bearer {full_key}"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(test_job_db.id)
        assert data["title"] == "Test API Job"
        assert data["status"] == "pending"

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

        from shandy.api.auth import get_current_user_from_api_key
        from shandy.api.router import api_router as router
        from shandy.database.rls import set_current_user
        from shandy.database.session import get_session

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
                f"/api/v1/jobs/{test_job_db.id}/status",
                headers={"Authorization": f"Bearer {full_key}"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(test_job_db.id)
        assert data["status"] == "pending"
        assert data["current_iteration"] == 0
        assert data["max_iterations"] == 5

    @pytest.mark.asyncio
    async def test_get_nonexistent_job_returns_404(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_api_key_db: tuple[APIKey, str],
    ):
        """Getting a non-existent job returns 404."""
        from fastapi import FastAPI

        from shandy.api.auth import get_current_user_from_api_key
        from shandy.api.router import api_router as router
        from shandy.database.rls import set_current_user
        from shandy.database.session import get_session

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

        from shandy.api.auth import get_current_user_from_api_key
        from shandy.api.router import api_router as router
        from shandy.database.rls import set_current_user
        from shandy.database.session import get_session

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
        assert "Invalid job ID format" in response.json()["detail"]

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

        from shandy.api.auth import get_current_user_from_api_key
        from shandy.api.router import api_router as router
        from shandy.database.rls import set_current_user
        from shandy.database.session import get_session

        _, full_key = test_api_key_db

        # Create job for user2
        other_job = Job(
            owner_id=test_user2_db.id,
            title="Other User's Job",
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
    async def test_cancel_job(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_api_key_db: tuple[APIKey, str],
    ):
        """Cancel a running job."""
        from fastapi import FastAPI

        from shandy.api.auth import get_current_user_from_api_key
        from shandy.api.router import api_router as router
        from shandy.database.rls import set_current_user
        from shandy.database.session import get_session

        _, full_key = test_api_key_db

        # Create a running job to cancel
        running_job = Job(
            owner_id=test_user_db.id,
            title="Running Job",
            description="Will be cancelled",
            status="running",
        )
        db_session.add(running_job)
        await db_session.commit()
        await db_session.refresh(running_job)

        app = FastAPI()

        async def override_get_session():
            await set_current_user(db_session, test_user_db.id)
            yield db_session

        async def override_get_user():
            return test_user_db

        # Mock the job manager
        mock_job_manager = MagicMock()
        mock_job_manager.cancel_job = MagicMock()

        app.dependency_overrides[get_session] = override_get_session
        app.dependency_overrides[get_current_user_from_api_key] = override_get_user
        app.include_router(router)

        with patch("shandy.api.endpoints.jobs._get_job_manager", return_value=mock_job_manager):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    f"/api/v1/jobs/{running_job.id}/cancel",
                    headers={"Authorization": f"Bearer {full_key}"},
                )

        assert response.status_code == 204
        mock_job_manager.cancel_job.assert_called_once()

    @pytest.mark.asyncio
    async def test_cannot_cancel_completed_job(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_api_key_db: tuple[APIKey, str],
        completed_job_db: Job,
    ):
        """Cannot cancel a completed job."""
        from fastapi import FastAPI

        from shandy.api.auth import get_current_user_from_api_key
        from shandy.api.router import api_router as router
        from shandy.database.rls import set_current_user
        from shandy.database.session import get_session

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
        from datetime import datetime, timezone
        from types import SimpleNamespace

        from fastapi import FastAPI

        from shandy.api.auth import get_current_user_from_api_key
        from shandy.api.router import api_router as router
        from shandy.database.rls import set_current_user
        from shandy.database.session import get_session

        _, full_key = test_api_key_db

        app = FastAPI()

        async def override_get_session():
            await set_current_user(db_session, test_user_db.id)
            yield db_session

        async def override_get_user():
            return test_user_db

        # Mock job manager
        mock_job_manager = MagicMock()
        mock_job_manager.create_job = MagicMock()
        mock_loaded_job = SimpleNamespace(
            id=uuid.uuid4(),
            title="API Created Job",
            description="Created via REST API",
            status="pending",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            max_iterations=10,
            current_iteration=0,
            pdb_code=None,
            space_group=None,
        )

        app.dependency_overrides[get_session] = override_get_session
        app.dependency_overrides[get_current_user_from_api_key] = override_get_user
        app.include_router(router)

        with (
            patch("shandy.api.endpoints.jobs._get_job_manager", return_value=mock_job_manager),
            patch(
                "shandy.api.endpoints.jobs.get_job_by_id", new_callable=AsyncMock
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
                        "title": "API Created Job",
                        "description": "Created via REST API",
                        "research_question": "What is the structure of protein X?",
                        "max_iterations": 10,
                        "use_skills": True,
                        "investigation_mode": "autonomous",
                    },
                    headers={"Authorization": f"Bearer {full_key}"},
                )

        assert response.status_code == 201
        data = response.json()
        assert data["title"] == "API Created Job"
        assert data["status"] == "pending"
        mock_job_manager.create_job.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_job_rejected_for_unapproved_user(
        self,
        db_session: AsyncSession,
        test_api_key_db: tuple[APIKey, str],
    ):
        """Unapproved users cannot start jobs via the API."""
        from fastapi import FastAPI

        from shandy.api.auth import get_current_user_from_api_key
        from shandy.api.router import api_router as router
        from shandy.database.session import get_session

        _, full_key = test_api_key_db

        app = FastAPI()

        async def override_get_session():
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

        with patch("shandy.api.endpoints.jobs._get_job_manager") as mock_get_job_manager:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/api/v1/jobs",
                    json={
                        "title": "Blocked Job",
                        "description": "Should not be created",
                        "research_question": "Will this run before approval?",
                        "max_iterations": 5,
                        "use_skills": True,
                        "investigation_mode": "autonomous",
                    },
                    headers={"Authorization": f"Bearer {full_key}"},
                )

        assert response.status_code == 403
        assert "pending administrator approval" in response.json()["detail"]
        mock_get_job_manager.assert_not_called()

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
        from fastapi import FastAPI

        from shandy.api.auth import get_current_user_from_api_key
        from shandy.api.router import api_router as router
        from shandy.database.rls import set_current_user
        from shandy.database.session import get_session

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

        from shandy.api.auth import get_current_user_from_api_key
        from shandy.api.router import api_router as router
        from shandy.database.rls import set_current_user
        from shandy.database.session import get_session

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

        from shandy.api.auth import get_current_user_from_api_key
        from shandy.api.router import api_router as router
        from shandy.database.rls import set_current_user
        from shandy.database.session import get_session

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

        with patch("shandy.api.endpoints.jobs.Path") as mock_path:
            # Make Path("jobs") / job_id return our tmp_path
            mock_path.return_value.__truediv__ = lambda self, x: tmp_path / "jobs" / x

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
        from fastapi import FastAPI

        from shandy.api.auth import get_current_user_from_api_key
        from shandy.api.router import api_router as router
        from shandy.database.rls import set_current_user
        from shandy.database.session import get_session

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
        from fastapi import FastAPI

        from shandy.api.auth import get_current_user_from_api_key
        from shandy.api.router import api_router as router
        from shandy.database.rls import set_current_user
        from shandy.database.session import get_session

        _, full_key = test_api_key_db

        # Create job directory with some files
        job_dir = tmp_path / "jobs" / str(test_job_db.id)
        job_dir.mkdir(parents=True)
        (job_dir / "plot.png").write_bytes(b"fake png data")
        (job_dir / "data.csv").write_text("a,b,c\n1,2,3\n")

        app = FastAPI()

        async def override_get_session():
            await set_current_user(db_session, test_user_db.id)
            yield db_session

        async def override_get_user():
            return test_user_db

        app.dependency_overrides[get_session] = override_get_session
        app.dependency_overrides[get_current_user_from_api_key] = override_get_user
        app.include_router(router)

        with patch("shandy.api.endpoints.jobs.Path") as mock_path:
            # Make Path("jobs") / job_id return our tmp_path structure
            def mock_truediv(self, x):
                return tmp_path / "jobs" / x

            mock_path.return_value.__truediv__ = mock_truediv

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

        from shandy.api.auth import get_current_user_from_api_key
        from shandy.api.router import api_router as router
        from shandy.database.rls import set_current_user
        from shandy.database.session import get_session

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

        with patch("shandy.api.endpoints.shares.get_admin_session", mock_get_admin_session):
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

    @pytest.mark.asyncio
    async def test_search_users_for_sharing(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_user2_db: User,
        test_api_key_db: tuple[APIKey, str],
    ):
        """Search for users to share with."""
        from contextlib import asynccontextmanager
        from unittest.mock import patch

        from fastapi import FastAPI

        from shandy.api.auth import get_current_user_from_api_key
        from shandy.api.router import api_router as router
        from shandy.database.rls import set_current_user
        from shandy.database.session import get_session

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

        with patch("shandy.api.endpoints.shares.get_admin_session", mock_get_admin_session):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.get(
                    f"/api/v1/shares/search/users?q={test_user2_db.email[:5]}",
                    headers={"Authorization": f"Bearer {full_key}"},
                )

        assert response.status_code == 200
        data = response.json()
        assert "users" in data

    @pytest.mark.asyncio
    async def test_list_job_shares(
        self,
        db_session: AsyncSession,
        test_user_db: User,
        test_user2_db: User,
        test_api_key_db: tuple[APIKey, str],
        test_job_db: Job,
    ):
        """List shares for a job."""

        from fastapi import FastAPI

        from shandy.api.auth import get_current_user_from_api_key
        from shandy.api.router import api_router as router
        from shandy.database.models import JobShare
        from shandy.database.rls import set_current_user
        from shandy.database.session import get_session

        _, full_key = test_api_key_db

        # Create a share first
        share = JobShare(
            job_id=test_job_db.id,
            shared_with_user_id=test_user2_db.id,
            permission_level="view",
        )
        db_session.add(share)
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
                f"/api/v1/shares/job/{test_job_db.id}",
                headers={"Authorization": f"Bearer {full_key}"},
            )

        assert response.status_code == 200
        data = response.json()
        assert "shares" in data
        assert data["total"] >= 1
        # Verify the share we created is in the list
        share_emails = [s["shared_with_email"] for s in data["shares"]]
        assert test_user2_db.email in share_emails

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

        from shandy.api.auth import get_current_user_from_api_key
        from shandy.api.router import api_router as router
        from shandy.database.rls import set_current_user
        from shandy.database.session import get_session

        _, full_key = test_api_key_db

        # Create a job owned by user2
        other_job = Job(
            owner_id=test_user2_db.id,
            title="Other User's Job",
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

        from shandy.api.auth import get_current_user_from_api_key
        from shandy.api.router import api_router as router
        from shandy.database.models import JobShare
        from shandy.database.rls import set_current_user
        from shandy.database.session import get_session

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

        from shandy.api.auth import get_current_user_from_api_key
        from shandy.api.router import api_router as router
        from shandy.database.models import JobShare
        from shandy.database.rls import set_current_user
        from shandy.database.session import get_session

        _, full_key = test_api_key_db

        # Create a job owned by user2
        other_job = Job(
            owner_id=test_user2_db.id,
            title="Other User's Job",
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

        from shandy.api.auth import get_current_user_from_api_key
        from shandy.api.router import api_router as router
        from shandy.database.rls import set_current_user
        from shandy.database.session import get_session

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

        from shandy.api.router import api_router as router
        from shandy.database.session import get_session

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

        from shandy.api import auth as api_auth
        from shandy.api.router import api_router as router
        from shandy.database.session import get_session

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
