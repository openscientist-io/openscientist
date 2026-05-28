"""Tests for the standalone Exomiser tool (`run_exomiser`)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml  # type: ignore[import-untyped]
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from sqlalchemy import delete

from openscientist.database import AsyncSessionLocal
from openscientist.database.models.job import Job
from openscientist.knowledge_state import KnowledgeState
from openscientist_tools import exomiser as exo
from openscientist_tools.exomiser import (
    _baseline_run_metadata,
    _exomiser_installed,
    _extract_hpo_ids,
    _gene_ranking_entry,
    _write_baseline_ranking,
    _write_phenotype_only_analysis,
    run_exomiser,
)
from openscientist_tools.state import STATE


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _state_job_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(STATE, "job_id", "test-job-uuid")


@pytest.fixture
def state_job_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point STATE.job_dir at tmp_path and create the data subdir."""
    monkeypatch.setattr(STATE, "job_dir", tmp_path)
    (tmp_path / "data").mkdir()
    return tmp_path


def _gene_record(
    symbol: str,
    combined: float,
    *,
    ensembl: str,
    entrez: str,
    moi: str = "AUTOSOMAL_DOMINANT",
    acmg: str | None = "PATHOGENIC",
    variant: tuple[str, int, str, str] | None = ("10", 121520163, "G", "C"),
    diseases: list[tuple[str, str, float]],
    pheno: float = 0.5,
    var: float = 0.9,
) -> dict[str, Any]:
    """Build a minimal Exomiser-shaped gene JSONL record."""
    gene_scores: dict[str, Any] = {"modeOfInheritance": moi}
    if acmg:
        gene_scores["acmgAssignments"] = [{"acmgClassification": acmg}]
    if variant:
        gene_scores["contributingVariants"] = [
            {"contigName": variant[0], "start": variant[1], "ref": variant[2], "alt": variant[3]}
        ]
    return {
        "geneSymbol": symbol,
        "combinedScore": combined,
        "priorityScore": pheno,
        "variantScore": var,
        "geneIdentifier": {"geneSymbol": symbol, "ensemblId": ensembl, "entrezId": entrez},
        "geneScores": [gene_scores],
        "priorityResults": {
            "HIPHIVE_PRIORITY": {
                "diseaseMatches": [
                    {"score": s, "model": {"disease": {"diseaseId": did, "diseaseName": dn}}}
                    for (did, dn, s) in diseases
                ]
            }
        },
    }


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> str:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return str(path)


# --------------------------------------------------------------------------- #
# _extract_hpo_ids
# --------------------------------------------------------------------------- #
def test_extract_hpo_ids_v1_yaml(tmp_path: Path) -> None:
    p = tmp_path / "pp.yml"
    p.write_text(
        yaml.safe_dump(
            {
                "phenotypicFeatures": [
                    {"type": {"id": "HP:0001159", "label": "Syndactyly"}},
                    {"type": {"id": "HP:0000316", "label": "Hypertelorism"}},
                ]
            }
        )
    )
    assert _extract_hpo_ids(p) == ["HP:0001159", "HP:0000316"]


def test_extract_hpo_ids_json_and_excluded(tmp_path: Path) -> None:
    p = tmp_path / "pp.json"
    p.write_text(
        json.dumps(
            {
                "phenotypicFeatures": [
                    {"type": {"id": "HP:0000001"}},
                    {"type": {"id": "HP:0000002"}, "excluded": True},
                    {"type": {"id": "NOTHP:9"}},
                ]
            }
        )
    )
    # JSON parses via the YAML loader; excluded + non-HP terms are skipped.
    assert _extract_hpo_ids(p) == ["HP:0000001"]


def test_extract_hpo_ids_no_features(tmp_path: Path) -> None:
    p = tmp_path / "pp.yml"
    p.write_text(yaml.safe_dump({"subject": {"id": "x"}}))
    assert _extract_hpo_ids(p) == []


