"""Tests for the standalone phenix tools."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import TextContent
from sqlalchemy import delete

from openscientist.database import async_session_local
from openscientist.database.models.job import Job
from openscientist.knowledge_state import KnowledgeState
from openscientist_tools import phenix as phenix_module
from openscientist_tools.phenix import (
    compare_structures,
    parse_alphafold_confidence,
    run_phenix_tool,
)
from openscientist_tools.state import STATE


def _phenix_available_in_test_env() -> bool:
    """Check whether PHENIX_PATH is set and points at a real install."""
    phenix_path = os.environ.get("PHENIX_PATH")
    if not phenix_path:
        return False
    return (Path(phenix_path) / "bin" / "phenix.about").is_file()


PHENIX_REQUIRED = pytest.mark.skipif(
    not _phenix_available_in_test_env(),
    reason="PHENIX_PATH not set or Phenix not installed at that path",
)


def _write_pdb(path: Path, residues: list[tuple[int, float]]) -> None:
    """Write a minimal PDB with one CA atom per residue at the given pLDDT.

    Column positions follow the PDB ATOM-record spec so that the standalone
    parser's slices (line[12:16] for atom name, line[22:26] for residue
    number, line[60:66] for B-factor / pLDDT) hit the right fields.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    serial = 1
    for resnum, plddt in residues:
        lines.append(
            "ATOM  "
            f"{serial:>5d}"
            " "
            f"{'CA':>4s}"
            " "
            "ALA"
            " "
            "A"
            f"{resnum:>4d}"
            "    "
            f"{0.0:>8.3f}{0.0:>8.3f}{0.0:>8.3f}"
            f"{1.0:>6.2f}{plddt:>6.2f}"
            "          "
            f"{'C':>2s}"
            "\n"
        )
        serial += 1
    path.write_text("".join(lines))


@asynccontextmanager
async def _spawned_for_job(
    server_env: Callable[..., dict[str, str]],
    server_params: Callable[[dict[str, str]], StdioServerParameters],
    tmp_path: Path,
    test_database_url: str,
    job_id: UUID,
    *,
    env_overrides: dict[str, str] | None = None,
) -> AsyncGenerator[ClientSession, None]:
    async with async_session_local() as setup:
        setup.add(
            Job(
                id=job_id,
                research_question="phenix subprocess test",
                llm_provider="mock",
                llm_config={"model": "mock-model-v1"},
                status="pending",
            )
        )
        await setup.commit()
    try:
        env = server_env(tmp_path, OPENSCIENTIST_JOB_ID=str(job_id))
        env["DATABASE_URL"] = test_database_url
        env["OPENSCIENTIST_SECRET_KEY"] = os.environ["OPENSCIENTIST_SECRET_KEY"]
        if os.environ.get("PHENIX_PATH"):
            env["PHENIX_PATH"] = os.environ["PHENIX_PATH"]
        if env_overrides:
            for key, value in env_overrides.items():
                if value == "":
                    env.pop(key, None)
                else:
                    env[key] = value
        # Run the subprocess in tmp_path so phenix.* tools that emit
        # output files into CWD (e.g. phenix.superpose_pdbs writes
        # `*_fitted.pdb`) don't leak into the repo root.
        params = server_params(env)
        params.cwd = str(tmp_path)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    finally:
        async with async_session_local() as cleanup:
            await cleanup.execute(delete(Job).where(Job.id == job_id))
            await cleanup.commit()


def _text(result: object) -> str:
    blocks = result.content  # type: ignore[attr-defined]
    (block,) = blocks
    assert isinstance(block, TextContent)
    return block.text


@pytest.fixture(autouse=True)
def _state_job_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(STATE, "job_id", "test-job-uuid")


