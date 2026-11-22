from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest
from rdflib import URIRef

from openscientist.evidence import hcmo_export

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "hcmo_evidence"
SNAPSHOT = EXAMPLE / "job-snapshot.json"


def _snapshot() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(SNAPSHOT.read_text(encoding="utf-8")))


def test_complete_export_passes_all_gates(tmp_path: Path) -> None:
    validation = hcmo_export.export_hcmo_evidence(
        SNAPSHOT,
        tmp_path,
        source_root=EXAMPLE,
        report_path=EXAMPLE / "final_report.md",
    )

    assert validation["valid"] is True
    assert validation["syntax"]["triples"] > 100
    assert validation["shacl"]["focus_nodes"] > 0
    assert validation["closed_world_vocabulary"]["undeclared_terms"] == []
    assert (
        hashlib.sha256((tmp_path / "semantic-manifest.json").read_bytes()).hexdigest()
        == validation["artifacts"]["semantic_manifest_sha256"]
    )
    assert (
        hashlib.sha256((tmp_path / "evidence.ttl").read_bytes()).hexdigest()
        == validation["artifacts"]["evidence_sha256"]
    )
    appendix = (tmp_path / "traceability-appendix.md").read_text(encoding="utf-8")
    assert "F001: Mean dark-phase activity" in appendix
    assert "A001" in appendix
    assert "PMID:12884972" in appendix


def test_unknown_analysis_reference_fails_closed() -> None:
    snapshot = _snapshot()
    snapshot["findings"][0]["analysis_ids"] = ["A404"]

    with pytest.raises(hcmo_export.EvidenceExportError, match="unknown analysis_log"):
        hcmo_export.validate_snapshot(snapshot)


def test_source_hash_is_verified(tmp_path: Path) -> None:
    snapshot = _snapshot()
    snapshot["data_files"][0]["sha256"] = "0" * 64
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")

    with pytest.raises(hcmo_export.EvidenceExportError, match="SHA-256 mismatch"):
        hcmo_export.export_hcmo_evidence(path, tmp_path / "out", source_root=EXAMPLE)


def test_source_path_cannot_escape_declared_root(tmp_path: Path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    outside = tmp_path / "outside.csv"
    outside.write_text("untrusted\n", encoding="utf-8")
    snapshot = _snapshot()
    snapshot["data_files"][0].update(
        {
            "file_path": "../outside.csv",
            "file_size": outside.stat().st_size,
            "sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
        }
    )

    with pytest.raises(hcmo_export.EvidenceExportError, match="escapes allowed root"):
        hcmo_export.verify_source_files(snapshot, source_root)


def test_agent_workspace_rejects_explicit_path_escape(tmp_path: Path) -> None:
    with pytest.raises(hcmo_export.EvidenceExportError, match="snapshot escapes allowed root"):
        hcmo_export.export_hcmo_evidence(
            SNAPSHOT,
            tmp_path / "out",
            workspace_root=tmp_path,
        )


def test_citation_grounding_is_rechecked() -> None:
    snapshot = _snapshot()
    snapshot["findings"][0]["citations"][0]["snippet"] = "Absent from the abstract."

    with pytest.raises(hcmo_export.EvidenceExportError, match="is not grounded"):
        hcmo_export.validate_snapshot(snapshot)


def test_semantic_manifest_requires_pinned_hashes() -> None:
    snapshot = _snapshot()
    snapshot["semantic_manifest"]["vocabularies"][0]["sha256"] = "unpinned"

    with pytest.raises(hcmo_export.EvidenceExportError, match="invalid SHA-256"):
        hcmo_export.validate_snapshot(snapshot)


def test_closed_vocabulary_rejects_invented_hcmo_term() -> None:
    graph = hcmo_export.EvidenceGraphBuilder(_snapshot()).build()
    graph.add(
        (
            URIRef("urn:test"),
            URIRef(str(hcmo_export.HCM) + "plausibleButInvented"),
            URIRef("urn:value"),
        )
    )
    profile = hcmo_export.DEFAULT_PROFILE.read_text(encoding="utf-8")

    result = hcmo_export._vocabulary_check(profile, graph)

    assert result["conforms"] is False
    assert result["undeclared_terms"] == ["https://w3id.org/hcmo/ontology/hcm#plausibleButInvented"]


def test_shacl_rejects_finding_without_generating_analysis() -> None:
    graph = hcmo_export.EvidenceGraphBuilder(_snapshot()).build()
    finding = next(graph.subjects(hcmo_export.RDF.type, hcmo_export.OSC.Finding))
    graph.remove((finding, hcmo_export.PROV.wasGeneratedBy, None))
    shapes = hcmo_export.DEFAULT_SHAPES.read_text(encoding="utf-8")

    result = hcmo_export._shacl_check(graph, shapes)

    assert result["conforms"] is False
    assert any(
        item["focus_node"] == str(finding) and item["constraint"] == "MinCountConstraintComponent"
        for item in result["violations"]
    )


def test_shacl_rejects_one_unit_population_claim() -> None:
    snapshot = _snapshot()
    snapshot["findings"][0]["inference_scope"] = "population"
    graph = hcmo_export.EvidenceGraphBuilder(snapshot).build()
    shapes = hcmo_export.DEFAULT_SHAPES.read_text(encoding="utf-8")

    result = hcmo_export._shacl_check(graph, shapes)

    assert result["conforms"] is False
    assert any("population-level" in item["message"] for item in result["violations"])


def test_appendix_attachment_is_idempotent() -> None:
    appendix = f"{hcmo_export.APPENDIX_BEGIN}\nexample\n{hcmo_export.APPENDIX_END}\n"
    report = "# Report\n"

    once = hcmo_export.attach_appendix(report, appendix)
    twice = hcmo_export.attach_appendix(once, appendix)

    assert once == twice
    assert twice.count(hcmo_export.APPENDIX_BEGIN) == 1