# --------------------------------------------------------------------------- #
# _write_phenotype_only_analysis
# --------------------------------------------------------------------------- #
def test_write_phenotype_only_analysis(tmp_path: Path) -> None:
    out = _write_phenotype_only_analysis(tmp_path, "hg38", ["HP:1", "HP:2"])
    doc = yaml.safe_load(out.read_text())
    assert doc["analysis"]["genomeAssembly"] == "hg38"
    assert doc["analysis"]["hpoIds"] == ["HP:1", "HP:2"]
    step_keys = [next(iter(s)) for s in doc["analysis"]["steps"]]
    assert step_keys == ["omimPrioritiser", "hiPhivePrioritiser"]
    assert "PARQUET" in doc["outputOptions"]["outputFormats"]


# --------------------------------------------------------------------------- #
# _gene_ranking_entry
# --------------------------------------------------------------------------- #
def test_gene_ranking_entry_fields() -> None:
    rec = _gene_record(
        "FGFR2",
        0.96,
        ensembl="ENSG00000066468",
        entrez="2263",
        diseases=[("OMIM:101600", "Craniofacial dysplasia", 0.86)],
    )
    e = _gene_ranking_entry(rec)
    assert e["geneSymbol"] == "FGFR2"
    assert e["geneId"] == "ENSG00000066468"
    assert e["entrezId"] == "2263"
    assert e["moi"] == "AUTOSOMAL_DOMINANT"
    assert e["acmgClassification"] == "PATHOGENIC"
    assert e["variants"] == ["10-121520163-G-C"]
    assert e["topDiseaseMatch"] == "OMIM:101600 Craniofacial dysplasia"


def test_gene_ranking_entry_tolerates_missing_pieces() -> None:
    rec = _gene_record("X", 0.1, ensembl="ENSG1", entrez="9", acmg=None, variant=None, diseases=[])
    e = _gene_ranking_entry(rec)
    assert e["acmgClassification"] is None
    assert e["variants"] == []
    assert e["topDiseaseMatch"] is None


# --------------------------------------------------------------------------- #
# _write_baseline_ranking
# --------------------------------------------------------------------------- #
def test_write_baseline_ranking_genes_and_diseases(tmp_path: Path) -> None:
    g1 = _gene_record(
        "G1",
        0.9,
        ensembl="ENSG1",
        entrez="1",
        diseases=[("OMIM:1", "A", 0.8), ("OMIM:2", "B", 0.5)],
    )
    g2 = _gene_record(
        "G2",
        0.7,
        ensembl="ENSG2",
        entrez="2",
        diseases=[("OMIM:2", "B", 0.9), ("OMIM:3", "C", 0.3)],
    )
    jsonl = _write_jsonl(tmp_path / "out.jsonl", [g2, g1])  # unsorted on purpose

    out = _write_baseline_ranking(
        tmp_path,
        [jsonl],
        sample="patient.yml",
        assembly="hg38",
        jar="/x/exomiser-cli-15.0.0.jar",
        exomiser_path="/x",
    )
    assert out is not None
    d = json.loads(out.read_text())

    # Genes sorted by combinedScore desc.
    assert [g["geneSymbol"] for g in d["geneRanking"]] == ["G1", "G2"]
    assert d["geneRanking"][0]["rank"] == 1
    assert d["totalGenes"] == 2

    # Diseases aggregated to best score per disease, sorted desc:
    # OMIM:2 best is 0.9 (from G2) > OMIM:1 0.8 > OMIM:3 0.3.
    ids = [x["diseaseId"] for x in d["diseaseRanking"]]
    assert ids == ["OMIM:2", "OMIM:1", "OMIM:3"]
    top = d["diseaseRanking"][0]
    assert top["phenotypeScore"] == 0.9
    assert top["topGene"] == "G2"
    assert d["totalDiseases"] == 3
    assert d["runMetadata"]["exomiserVersion"] == "15.0.0"