@pytest.fixture
def state_job_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point STATE.job_dir at tmp_path and create the data subdir."""
    monkeypatch.setattr(STATE, "job_dir", tmp_path)
    (tmp_path / "data").mkdir()
    return tmp_path


def _fake_setup_phenix_env() -> dict[str, str]:
    return {"PATH": "/usr/bin", "PHENIX": "/fake", "PHENIX_PREFIX": "/fake"}


# ----- In-process branch coverage -----


async def test_run_phenix_tool_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    state_job_dir: Path,
    patched_ks_persistence: KnowledgeState,
) -> None:
    (state_job_dir / "data" / "model.pdb").write_text("dummy")
    monkeypatch.setattr(phenix_module, "setup_phenix_env", _fake_setup_phenix_env)

    completed = subprocess.CompletedProcess(
        args=["phenix.clashscore", str(state_job_dir / "data" / "model.pdb")],
        returncode=0,
        stdout="MolProbity clashscore = 5.0\n",
        stderr="",
    )
    monkeypatch.setattr("openscientist_tools.phenix.subprocess.run", lambda *a, **kw: completed)

    result = await run_phenix_tool("phenix.clashscore", ["model.pdb"])
    assert "MolProbity clashscore = 5.0" in result
    assert "Command: phenix.clashscore" in result

    last_log = patched_ks_persistence.data["analysis_log"][-1]
    assert last_log["action"] == "run_phenix_tool"
    assert last_log["tool_name"] == "phenix.clashscore"
    assert last_log["success"] is True


async def test_run_phenix_tool_nonzero_returncode(
    monkeypatch: pytest.MonkeyPatch,
    state_job_dir: Path,
    patched_ks_persistence: KnowledgeState,
) -> None:
    (state_job_dir / "data" / "model.pdb").write_text("dummy")
    monkeypatch.setattr(phenix_module, "setup_phenix_env", _fake_setup_phenix_env)

    completed = subprocess.CompletedProcess(
        args=["phenix.clashscore"],
        returncode=1,
        stdout="",
        stderr="bad input file",
    )
    monkeypatch.setattr("openscientist_tools.phenix.subprocess.run", lambda *a, **kw: completed)

    result = await run_phenix_tool("phenix.clashscore", ["model.pdb"])
    assert "❌" in result
    assert "exited with code 1" in result
    assert "bad input file" in result
    assert patched_ks_persistence.data["analysis_log"][-1]["success"] is False


async def test_run_phenix_tool_timeout(
    monkeypatch: pytest.MonkeyPatch,
    state_job_dir: Path,
    patched_ks_persistence: KnowledgeState,
) -> None:
    (state_job_dir / "data" / "model.pdb").write_text("dummy")
    monkeypatch.setattr(phenix_module, "setup_phenix_env", _fake_setup_phenix_env)

    def _raise_timeout(*_a: object, **_kw: object) -> None:
        raise subprocess.TimeoutExpired(cmd="phenix.clashscore", timeout=300)

    monkeypatch.setattr("openscientist_tools.phenix.subprocess.run", _raise_timeout)

    result = await run_phenix_tool("phenix.clashscore", ["model.pdb"])
    assert "timed out after 5 minutes" in result


async def test_run_phenix_tool_missing_input_file(
    monkeypatch: pytest.MonkeyPatch,
    state_job_dir: Path,
    patched_ks_persistence: KnowledgeState,
) -> None:
    monkeypatch.setattr(phenix_module, "setup_phenix_env", _fake_setup_phenix_env)
    result = await run_phenix_tool("phenix.clashscore", ["nonexistent.pdb"])
    # The error names the missing input file
    assert result.startswith("❌")
    assert "nonexistent.pdb" in result


async def test_compare_structures_appends_rmsd_interpretation(
    monkeypatch: pytest.MonkeyPatch,
    state_job_dir: Path,
    patched_ks_persistence: KnowledgeState,
) -> None:
    (state_job_dir / "data" / "exp.pdb").write_text("dummy")
    (state_job_dir / "data" / "pred.pdb").write_text("dummy")
    monkeypatch.setattr(phenix_module, "setup_phenix_env", _fake_setup_phenix_env)

    completed = subprocess.CompletedProcess(
        args=["phenix.superpose_pdbs"],
        returncode=0,
        stdout="RMSD = 1.23\n",
        stderr="",
    )
    monkeypatch.setattr("openscientist_tools.phenix.subprocess.run", lambda *a, **kw: completed)

    result = await compare_structures("exp.pdb", "pred.pdb")
    assert "RMSD = 1.23" in result
    assert "Interpretation hints" in result
    assert "RMSD 1-2 Å" in result


def test_parse_alphafold_confidence_extracts_plddt(state_job_dir: Path) -> None:
    _write_pdb(
        state_job_dir / "data" / "model.pdb",
        [(1, 95.0), (2, 92.0), (3, 88.0), (4, 75.0)],
    )

    result = parse_alphafold_confidence("model.pdb")
    assert "Residues analyzed: 4" in result
    assert "Average pLDDT: 87.5" in result
    assert "Min pLDDT: 75.0" in result
    assert "Max pLDDT: 95.0" in result
    assert "✅ No low confidence regions detected" in result


def test_parse_alphafold_confidence_with_pae_json(state_job_dir: Path) -> None:
    _write_pdb(state_job_dir / "data" / "model.pdb", [(1, 95.0), (2, 92.0)])
    (state_job_dir / "data" / "pae.json").write_text(json.dumps({"a": 1, "b": 2, "c": 3}))

    result = parse_alphafold_confidence("model.pdb", pae_json="pae.json")
    assert "PAE data loaded: 3 entries" in result


def test_parse_alphafold_confidence_low_confidence_ranges(state_job_dir: Path) -> None:
    _write_pdb(
        state_job_dir / "data" / "model.pdb",
        [(1, 95.0), (2, 50.0), (3, 60.0), (4, 65.0), (5, 90.0), (6, 40.0)],
    )

    result = parse_alphafold_confidence("model.pdb")
    assert "Low confidence regions (<70): 2-4, 6-6" in result


# ----- Full-call subprocess + real-DB tests, gated on Phenix install -----


@PHENIX_REQUIRED
async def test_run_phenix_tool_via_subprocess(
    tmp_path: Path,
    server_env: Callable[..., dict[str, str]],
    server_params: Callable[[dict[str, str]], StdioServerParameters],
    test_database_url: str,
    _apply_migrations_once: None,
) -> None:
    job_id = uuid4()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_pdb(data_dir / "model.pdb", [(i, 90.0) for i in range(1, 11)])

    async with _spawned_for_job(
        server_env, server_params, tmp_path, test_database_url, job_id
    ) as mcp:
        response = await mcp.call_tool(
            "run_phenix_tool",
            {"tool_name": "phenix.clashscore", "input_files": ["model.pdb"]},
        )
        text = _text(response)
        assert "❌" not in text
        assert "Command: phenix.clashscore" in text

        reloaded = await KnowledgeState.load_from_database(str(job_id))
        assert reloaded.data["analysis_log"][-1]["action"] == "run_phenix_tool"
        assert reloaded.data["analysis_log"][-1]["tool_name"] == "phenix.clashscore"


@PHENIX_REQUIRED
async def test_compare_structures_via_subprocess(
    tmp_path: Path,
    server_env: Callable[..., dict[str, str]],
    server_params: Callable[[dict[str, str]], StdioServerParameters],
    test_database_url: str,
    _apply_migrations_once: None,
) -> None:
    job_id = uuid4()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_pdb(data_dir / "exp.pdb", [(i, 80.0) for i in range(1, 11)])
    _write_pdb(data_dir / "pred.pdb", [(i, 85.0) for i in range(1, 11)])

    async with _spawned_for_job(
        server_env, server_params, tmp_path, test_database_url, job_id
    ) as mcp:
        response = await mcp.call_tool(
            "compare_structures",
            {"experimental_pdb": "exp.pdb", "predicted_pdb": "pred.pdb"},
        )
        text = _text(response)
        assert "RMSD" in text


@PHENIX_REQUIRED
async def test_parse_alphafold_confidence_via_subprocess(
    tmp_path: Path,
    server_env: Callable[..., dict[str, str]],
    server_params: Callable[[dict[str, str]], StdioServerParameters],
    test_database_url: str,
    _apply_migrations_once: None,
) -> None:
    job_id = uuid4()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_pdb(data_dir / "model.pdb", [(1, 95.0), (2, 92.0), (3, 88.0)])

    async with _spawned_for_job(
        server_env, server_params, tmp_path, test_database_url, job_id
    ) as mcp:
        response = await mcp.call_tool("parse_alphafold_confidence", {"alphafold_pdb": "model.pdb"})
        text = _text(response)
        assert "Residues analyzed: 3" in text
        assert "Average pLDDT: 91.7" in text


# ----- Conditional registration -----


@PHENIX_REQUIRED
async def test_phenix_tools_present_when_phenix_available(
    tmp_path: Path,
    server_env: Callable[..., dict[str, str]],
    server_params: Callable[[dict[str, str]], StdioServerParameters],
    test_database_url: str,
    _apply_migrations_once: None,
) -> None:
    job_id = uuid4()
    async with _spawned_for_job(
        server_env, server_params, tmp_path, test_database_url, job_id
    ) as mcp:
        tools = await mcp.list_tools()
        names = {t.name for t in tools.tools}
        assert "run_phenix_tool" in names
        assert "compare_structures" in names
        assert "parse_alphafold_confidence" in names


async def test_phenix_tools_absent_when_phenix_unavailable(
    tmp_path: Path,
    server_env: Callable[..., dict[str, str]],
    server_params: Callable[[dict[str, str]], StdioServerParameters],
    test_database_url: str,
    _apply_migrations_once: None,
) -> None:
    job_id = uuid4()
    async with _spawned_for_job(
        server_env,
        server_params,
        tmp_path,
        test_database_url,
        job_id,
        env_overrides={"PHENIX_PATH": ""},
    ) as mcp:
        tools = await mcp.list_tools()
        names = {t.name for t in tools.tools}
        assert "run_phenix_tool" not in names
        assert "compare_structures" not in names
        assert "parse_alphafold_confidence" not in names
