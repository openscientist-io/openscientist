"""Tests for load_enabled_experts."""

from __future__ import annotations

import logging

import pytest
from claude_agent_sdk.types import AgentDefinition
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.agent.expert_loader import load_enabled_experts
from openscientist.database.models import Expert


def _expert(**overrides: object) -> Expert:
    """Build an Expert with sensible defaults for loader tests."""
    defaults: dict[str, object] = {
        "slug": "loader-test",
        "name": "Loader Test",
        "description": "Default description",
        "prompt": "Default prompt",
        "category": "research",
        "source": "openscientist",
    }
    defaults.update(overrides)
    return Expert(**defaults)


class _FakeScalars:
    """Stand-in for ScalarResult to test resilience with constraint-violating rows."""

    def __init__(self, rows: list[Expert]) -> None:
        self._rows = rows

    def __iter__(self) -> "_FakeScalars":
        self._iter = iter(self._rows)
        return self

    def __next__(self) -> Expert:
        return next(self._iter)


class _FakeResult:
    def __init__(self, rows: list[Expert]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._rows)


class _FakeSession:
    def __init__(self, rows: list[Expert]) -> None:
        self._rows = rows

    async def execute(self, _stmt: object) -> _FakeResult:
        return _FakeResult(self._rows)


@pytest.mark.asyncio
async def test_load_enabled_experts_via_admin_session_sees_seeded_rows(
    db_session: AsyncSession,
) -> None:
    """Admin session sees all 8 seeded experts through RLS."""
    _ = db_session
    from openscientist.database.session import get_admin_session

    async with get_admin_session() as session:
        result = await load_enabled_experts(session)

    expected_slugs = {
        "research-lead",
        "research-subagent",
        "citations-agent",
        "data-scientist",
        "python-pro",
        "scientific-literature-researcher",
        "data-researcher",
        "research-analyst",
    }
    assert expected_slugs.issubset(result.keys())


@pytest.mark.asyncio
async def test_load_returns_dict_instance(db_session: AsyncSession) -> None:
    """Loader returns a dict."""
    result = await load_enabled_experts(db_session)
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_load_skips_disabled_experts(db_session: AsyncSession) -> None:
    """is_enabled=False rows are filtered out."""
    db_session.add(_expert(slug="enabled-1", is_enabled=True))
    db_session.add(_expert(slug="enabled-2", is_enabled=True))
    db_session.add(_expert(slug="disabled", is_enabled=False))
    await db_session.commit()

    result = await load_enabled_experts(db_session)
    assert {"enabled-1", "enabled-2"}.issubset(result.keys())
    assert "disabled" not in result


@pytest.mark.asyncio
async def test_load_keys_by_slug(db_session: AsyncSession) -> None:
    """Returned dict must be keyed by Expert.slug."""
    db_session.add(_expert(slug="foo-bar"))
    await db_session.commit()

    result = await load_enabled_experts(db_session)
    assert "foo-bar" in result


@pytest.mark.asyncio
async def test_load_maps_description(db_session: AsyncSession) -> None:
    """AgentDefinition.description mirrors Expert.description verbatim."""
    db_session.add(_expert(slug="desc-check", description="Exact description text."))
    await db_session.commit()

    result = await load_enabled_experts(db_session)
    assert result["desc-check"].description == "Exact description text."


@pytest.mark.asyncio
async def test_load_maps_prompt(db_session: AsyncSession) -> None:
    """AgentDefinition.prompt mirrors Expert.prompt verbatim."""
    db_session.add(_expert(slug="prompt-check", prompt="You are a very specific test agent."))
    await db_session.commit()

    result = await load_enabled_experts(db_session)
    assert result["prompt-check"].prompt == "You are a very specific test agent."


@pytest.mark.asyncio
async def test_load_maps_tools_list(db_session: AsyncSession) -> None:
    """Row with tools=['a','b'] → AgentDefinition.tools == ['a','b']."""
    tools = ["mcp__openscientist-tools__search_pubmed", "Read", "Bash"]
    db_session.add(_expert(slug="tools-list", tools=tools))
    await db_session.commit()

    result = await load_enabled_experts(db_session)
    assert result["tools-list"].tools == tools