def test_write_baseline_ranking_caps_lists(tmp_path: Path) -> None:
    g1 = _gene_record("G1", 0.9, ensembl="E1", entrez="1", diseases=[("OMIM:1", "A", 0.8)])
    g2 = _gene_record("G2", 0.7, ensembl="E2", entrez="2", diseases=[("OMIM:2", "B", 0.7)])
    jsonl = _write_jsonl(tmp_path / "out.jsonl", [g1, g2])
    out = _write_baseline_ranking(
        tmp_path,
        [jsonl],
        sample="s",
        assembly="hg38",
        jar="/x/exomiser-cli-15.0.0.jar",
        exomiser_path="/x",
        max_genes=1,
        max_diseases=1,
    )
    assert out is not None
    d = json.loads(out.read_text())
    assert len(d["geneRanking"]) == 1 and d["totalGenes"] == 2
    assert len(d["diseaseRanking"]) == 1 and d["totalDiseases"] == 2


def test_write_baseline_ranking_no_jsonl_returns_none(tmp_path: Path) -> None:
    assert (
        _write_baseline_ranking(
            tmp_path, [], sample="s", assembly="hg38", jar="/x/j.jar", exomiser_path="/x"
        )
        is None
    )


# --------------------------------------------------------------------------- #
# _baseline_run_metadata
# --------------------------------------------------------------------------- #
def test_baseline_run_metadata_reads_manifest(tmp_path: Path) -> None:
    (tmp_path / "openscientist-exomiser-manifest.txt").write_text(
        "exomiser_cli_version=15.0.0\nexomiser_data_version=2512\nassembly=hg38\n"
    )
    md = _baseline_run_metadata(
        "patient.yml", "hg38", str(tmp_path / "exomiser-cli-15.0.0.jar"), str(tmp_path)
    )
    assert md == {
        "sampleId": "patient",
        "assembly": "hg38",
        "exomiserVersion": "15.0.0",
        "dataVersion": "2512",
    }


def test_baseline_run_metadata_missing_manifest(tmp_path: Path) -> None:
    md = _baseline_run_metadata(
        "p.yml", "hg19", str(tmp_path / "exomiser-cli-14.0.0.jar"), str(tmp_path)
    )
    assert md["exomiserVersion"] == "14.0.0"
    assert md["dataVersion"] is None


# --------------------------------------------------------------------------- #
# _exomiser_installed (registration gate)
# --------------------------------------------------------------------------- #
def test_exomiser_installed_true(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "exomiser-cli-15.0.0.jar").write_text("")
    monkeypatch.setenv("EXOMISER_PATH", str(tmp_path))
    assert _exomiser_installed() is True


def test_exomiser_installed_false_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMISER_PATH", raising=False)
    assert _exomiser_installed() is False


def test_exomiser_installed_false_no_jar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMISER_PATH", str(tmp_path))
    assert _exomiser_installed() is False


# --------------------------------------------------------------------------- #
# run_exomiser (in-process, mocked subprocess + setup)
# --------------------------------------------------------------------------- #
def _mock_exomiser_setup(monkeypatch: pytest.MonkeyPatch, exomiser_path: str) -> None:
    monkeypatch.setattr(exo, "setup_exomiser_env", lambda: {"PATH": "/usr/bin"})
    monkeypatch.setattr(
        exo,
        "get_settings",
        lambda: SimpleNamespace(exomiser=SimpleNamespace(exomiser_path=exomiser_path)),
    )
    monkeypatch.setattr(
        exo, "find_exomiser_jar", lambda p: f"{exomiser_path}/exomiser-cli-15.0.0.jar"
    )


