"""Tests for MARDUK persistent memory: distillation logic + DB model/RLS."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist import marduk_memory
from openscientist.database.models import Job, MardukMemory, User
from openscientist.database.rls import set_current_user
from tests.helpers import enable_rls

# --------------------------------------------------------------------------- #
# Pure-function tests (no DB)                                                  #
# --------------------------------------------------------------------------- #


def test_extract_curies_dedupes_in_order() -> None:
    curies = marduk_memory.extract_curies(
        "MONDO:0007947 is linked to HGNC:3603 and HP:0002616",
        "HGNC:3603 again",
        None,
    )
    assert curies == ["MONDO:0007947", "HGNC:3603", "HP:0002616"]


def test_extract_curies_ignores_non_curie_text() -> None:
    assert marduk_memory.extract_curies("no identifiers here", "") == []


def test_format_memories_markdown_empty() -> None:
    assert "No prior MARDUK memories" in marduk_memory.format_memories_markdown([])


def test_format_memories_markdown_renders_fields() -> None:
    mem = MardukMemory(
        owner_id=uuid4(),
        title="FBN1 confirmed in Marfan",
        content="FBN1 loss-of-function causes Marfan syndrome.",
        entity_id="MONDO:0007947",
        entity_label="Marfan syndrome",
        evidence="job xyz",
        tags=["FBN1", "MONDO:0007947"],
    )
    text = marduk_memory.format_memories_markdown([mem])
    assert "FBN1 confirmed in Marfan" in text
    assert "MONDO:0007947" in text
    assert "Evidence" in text


# --------------------------------------------------------------------------- #
# extract_memories_from_job distillation logic (KS + save_memory mocked)       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_extract_uses_consensus_and_first_curie(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_ks = SimpleNamespace(
        data={
            "config": {"research_question": "What gene underlies MONDO:0007947?"},
            "consensus_answer": "The gene HGNC:3603 (FBN1) underlies MONDO:0007947.",
            "findings": [{"text": "FBN1 variants segregate with disease."}],
            "iteration": 4,
        }
    )
    # KnowledgeState is imported lazily inside the function; patch at its source.
    import openscientist.knowledge_state as ks_mod

    monkeypatch.setattr(
        ks_mod.KnowledgeState,
        "load_from_database_sync",
        classmethod(lambda cls, job_id: fake_ks),
    )
    saved: dict[str, object] = {}

    async def fake_save(**kwargs: object) -> object:
        saved.update(kwargs)
        return uuid4()

    monkeypatch.setattr(marduk_memory, "save_memory", fake_save)

    count = await marduk_memory.extract_memories_from_job(str(uuid4()))

    assert count == 1
    assert saved["content"] == "The gene HGNC:3603 (FBN1) underlies MONDO:0007947."
    assert saved["entity_id"] == "HGNC:3603"  # first curie in consensus
    assert "HGNC:3603" in saved["tags"]  # type: ignore[operator]


@pytest.mark.asyncio
async def test_extract_noop_on_empty_job(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_ks = SimpleNamespace(
        data={"config": {"research_question": "q"}, "consensus_answer": None, "findings": []}
    )
    import openscientist.knowledge_state as ks_mod

    monkeypatch.setattr(
        ks_mod.KnowledgeState,
        "load_from_database_sync",
        classmethod(lambda cls, job_id: fake_ks),
    )
    save_mock = AsyncMock()
    monkeypatch.setattr(marduk_memory, "save_memory", save_mock)

    count = await marduk_memory.extract_memories_from_job(str(uuid4()))

    assert count == 0
    save_mock.assert_not_called()


@pytest.mark.asyncio
async def test_extract_falls_back_to_findings(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_ks = SimpleNamespace(
        data={
            "config": {"research_question": "q"},
            "consensus_answer": "",
            "findings": [{"text": "finding one"}, {"text": "finding two"}],
            "iteration": 2,
        }
    )
    import openscientist.knowledge_state as ks_mod

    monkeypatch.setattr(
        ks_mod.KnowledgeState,
        "load_from_database_sync",
        classmethod(lambda cls, job_id: fake_ks),
    )
    saved: dict[str, object] = {}

    async def fake_save(**kwargs: object) -> object:
        saved.update(kwargs)
        return uuid4()

    monkeypatch.setattr(marduk_memory, "save_memory", fake_save)

    count = await marduk_memory.extract_memories_from_job(str(uuid4()))
    assert count == 1
    assert "finding one" in saved["content"]  # type: ignore[operator]


# --------------------------------------------------------------------------- #
# DB model + RLS (validates the migration end-to-end)                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_job_marduk_enabled_column_roundtrips(db_session: AsyncSession) -> None:
    user = User(email="marduk_flag@example.com", name="U")
    db_session.add(user)
    await db_session.commit()

    job = Job(owner_id=user.id, research_question="rare disease q", marduk_enabled=True)
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    assert job.marduk_enabled is True


@pytest.mark.asyncio
async def test_memory_rls_is_owner_scoped(db_session: AsyncSession) -> None:
    alice = User(email="alice_mem@example.com", name="Alice")
    bob = User(email="bob_mem@example.com", name="Bob")
    db_session.add_all([alice, bob])
    await db_session.commit()

    mem = MardukMemory(
        owner_id=alice.id,
        title="Alice's private insight",
        content="secret rare-disease insight",
        tags=[],
    )
    db_session.add(mem)
    await db_session.commit()

    await enable_rls(db_session)

    # Owner sees it.
    await set_current_user(db_session, alice.id)
    result = await db_session.execute(select(MardukMemory).where(MardukMemory.id == mem.id))
    assert result.scalar_one_or_none() is not None

    # Non-owner does not.
    await set_current_user(db_session, bob.id)
    result = await db_session.execute(select(MardukMemory).where(MardukMemory.id == mem.id))
    assert result.scalar_one_or_none() is None