@pytest.mark.asyncio
async def test_load_maps_tools_none(db_session: AsyncSession) -> None:
    """Row with tools=None → AgentDefinition.tools is None (inherit)."""
    db_session.add(_expert(slug="tools-none", tools=None))
    await db_session.commit()

    result = await load_enabled_experts(db_session)
    assert result["tools-none"].tools is None


@pytest.mark.asyncio
async def test_load_maps_model_none_to_inherit(db_session: AsyncSession) -> None:
    """DB NULL model materializes as 'inherit'."""
    db_session.add(_expert(slug="model-null", model=None))
    await db_session.commit()

    result = await load_enabled_experts(db_session)
    assert result["model-null"].model == "inherit"


@pytest.mark.asyncio
async def test_load_maps_model_sonnet(db_session: AsyncSession) -> None:
    """Row with model='sonnet' passes through unchanged."""
    db_session.add(_expert(slug="model-sonnet", model="sonnet"))
    await db_session.commit()

    result = await load_enabled_experts(db_session)
    assert result["model-sonnet"].model == "sonnet"


@pytest.mark.asyncio
async def test_load_skips_row_with_invalid_model_and_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Malformed row is skipped with a warning, not a crash."""
    bad = Expert(
        slug="loader-bad-model",
        name="bad",
        description="bad",
        prompt="bad",
        category="code",
        source="openscientist",
        model="gpt-4",  # invalid — would fail the CHECK constraint in practice
    )
    good = Expert(
        slug="loader-ok-model",
        name="ok",
        description="ok",
        prompt="ok",
        category="code",
        source="openscientist",
        model="sonnet",
    )
    session = _FakeSession([bad, good])

    with caplog.at_level(logging.WARNING):
        result = await load_enabled_experts(session)  # type: ignore[arg-type]

    assert "loader-ok-model" in result
    assert "loader-bad-model" not in result
    assert any("loader-bad-model" in record.message for record in caplog.records), (
        "expected a warning log mentioning the bad slug"
    )


@pytest.mark.asyncio
async def test_load_skips_row_with_non_list_tools(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Row with non-list tools is skipped."""
    bad = Expert(
        slug="loader-bad-tools",
        name="bad",
        description="bad",
        prompt="bad",
        category="code",
        source="openscientist",
    )
    bad.tools = {"not": "an array"}  # type: ignore[assignment]
    good = Expert(
        slug="loader-ok-tools",
        name="ok",
        description="ok",
        prompt="ok",
        category="code",
        source="openscientist",
        tools=["Read"],
    )
    session = _FakeSession([bad, good])

    with caplog.at_level(logging.WARNING):
        result = await load_enabled_experts(session)  # type: ignore[arg-type]

    assert "loader-bad-tools" not in result
    assert "loader-ok-tools" in result
    assert any("loader-bad-tools" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_load_returns_multiple_rows_independent_of_order(
    db_session: AsyncSession,
) -> None:
    """Two added rows both end up in the dict (subset of full result)."""
    db_session.add(_expert(slug="one"))
    db_session.add(_expert(slug="two"))
    await db_session.commit()

    result = await load_enabled_experts(db_session)
    assert {"one", "two"}.issubset(result.keys())


@pytest.mark.asyncio
async def test_load_returns_rows_in_deterministic_slug_order(
    db_session: AsyncSession,
) -> None:
    """Rows come back in slug order for deterministic prompt rendering."""
    db_session.add(_expert(slug="loader-order-charlie"))
    db_session.add(_expert(slug="loader-order-alpha"))
    db_session.add(_expert(slug="loader-order-bravo"))
    await db_session.commit()

    result = await load_enabled_experts(db_session)

    mine = [s for s in result.keys() if s.startswith("loader-order-")]
    assert mine == sorted(mine), f"expected sorted slugs, got {mine}"


@pytest.mark.asyncio
async def test_load_values_are_agentdefinition_instances(db_session: AsyncSession) -> None:
    """Values in the returned dict must be AgentDefinition instances."""
    db_session.add(_expert(slug="instance-check"))
    await db_session.commit()

    result = await load_enabled_experts(db_session)
    assert isinstance(result["instance-check"], AgentDefinition)