def test_run_exomiser_happy_path_emits_ranking(
    monkeypatch: pytest.MonkeyPatch,
    state_job_dir: Path,
    patched_ks_persistence: KnowledgeState,
) -> None:
    (state_job_dir / "data" / "patient.yml").write_text("phenopacket")
    _mock_exomiser_setup(monkeypatch, "/fake/exo")

    record = _gene_record(
        "FGFR2",
        0.9614,
        ensembl="ENSG00000066468",
        entrez="2263",
        diseases=[("OMIM:101600", "Craniofacial dysplasia", 0.86)],
    )

    def _fake_run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        out_dir = Path(cmd[cmd.index("--output-directory") + 1])
        _write_jsonl(out_dir / "patient-exomiser.jsonl", [record])
        return subprocess.CompletedProcess(cmd, 0, stdout="Exomising finished - Bye!", stderr="")

    monkeypatch.setattr("openscientist_tools.exomiser.subprocess.run", _fake_run)

    result = json.loads(run_exomiser(sample="patient.yml", preset="exome"))
    assert result["ok"] is True
    assert result["returncode"] == 0
    ranking_path = result["exomiser_ranking"]
    assert ranking_path and Path(ranking_path).exists()

    ranking = json.loads(Path(ranking_path).read_text())
    assert ranking["geneRanking"][0]["geneSymbol"] == "FGFR2"
    assert ranking["geneRanking"][0]["acmgClassification"] == "PATHOGENIC"
    assert ranking["diseaseRanking"][0]["diseaseId"] == "OMIM:101600"

    last = patched_ks_persistence.data["analysis_log"][-1]
    assert last["action"] == "run_exomiser" and last["success"] is True


def test_run_exomiser_invalid_preset(monkeypatch: pytest.MonkeyPatch, state_job_dir: Path) -> None:
    (state_job_dir / "data" / "patient.yml").write_text("x")
    _mock_exomiser_setup(monkeypatch, "/fake/exo")
    result = json.loads(run_exomiser(sample="patient.yml", preset="bogus"))
    assert result["ok"] is False
    assert "Invalid preset" in result["error"]


def test_run_exomiser_sample_not_found(
    monkeypatch: pytest.MonkeyPatch, state_job_dir: Path
) -> None:
    _mock_exomiser_setup(monkeypatch, "/fake/exo")
    result = json.loads(run_exomiser(sample="missing.yml", preset="exome"))
    assert result["ok"] is False
    assert "not found" in result["error"]


def test_run_exomiser_not_configured(monkeypatch: pytest.MonkeyPatch, state_job_dir: Path) -> None:
    monkeypatch.setattr(exo, "setup_exomiser_env", lambda: None)
    result = json.loads(run_exomiser(sample="patient.yml", preset="exome"))
    assert result["ok"] is False
    assert "EXOMISER_PATH not configured" in result["error"]


def test_run_exomiser_timeout(
    monkeypatch: pytest.MonkeyPatch,
    state_job_dir: Path,
    patched_ks_persistence: KnowledgeState,
) -> None:
    (state_job_dir / "data" / "patient.yml").write_text("x")
    _mock_exomiser_setup(monkeypatch, "/fake/exo")

    def _raise_timeout(*_a: object, **_kw: object) -> None:
        raise subprocess.TimeoutExpired(cmd="java", timeout=1800)

    monkeypatch.setattr("openscientist_tools.exomiser.subprocess.run", _raise_timeout)
    result = json.loads(run_exomiser(sample="patient.yml", preset="exome"))
    assert result["ok"] is False
    assert "timed out" in result["error"]


# --------------------------------------------------------------------------- #
# Conditional registration over the real MCP server (no Exomiser install)
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def _spawned_for_job(
    server_env: Callable[..., dict[str, str]],
    server_params: Callable[[dict[str, str]], StdioServerParameters],
    tmp_path: Path,
    test_database_url: str,
    job_id: UUID,
) -> AsyncGenerator[ClientSession, None]:
    import os

    async with AsyncSessionLocal(thread_safe=True) as setup:
        setup.add(
            Job(
                id=job_id,
                research_question="exomiser registration test",
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
        env.pop("EXOMISER_PATH", None)  # ensure the tool is NOT registered
        params = server_params(env)
        params.cwd = str(tmp_path)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    finally:
        async with AsyncSessionLocal(thread_safe=True) as cleanup:
            await cleanup.execute(delete(Job).where(Job.id == job_id))
            await cleanup.commit()


async def test_run_exomiser_absent_when_unconfigured(
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
        assert "run_exomiser" not in names
        # other tools still register fine
        assert "search_pubmed" in names
