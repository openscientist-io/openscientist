"""HTTP tests for guided-template API support (create-job + discovery)."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.api.auth import get_current_user_from_api_key
from openscientist.api.router import api_router
from openscientist.database.models import User
from openscientist.database.rls import set_current_user
from openscientist.database.session import get_session


@pytest_asyncio.fixture
async def approved_user(db_session: AsyncSession) -> User:
    user = User(email="tmpl@example.com", name="Template User", is_approved=True)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _build_app(db_session: AsyncSession, user: User) -> FastAPI:
    app = FastAPI()

    async def override_get_session():
        await set_current_user(db_session, user.id)
        yield db_session

    async def override_get_user():
        return user

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_current_user_from_api_key] = override_get_user
    app.include_router(api_router)
    return app


def _mock_loaded_job() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        research_question="composed",
        short_title=None,
        description=None,
        status="pending",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        max_iterations=2,
        current_iteration=0,
        pdb_code=None,
        space_group=None,
    )


class TestJobTemplateDiscovery:
    @pytest.mark.asyncio
    async def test_list_templates_returns_schemas(self, db_session, approved_user):
        app = _build_app(db_session, approved_user)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/v1/job-templates")

        assert response.status_code == 200
        templates = {t["id"]: t for t in response.json()}
        assert set(templates) == {"gene-set-enrichment", "variant-interpretation"}

        gse = templates["gene-set-enrichment"]
        assert gse["version"] == "1"
        field_keys = {f["key"] for f in gse["fields"]}
        assert {"foreground_genes", "background_genes", "organism", "database"} <= field_keys
        database_field = next(f for f in gse["fields"] if f["key"] == "database")
        assert {o["value"] for o in database_field["options"]} >= {"go", "kegg", "reactome"}


class TestCreateJobFromTemplate:
    @pytest.mark.asyncio
    async def test_template_composes_question_and_guidance(self, db_session, approved_user):
        app = _build_app(db_session, approved_user)
        mock_manager = MagicMock()
        mock_manager.create_job = MagicMock()

        with (
            patch("openscientist.api.endpoints.jobs._get_job_manager", return_value=mock_manager),
            patch(
                "openscientist.api.endpoints.jobs.get_job_by_id", new_callable=AsyncMock
            ) as mock_get_job,
        ):
            mock_get_job.return_value = _mock_loaded_job()
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/jobs",
                    json={
                        "template_id": "gene-set-enrichment",
                        "template_inputs": {
                            "gene_set_label": "cold-up",
                            "foreground_genes": "TP53\nBRCA1",
                            "organism": "Homo sapiens",
                            "database": "go",
                        },
                        "additional_context": "focus on adipose tissue",
                        "max_iterations": 2,
                    },
                )

        assert response.status_code == 201
        mock_manager.create_job.assert_called_once()
        kwargs = mock_manager.create_job.call_args.kwargs
        assert "gene set enrichment" in kwargs["research_question"].lower()
        assert "GO enrichment" in kwargs["research_question"]
        assert "focus on adipose tissue" in kwargs["research_question"]
        assert "Methodology Guardrails" in kwargs["description"]

    @pytest.mark.asyncio
    async def test_missing_required_input_returns_422(self, db_session, approved_user):
        app = _build_app(db_session, approved_user)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/jobs",
                json={
                    "template_id": "gene-set-enrichment",
                    "template_inputs": {"organism": "Homo sapiens"},
                },
            )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_unknown_template_returns_422(self, db_session, approved_user):
        app = _build_app(db_session, approved_user)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/jobs",
                json={"template_id": "does-not-exist", "template_inputs": {}},
            )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_version_mismatch_returns_422(self, db_session, approved_user):
        app = _build_app(db_session, approved_user)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/jobs",
                json={
                    "template_id": "gene-set-enrichment",
                    "template_version": "99",
                    "template_inputs": {
                        "gene_set_label": "x",
                        "foreground_genes": "TP53",
                        "organism": "Homo sapiens",
                        "database": "go",
                    },
                },
            )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_freeform_still_requires_research_question(self, db_session, approved_user):
        app = _build_app(db_session, approved_user)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/v1/jobs", json={"max_iterations": 2})
        assert response.status_code == 422
