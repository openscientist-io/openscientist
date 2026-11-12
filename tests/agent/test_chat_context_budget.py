"""Chat requests leave room for on-demand job artifacts."""

import asyncio
import json
import os
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.agent.base import AgentConfig
from openscientist.agent.claude_code_agent import ClaudeCodeAgent
from openscientist.agent.codex_agent import CodexAgent
from openscientist.agent.omp_agent import OmpAgent
from openscientist.database.models import Job, Skill
from openscientist.job_chat import (
    _build_chat_request,
    _chat_turn_files,
    _load_chat_context,
    run_chat_turn_async,
)
from openscientist.knowledge_state import KnowledgeState
from openscientist.skill_ingestion import SkillParser
from tests.agent.fake_anthropic_api import ReadFile, Say, answered_tool, serving
from tests.helpers import StubClaudeProvider


@pytest.fixture
async def large_chat_job(tmp_path: Path, db_session: AsyncSession, test_job: Job) -> Path:
    job = tmp_path / str(test_job.id)
    job.mkdir()
    state = KnowledgeState(str(test_job.id), "Which evidence supports the result?", 60)
    evidence = "The adjusted effect persists across cohorts with confidence interval 0.12 to 0.37. "
    for i in range(120):
        state.add_finding(f"Cohort finding {i}", evidence * 6)
        state.add_hypothesis(f"The effect in cohort {i} is reproducible.")
    state.data["literature"] = [
        {"title": f"Cohort study {i}", "pmid": str(100000 + i), "abstract": evidence * 20}
        for i in range(240)
    ]
    state.data["iteration_summaries"] = [
        {"iteration": i, "strapline": f"Cohort analysis {i}", "summary": evidence * 25}
        for i in range(1, 61)
    ]
    await state.save_to_database(str(test_job.id), db_session)
    (job / "final_report.md").write_text(evidence * 2000)
    (job / "data").mkdir()
    (job / "data" / "cohorts.csv").write_text("cohort,effect\n" + "1,0.25\n" * 20000)
    (job / "provenance").mkdir()
    (job / "provenance" / "iter60_transcript.json").write_text(json.dumps(state.to_dict()))
    agent = ClaudeCodeAgent(AgentConfig(job_dir=job), StubClaudeProvider())
    agent._write_job_claude_md(use_hypotheses=True)
    parser = SkillParser()
    sources = [
        (
            f"scientific-skills/method-{i}/SKILL.md",
            f"---\nname: method-{i}\ndescription: Scientific analysis of cohort data\n---\n"
            + evidence * 200,
        )
        for i in range(177)
    ]
    for path, content in sources:
        parsed = parser.parse_content(content, path)
        skill = Skill(
            name=parsed.name,
            slug=parsed.slug,
            category=parsed.category,
            description=parsed.description,
            content=parsed.content,
        )
        agent._write_skill(job / ".claude" / "skills", skill)
    return job


@pytest.fixture
def isolated_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    for name in (
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDECODE",
    ):
        monkeypatch.delenv(name, raising=False)
    for name in (
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "DISABLE_TELEMETRY",
        "DISABLE_AUTOUPDATER",
    ):
        monkeypatch.setenv(name, "1")
    monkeypatch.setattr("openscientist.providers.get_provider", StubClaudeProvider)


def _bytes(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message", ["What is the main result?", "What does @data/cohorts.csv show?"]
)
async def test_large_job_chat_request_budget(
    large_chat_job: Path,
    db_session: AsyncSession,
    test_job: Job,
    isolated_cli: None,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    request = await _build_chat_request(db_session, test_job.id, message)
    request_path, response_path = _chat_turn_files(large_chat_job, "budget")
    request_path.write_text(json.dumps(request))
    with serving(lambda _body: Say("The effect persists.")) as (url, api):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", url)
        async with asyncio.timeout(60):
            status = await run_chat_turn_async(large_chat_job, "budget", str(test_job.id))
    assert status == {"status": "completed"}, response_path.read_text()
    body = api.requests[0]
    tools = body.get("tools", [])
    assert isinstance(tools, list)
    assert _bytes(body) < 100_000
    assert "1,0.25" not in json.dumps(body)


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_type", [CodexAgent, OmpAgent])
async def test_large_job_prompt_budget(
    large_chat_job: Path,
    db_session: AsyncSession,
    test_job: Job,
    isolated_cli: None,
    monkeypatch: pytest.MonkeyPatch,
    agent_type: type[CodexAgent] | type[OmpAgent],
) -> None:
    monkeypatch.setattr(
        "openscientist.agent.factory.agent_class_for_provider", lambda _provider: agent_type
    )
    request = await _build_chat_request(db_session, test_job.id, "What is the main result?")
    prompt = request["prompt"] or ""
    instructions = request["system_prompt"] or ""
    assert len(prompt.encode()) < 20_000
    assert len(instructions.encode()) < 10_000


@pytest.mark.asyncio
async def test_chat_reads_full_state_without_loading_discovery_memory(
    large_chat_job: Path,
    db_session: AsyncSession,
    test_job: Job,
    isolated_cli: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = await _build_chat_request(db_session, test_job.id, "Explain finding F120.")
    snapshot = request["knowledge_state"]
    assert snapshot is not None
    state = json.loads(snapshot)
    assert len(state["findings"]) == 120
    assert len(state["literature"]) == 240
    assert state["findings"][-1]["evidence"].endswith("0.12 to 0.37. ")
    assert "Cohort finding 119" not in (request["prompt"] or "")
    (large_chat_job / "CLAUDE.md").write_text("JOB_MEMORY_SENTINEL " * 10000)
    (large_chat_job.parent / "CLAUDE.md").write_text("PARENT_MEMORY_SENTINEL " * 10000)
    home_memory = Path(os.environ["CLAUDE_CONFIG_DIR"])
    home_memory.mkdir(exist_ok=True)
    (home_memory / "CLAUDE.md").write_text("HOME_MEMORY_SENTINEL " * 10000)
    discovery_doc = large_chat_job / ".claude" / "CLAUDE.md"
    discovery_text = discovery_doc.read_text()
    request_path, response_path = _chat_turn_files(large_chat_job, "details")
    request_path.write_text(json.dumps(request))
    context_path = large_chat_job / ".chat_context_details.json"
    target_line = next(
        i for i, line in enumerate(snapshot.splitlines(), 1) if "Cohort finding 119" in line
    )

    def reply(body: dict[str, object]) -> ReadFile | Say:
        if answered_tool(body):
            return Say("Finding F120 is supported by the adjusted effect.")
        assert context_path.read_text() == snapshot
        return ReadFile(str(context_path), offset=target_line, limit=4)

    with serving(reply) as (url, api):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", url)
        async with asyncio.timeout(60):
            status = await run_chat_turn_async(large_chat_job, "details", str(test_job.id))
    assert status == {"status": "completed"}, response_path.read_text()
    assert len(api.requests) == 2
    assert "Cohort finding 119" in json.dumps(api.requests[-1])
    assert "MEMORY_SENTINEL" not in json.dumps(api.requests)
    assert discovery_doc.read_text() == discovery_text
    assert not context_path.exists()
    assert all(_bytes(body) < 100_000 for body in api.requests)


@pytest.mark.asyncio
async def test_evidence_text_is_not_split_into_character_bullets(
    db_session: AsyncSession,
    test_job: Job,
) -> None:
    state = KnowledgeState(str(test_job.id), "Evidence?", 1)
    state.add_finding("Effect", "The confidence interval excludes zero.")
    await state.save_to_database(str(test_job.id), db_session)
    context = (await _load_chat_context(str(test_job.id), db_session))[0]
    assert "The confidence interval excludes zero." in context
    assert "- T\n- h\n" not in context


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["", "x"])
async def test_non_ascii_context_is_bounded_in_bytes(
    db_session: AsyncSession,
    test_job: Job,
    prefix: str,
) -> None:
    state = KnowledgeState(str(test_job.id), "Evidence?", 20)
    for _ in range(20):
        state.add_finding(prefix + "\U0001f9ec" * 1000, prefix + "\U0001f9ec" * 1000)
    await state.save_to_database(str(test_job.id), db_session)
    context = (await _load_chat_context(str(test_job.id), db_session))[0]
    assert "\U0001f9ec" in context
    assert "\ufffd" not in context
    assert len(context.encode()) <= 12000
