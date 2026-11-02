#!/usr/bin/env python3
"""Export a normalized OpenScientist job as an HCMO/PROV/STATO evidence bundle.

This is an experimental, offline adapter. OpenScientist's database remains
authoritative; the adapter reads a snapshot and writes derivative artifacts.
It deliberately requires explicit finding-to-analysis, data, result, and
literature references rather than asking an LLM to reconstruct provenance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

from pyshacl import validate as pyshacl_validate
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import DCTERMS, OWL, RDF, RDFS, XSD
from rdflib.query import ResultRow

OSC = Namespace("https://example.org/openscientist/evidence#")
# These are canonical vocabulary identifiers, not transport endpoints. Changing
# their schemes would create different RDF terms rather than make a request safer.
PROV = Namespace("http://www.w3.org/ns/prov#")  # NOSONAR
SCHEMA = Namespace("http://schema.org/")  # NOSONAR
SOSA = Namespace("http://www.w3.org/ns/sosa/")  # NOSONAR
QUDT = Namespace("http://qudt.org/schema/qudt/")  # NOSONAR
TIME = Namespace("http://www.w3.org/2006/time#")  # NOSONAR
SH = Namespace("http://www.w3.org/ns/shacl#")  # NOSONAR
HCM = Namespace("https://w3id.org/hcmo/ontology/hcm#")
HCM_BIO = Namespace("https://w3id.org/hcmo/ontology/hcm/bio#")
HCM_OBS = Namespace("https://w3id.org/hcmo/ontology/hcm/obs#")
HCM_TECH = Namespace("https://w3id.org/hcmo/ontology/hcm/tech#")

RESOURCE_ROOT = Path(__file__).with_name("resources")
DEFAULT_PROFILE = RESOURCE_ROOT / "approved-vocabulary.ttl"
DEFAULT_SHAPES = RESOURCE_ROOT / "evidence-shapes.ttl"
APPENDIX_BEGIN = "<!-- BEGIN OPENSCIENTIST TRACEABILITY APPENDIX -->"
APPENDIX_END = "<!-- END OPENSCIENTIST TRACEABILITY APPENDIX -->"
COLLECTION_NAMES = (
    "hypotheses",
    "findings",
    "literature",
    "analysis_log",
    "data_files",
    "statistical_results",
)
FINDING_REFERENCES = {
    "supporting_hypotheses": "hypotheses",
    "analysis_ids": "analysis_log",
    "data_file_ids": "data_files",
    "result_ids": "statistical_results",
    "literature_support": "literature",
}
INFERENCE_SCOPES = frozenset({"individual", "sample", "population", "causal"})


class EvidenceExportError(ValueError):
    """A snapshot cannot support an honest evidence export."""


def _require(record: dict[str, Any], fields: Iterable[str], context: str) -> None:
    missing = [field for field in fields if record.get(field) in (None, "")]
    if missing:
        raise EvidenceExportError(f"{context} missing: {', '.join(missing)}")


def _load_snapshot(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise EvidenceExportError("snapshot root must be an object")
    return value


def _canonical_json(value: Any) -> str:
    """Serialize a manifest deterministically for hashing and publication."""
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def _require_object(container: dict[str, Any], key: str, context: str) -> dict[str, Any]:
    value = container.get(key)
    if not isinstance(value, dict):
        raise EvidenceExportError(f"{context} must be an object")
    return value


def _validate_collections(snapshot: dict[str, Any]) -> dict[str, set[str]]:
    """Validate normalized record collections and return their ID indexes."""
    ids: dict[str, set[str]] = {}
    for name in COLLECTION_NAMES:
        records = snapshot.get(name)
        if not isinstance(records, list):
            raise EvidenceExportError(f"snapshot.{name} must be a list")
        record_ids: list[str] = []
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                raise EvidenceExportError(f"snapshot.{name}[{index}] must be an object")
            _require(record, ("id",), f"snapshot.{name}[{index}]")
            record_ids.append(str(record["id"]))
        if len(record_ids) != len(set(record_ids)):
            raise EvidenceExportError(f"snapshot.{name} contains duplicate IDs")
        ids[name] = set(record_ids)
    return ids


def _validate_semantic_context(snapshot: dict[str, Any]) -> None:
    semantic = _require_object(snapshot, "semantic_context", "snapshot.semantic_context")
    for name in ("enclosure", "subject", "sensor", "observation"):
        if not isinstance(semantic.get(name), dict):
            raise EvidenceExportError(f"snapshot.semantic_context.{name} must be an object")


def _validate_semantic_manifest(snapshot: dict[str, Any]) -> None:
    manifest = _require_object(snapshot, "semantic_manifest", "snapshot.semantic_manifest")
    _require(
        manifest,
        ("contract_version", "authoritative_state", "projection_mode"),
        "semantic_manifest",
    )
    vocabularies = manifest.get("vocabularies")
    if not isinstance(vocabularies, list) or not vocabularies:
        raise EvidenceExportError("snapshot.semantic_manifest.vocabularies must be non-empty")
    vocabulary_ids: list[str] = []
    for index, vocabulary in enumerate(vocabularies):
        if not isinstance(vocabulary, dict):
            raise EvidenceExportError(f"semantic_manifest.vocabularies[{index}] must be an object")
        _require(
            vocabulary,
            ("id", "version", "version_iri", "sha256"),
            f"semantic_manifest.vocabularies[{index}]",
        )
        if not re.fullmatch(r"[0-9a-f]{64}", str(vocabulary["sha256"])):
            raise EvidenceExportError(
                f"semantic_manifest vocabulary {vocabulary['id']} has an invalid SHA-256"
            )
        vocabulary_ids.append(str(vocabulary["id"]))
    if len(vocabulary_ids) != len(set(vocabulary_ids)):
        raise EvidenceExportError("snapshot.semantic_manifest.vocabularies has duplicate IDs")


def _validate_findings(snapshot: dict[str, Any], ids: dict[str, set[str]]) -> None:
    for finding in snapshot["findings"]:
        _require(
            finding,
            (
                "title",
                "evidence",
                "iteration_discovered",
                "inference_scope",
                "experimental_unit_count",
            ),
            "finding",
        )
        if finding["inference_scope"] not in INFERENCE_SCOPES:
            raise EvidenceExportError(
                f"finding {finding['id']} has invalid inference_scope {finding['inference_scope']!r}"
            )
        if int(finding["experimental_unit_count"]) < 1:
            raise EvidenceExportError(
                f"finding {finding['id']} experimental_unit_count must be positive"
            )
        for field, target in FINDING_REFERENCES.items():
            values = finding.get(field, [])
            if not isinstance(values, list):
                raise EvidenceExportError(f"finding {finding['id']}.{field} must be a list")
            unknown = sorted(set(map(str, values)) - ids[target])
            if unknown:
                raise EvidenceExportError(
                    f"finding {finding['id']} references unknown {target}: {unknown}"
                )


def _validate_analysis_references(snapshot: dict[str, Any], ids: dict[str, set[str]]) -> None:
    for analysis in snapshot["analysis_log"]:
        for field, target in (
            ("input_file_ids", "data_files"),
            ("output_result_ids", "statistical_results"),
        ):
            unknown = sorted(set(map(str, analysis.get(field, []))) - ids[target])
            if unknown:
                raise EvidenceExportError(
                    f"analysis {analysis['id']} references unknown {target}: {unknown}"
                )


def _normalize_citation_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip()).strip(".,;:!?\"'()[]{}")


def _citation_is_grounded(snippet: str, abstract: str, status: Any) -> bool:
    if status == "verified":
        return snippet in abstract
    if status == "verified_normalized":
        normalized_snippet = _normalize_citation_text(snippet)
        return bool(normalized_snippet) and normalized_snippet in _normalize_citation_text(abstract)
    return False


def _validate_citations(snapshot: dict[str, Any]) -> None:
    literature_by_pmid = {
        str(item.get("pmid")): item for item in snapshot["literature"] if item.get("pmid")
    }
    for finding in snapshot["findings"]:
        for citation in finding.get("citations", []):
            pmid = str(citation.get("pmid") or "")
            paper = literature_by_pmid.get(pmid)
            if paper is None:
                raise EvidenceExportError(f"finding {finding['id']} cites absent PMID {pmid}")
            snippet = str(citation.get("snippet") or "")
            abstract = str(paper.get("abstract") or "")
            status = citation.get("validation_status")
            if not _citation_is_grounded(snippet, abstract, status):
                raise EvidenceExportError(
                    f"finding {finding['id']} citation PMID {pmid} is not grounded"
                )


def validate_snapshot(snapshot: dict[str, Any]) -> None:
    """Validate structure, references, and stored citation grounding."""
    config = _require_object(snapshot, "config", "snapshot.config")
    _require(config, ("job_id", "research_question", "started_at"), "config")
    ids = _validate_collections(snapshot)
    _validate_semantic_context(snapshot)
    _validate_semantic_manifest(snapshot)
    _validate_findings(snapshot, ids)
    _validate_analysis_references(snapshot, ids)
    _validate_citations(snapshot)


def _resolve_within(root: Path, path: Path, context: str) -> Path:
    """Resolve a path and reject absolute, parent, or symlink escapes."""
    resolved_root = root.resolve()
    candidate = path if path.is_absolute() else resolved_root / path
    resolved = candidate.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise EvidenceExportError(
            f"{context} escapes allowed root {resolved_root}: {path}"
        ) from exc
    return resolved


def verify_source_files(snapshot: dict[str, Any], source_root: Path) -> list[dict[str, Any]]:
    """Fail closed if source bytes differ from the snapshot manifest."""
    verified: list[dict[str, Any]] = []
    for record in snapshot["data_files"]:
        _require(record, ("file_path", "file_size", "sha256"), f"data file {record['id']}")
        path = _resolve_within(
            source_root,
            Path(record["file_path"]),
            f"data file {record['id']}",
        )
        if not path.is_file():
            raise EvidenceExportError(f"data file {record['id']} does not exist: {path}")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        actual_hash = digest.hexdigest()
        actual_size = path.stat().st_size
        if actual_size != int(record["file_size"]):
            raise EvidenceExportError(f"data file {record['id']} size mismatch")
        if actual_hash != str(record["sha256"]).lower():
            raise EvidenceExportError(f"data file {record['id']} SHA-256 mismatch")
        verified.append(
            {
                "id": str(record["id"]),
                "path": str(record["file_path"]),
                "bytes": actual_size,
                "sha256": actual_hash,
            }
        )
    return verified


class EvidenceGraphBuilder:
    """Deterministically map a normalized snapshot to the evidence graph."""

    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.snapshot = snapshot
        job_id = quote(str(snapshot["config"]["job_id"]), safe="")
        self.base = f"https://example.org/openscientist/jobs/{job_id}/"
        self.graph = Graph()
        for prefix, namespace in (
            ("osc", OSC),
            ("prov", PROV),
            ("schema", SCHEMA),
            ("sosa", SOSA),
            ("qudt", QUDT),
            ("time", TIME),
            ("hcm", HCM),
            ("hcm-bio", HCM_BIO),
            ("hcm-obs", HCM_OBS),
            ("hcm-tech", HCM_TECH),
            ("dcterms", DCTERMS),
            ("rdfs", RDFS),
            ("xsd", XSD),
        ):
            self.graph.bind(prefix, namespace)

    def node(self, kind: str, identifier: str) -> URIRef:
        return URIRef(f"{self.base}{kind}/{quote(str(identifier), safe='')}")

    def _interval(self, identifier: str, start: str, end: str) -> URIRef:
        interval = self.node("interval", identifier)
        beginning = self.node("instant", f"{identifier}-start")
        ending = self.node("instant", f"{identifier}-end")
        self.graph.add((interval, RDF.type, TIME.Interval))
        self.graph.add((interval, TIME.hasBeginning, beginning))
        self.graph.add((interval, TIME.hasEnd, ending))
        self.graph.add((beginning, RDF.type, TIME.Instant))
        self.graph.add((beginning, TIME.inXSDDateTime, Literal(start, datatype=XSD.dateTime)))
        self.graph.add((ending, RDF.type, TIME.Instant))
        self.graph.add((ending, TIME.inXSDDateTime, Literal(end, datatype=XSD.dateTime)))
        return interval

    def _add_job(self) -> URIRef:
        config = self.snapshot["config"]
        job = URIRef(f"{self.base}investigation")
        self.graph.add((job, RDF.type, OSC.Investigation))
        self.graph.add((job, RDF.type, PROV.Activity))
        self.graph.add((job, DCTERMS.identifier, Literal(str(config["job_id"]))))
        self.graph.add((job, DCTERMS.title, Literal(config.get("short_title", "Investigation"))))
        self.graph.add((job, OSC.researchQuestion, Literal(config["research_question"])))
        self.graph.add((job, OSC.jobStatus, Literal(config.get("status", "completed"))))
        self.graph.add((job, OSC.maxIterations, Literal(int(config.get("max_iterations", 1)))))
        self.graph.add(
            (job, PROV.startedAtTime, Literal(config["started_at"], datatype=XSD.dateTime))
        )
        if ended := config.get("ended_at"):
            self.graph.add((job, PROV.endedAtTime, Literal(ended, datatype=XSD.dateTime)))
        if provider := config.get("llm_provider"):
            self.graph.add((job, OSC.llmProvider, Literal(provider)))
        return job

    def _add_semantic_manifest(self, job: URIRef) -> URIRef:
        manifest = self.snapshot["semantic_manifest"]
        manifest_node = self.node("semantic-manifest", "profile")
        canonical = _canonical_json(manifest)
        self.graph.add((manifest_node, RDF.type, OSC.SemanticManifest))
        self.graph.add((manifest_node, RDF.type, PROV.Entity))
        self.graph.add((manifest_node, OSC.semanticManifestSha256, Literal(_sha256(canonical))))
        self.graph.add(
            (manifest_node, OSC.evidenceContractVersion, Literal(manifest["contract_version"]))
        )
        self.graph.add(
            (manifest_node, OSC.authoritativeState, Literal(manifest["authoritative_state"]))
        )
        self.graph.add((manifest_node, OSC.projectionMode, Literal(manifest["projection_mode"])))
        self.graph.add((job, OSC.hasSemanticManifest, manifest_node))
        self.graph.add((job, PROV.used, manifest_node))
        for vocabulary in manifest["vocabularies"]:
            node = self.node("vocabulary", vocabulary["id"])
            self.graph.add((node, RDF.type, OSC.VocabularySnapshot))
            self.graph.add((node, RDF.type, PROV.Entity))
            self.graph.add((node, DCTERMS.identifier, Literal(vocabulary["id"])))
            self.graph.add((node, OSC.vocabularyVersion, Literal(vocabulary["version"])))
            self.graph.add((node, OSC.versionIri, URIRef(vocabulary["version_iri"])))
            self.graph.add((node, OSC.sha256, Literal(vocabulary["sha256"])))
            self.graph.add((manifest_node, OSC.includesVocabulary, node))
        return manifest_node

    def _add_hcmo_context(self) -> dict[str, URIRef]:
        context = self.snapshot["semantic_context"]
        enclosure_data = context["enclosure"]
        subject_data = context["subject"]
        sensor_data = context["sensor"]
        observation_data = context["observation"]
        for record, name in (
            (enclosure_data, "enclosure"),
            (subject_data, "subject"),
            (sensor_data, "sensor"),
            (observation_data, "observation"),
        ):
            _require(record, ("id",), f"semantic_context.{name}")

        enclosure = self.node("hcmo/enclosure", enclosure_data["id"])
        dimensions = self.node("hcmo/dimensions", f"{enclosure_data['id']}-dimensions")
        self.graph.add((enclosure, RDF.type, HCM.MonitoredEnclosure))
        self.graph.add((enclosure, RDFS.label, Literal(enclosure_data["label"])))
        self.graph.add(
            (enclosure, HCM.hasEnclosureIdentifier, Literal(enclosure_data["identifier"]))
        )
        self.graph.add((enclosure, HCM.hasDimensions, dimensions))
        self.graph.add((dimensions, RDF.type, HCM.EnclosureDimensions))
        for axis, predicate in (
            ("width", HCM.hasWidthQuantity),
            ("length", HCM.hasLengthQuantity),
            ("height", HCM.hasHeightQuantity),
        ):
            quantity = self.node("hcmo/quantity", f"{enclosure_data['id']}-{axis}")
            self.graph.add((dimensions, predicate, quantity))
            self.graph.add((quantity, RDF.type, QUDT.QuantityValue))
            self.graph.add(
                (quantity, QUDT.numericValue, Literal(Decimal(str(enclosure_data[axis]))))
            )
            self.graph.add((quantity, QUDT.hasUnit, URIRef(enclosure_data["dimension_unit_iri"])))

        subject = self.node("hcmo/subject", subject_data["id"])
        assignment = self.node("hcmo/assignment", f"{subject_data['id']}-{enclosure_data['id']}")
        self.graph.add((subject, RDF.type, HCM_BIO.Subject))
        self.graph.add((subject, RDFS.label, Literal(subject_data["label"])))
        self.graph.add((subject, HCM_BIO.hasSpecies, Literal(subject_data["species"])))
        self.graph.add((subject, HCM_BIO.hasHousingAssignment, assignment))
        self.graph.add((assignment, RDF.type, HCM_BIO.HousingAssignment))
        self.graph.add((assignment, HCM_BIO.assignedToEnclosure, enclosure))
        housing = self._interval(
            "housing", subject_data["housing_start"], subject_data["housing_end"]
        )
        self.graph.add((assignment, TIME.hasTime, housing))

        sensor = self.node("hcmo/sensor", sensor_data["id"])
        observed = self.node("hcmo/property", sensor_data["observed_property_id"])
        self.graph.add((sensor, RDF.type, HCM_TECH.Sensor))
        self.graph.add((sensor, RDFS.label, Literal(sensor_data["label"])))
        self.graph.add((sensor, HCM_TECH.hasSensorIdentifier, Literal(sensor_data["identifier"])))
        self.graph.add((sensor, HCM_TECH.installedIn, enclosure))
        self.graph.add((sensor, HCM_TECH.captures, observed))
        self.graph.add((enclosure, HCM_TECH.monitoredBy, sensor))
        self.graph.add((observed, RDF.type, SOSA.ObservableProperty))
        self.graph.add((observed, RDFS.label, Literal(sensor_data["observed_property_label"])))

        observation = self.node("hcmo/observation", observation_data["id"])
        behavior_result = self.node("hcmo/result", observation_data["result_id"])
        condition = self.node("hcmo/condition", observation_data["condition_id"])
        self.graph.add((observation, RDF.type, HCM_OBS.BehaviorObservation))
        self.graph.add((observation, SOSA.hasFeatureOfInterest, subject))
        self.graph.add((observation, SOSA.madeBySensor, sensor))
        self.graph.add((observation, SOSA.observedProperty, observed))
        self.graph.add((observation, SOSA.hasResult, behavior_result))
        self.graph.add((observation, HCM_OBS.occursIn, enclosure))
        self.graph.add((observation, HCM_OBS.hasCondition, condition))
        observation_time = self._interval(
            "observation", observation_data["started_at"], observation_data["ended_at"]
        )
        self.graph.add((observation, SOSA.phenomenonTime, observation_time))
        self.graph.add((behavior_result, RDF.type, HCM_OBS.BehaviorResult))
        self.graph.add(
            (behavior_result, HCM_OBS.hasBehaviorType, Literal(observation_data["behavior_type"]))
        )
        self.graph.add((condition, RDFS.label, Literal(observation_data["condition_label"])))
        return {"observation": observation}

    def _add_data_files(self, job: URIRef, observation: URIRef) -> dict[str, URIRef]:
        files: dict[str, URIRef] = {}
        for record in self.snapshot["data_files"]:
            node = self.node("data-file", record["id"])
            files[str(record["id"])] = node
            for type_iri in (OSC.DataFile, PROV.Entity, HCM_TECH.TimeSeries):
                self.graph.add((node, RDF.type, type_iri))
            self.graph.add((node, DCTERMS.title, Literal(record["filename"])))
            self.graph.add((node, HCM_TECH.hasFileFormat, Literal(record["mime_type"])))
            self.graph.add((node, HCM_TECH.hasStoragePath, Literal(record["file_path"])))
            self.graph.add((node, OSC.byteSize, Literal(int(record["file_size"]))))
            self.graph.add((node, OSC.sha256, Literal(record["sha256"])))
            self.graph.add((node, OSC.recordsObservation, observation))
            self.graph.add((job, PROV.used, node))
        return files

    def _add_hypotheses(self, job: URIRef) -> dict[str, URIRef]:
        hypotheses: dict[str, URIRef] = {}
        for record in self.snapshot["hypotheses"]:
            node = self.node("hypothesis", record["id"])
            hypotheses[str(record["id"])] = node
            self.graph.add((node, RDF.type, OSC.Hypothesis))
            self.graph.add((node, RDF.type, PROV.Entity))
            self.graph.add((node, DCTERMS.identifier, Literal(str(record["id"]))))
            self.graph.add((node, DCTERMS.description, Literal(record["statement"])))
            self.graph.add((node, OSC.hypothesisStatus, Literal(record["status"])))
            self.graph.add((node, OSC.iteration, Literal(int(record["iteration_proposed"]))))
            self.graph.add((node, PROV.wasGeneratedBy, job))
            if strategy := record.get("test_strategy") or record.get("test_code"):
                self.graph.add((node, OSC.testStrategy, Literal(strategy)))
            self.graph.add((job, OSC.hasHypothesis, node))
        return hypotheses

    def _add_statistical_results(self) -> dict[str, URIRef]:
        results: dict[str, URIRef] = {}
        for record in self.snapshot["statistical_results"]:
            node = self.node("statistical-result", record["id"])
            results[str(record["id"])] = node
            for type_iri in (OSC.StatisticalResult, PROV.Entity, URIRef(record["type_iri"])):
                self.graph.add((node, RDF.type, type_iri))
            self.graph.add((node, RDFS.label, Literal(record["label"])))
            self.graph.add((node, SCHEMA.value, Literal(Decimal(str(record["value"])))))
            self.graph.add((node, QUDT.hasUnit, URIRef(record["unit_iri"])))
        return results

    def _add_analyses(
        self,
        job: URIRef,
        files: dict[str, URIRef],
        results: dict[str, URIRef],
    ) -> dict[str, URIRef]:
        analyses: dict[str, URIRef] = {}
        for record in self.snapshot["analysis_log"]:
            node = self.node("analysis", record["id"])
            analyses[str(record["id"])] = node
            self.graph.add((node, RDF.type, OSC.AnalysisActivity))
            self.graph.add((node, RDF.type, PROV.Activity))
            self.graph.add((node, DCTERMS.identifier, Literal(str(record["id"]))))
            self.graph.add((node, DCTERMS.description, Literal(record["description"])))
            self.graph.add((node, OSC.iteration, Literal(int(record["iteration"]))))
            self.graph.add((node, OSC.analysisAction, Literal(record["action"])))
            self.graph.add((node, OSC.success, Literal(bool(record["success"]))))
            self.graph.add((node, PROV.wasInformedBy, job))
            if timestamp := record.get("timestamp"):
                self.graph.add(
                    (node, PROV.startedAtTime, Literal(timestamp, datatype=XSD.dateTime))
                )
            if code_hash := record.get("code_sha256"):
                self.graph.add((node, OSC.codeSha256, Literal(code_hash)))
            for file_id in record.get("input_file_ids", []):
                self.graph.add((node, PROV.used, files[str(file_id)]))
            for result_id in record.get("output_result_ids", []):
                result = results[str(result_id)]
                self.graph.add((node, PROV.generated, result))
                self.graph.add((result, PROV.wasGeneratedBy, node))
        return analyses

    def _add_literature(self) -> tuple[dict[str, URIRef], dict[str, URIRef]]:
        literature: dict[str, URIRef] = {}
        literature_by_pmid: dict[str, URIRef] = {}
        for record in self.snapshot["literature"]:
            node = self.node("literature", record["id"])
            literature[str(record["id"])] = node
            for type_iri in (OSC.LiteratureReference, PROV.Entity, SCHEMA.ScholarlyArticle):
                self.graph.add((node, RDF.type, type_iri))
            self.graph.add((node, DCTERMS.title, Literal(record["title"])))
            if pmid := str(record.get("pmid") or ""):
                self.graph.add((node, OSC.pmid, Literal(pmid)))
                self.graph.add(
                    (node, SCHEMA.url, URIRef(f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"))
                )
                literature_by_pmid[pmid] = node
        return literature, literature_by_pmid

    def _add_citations(
        self,
        finding: URIRef,
        record: dict[str, Any],
        literature_by_pmid: dict[str, URIRef],
    ) -> None:
        for index, citation in enumerate(record.get("citations", []), 1):
            paper = literature_by_pmid[str(citation["pmid"])]
            citation_node = self.node("citation", f"{record['id']}-{index}")
            self.graph.add((citation_node, RDF.type, OSC.Citation))
            self.graph.add((citation_node, OSC.citesLiterature, paper))
            self.graph.add((citation_node, OSC.citationSnippet, Literal(citation["snippet"])))
            self.graph.add(
                (
                    citation_node,
                    OSC.citationValidationStatus,
                    Literal(citation["validation_status"]),
                )
            )
            self.graph.add((finding, OSC.hasCitation, citation_node))

    def _add_findings(
        self,
        job: URIRef,
        hypotheses: dict[str, URIRef],
        analyses: dict[str, URIRef],
        files: dict[str, URIRef],
        results: dict[str, URIRef],
        literature: dict[str, URIRef],
        literature_by_pmid: dict[str, URIRef],
    ) -> None:
        for record in self.snapshot["findings"]:
            node = self.node("finding", record["id"])
            self.graph.add((node, RDF.type, OSC.Finding))
            self.graph.add((node, RDF.type, PROV.Entity))
            self.graph.add((node, DCTERMS.identifier, Literal(str(record["id"]))))
            self.graph.add((node, DCTERMS.title, Literal(record["title"])))
            self.graph.add((node, OSC.evidenceText, Literal(record["evidence"])))
            self.graph.add((node, OSC.findingStatus, Literal(record.get("status", "supported"))))
            self.graph.add((node, OSC.inferenceScope, Literal(record["inference_scope"])))
            self.graph.add(
                (
                    node,
                    OSC.experimentalUnitCount,
                    Literal(int(record["experimental_unit_count"])),
                )
            )
            self.graph.add((node, OSC.iteration, Literal(int(record["iteration_discovered"]))))
            self.graph.add((job, OSC.hasFinding, node))
            for identifier in record.get("supporting_hypotheses", []):
                self.graph.add((node, OSC.addressesHypothesis, hypotheses[str(identifier)]))
            for identifier in record.get("analysis_ids", []):
                self.graph.add((node, PROV.wasGeneratedBy, analyses[str(identifier)]))
            for identifier in record.get("data_file_ids", []):
                self.graph.add((node, PROV.wasDerivedFrom, files[str(identifier)]))
            for identifier in record.get("result_ids", []):
                self.graph.add((node, PROV.wasDerivedFrom, results[str(identifier)]))
            for identifier in record.get("literature_support", []):
                self.graph.add((node, PROV.wasDerivedFrom, literature[str(identifier)]))
            self._add_citations(node, record, literature_by_pmid)

    def build(self) -> Graph:
        job = self._add_job()
        self._add_semantic_manifest(job)
        hcmo = self._add_hcmo_context()
        files = self._add_data_files(job, hcmo["observation"])
        hypotheses = self._add_hypotheses(job)
        results = self._add_statistical_results()
        analyses = self._add_analyses(job, files, results)
        literature, literature_by_pmid = self._add_literature()
        self._add_findings(
            job,
            hypotheses,
            analyses,
            files,
            results,
            literature,
            literature_by_pmid,
        )
        return self.graph


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _vocabulary_check(profile_text: str, evidence_graph: Graph) -> dict[str, Any]:
    profile = Graph().parse(data=profile_text, format="turtle")
    declaration_types = {OWL.Class, OWL.ObjectProperty, OWL.DatatypeProperty}
    declared = {
        subject
        for subject, _, object_ in profile.triples((None, RDF.type, None))
        if object_ in declaration_types
    }
    checked_namespaces = sorted(
        {
            str(subject).rsplit("/", 1)[0] + "/"
            if "#" not in str(subject)
            else str(subject).split("#", 1)[0] + "#"
            for subject in declared
            if isinstance(subject, URIRef)
        }
    )
    terms = {predicate for _, predicate, _ in evidence_graph if isinstance(predicate, URIRef)}
    terms.update(
        object_
        for _, _, object_ in evidence_graph.triples((None, RDF.type, None))
        if isinstance(object_, URIRef)
    )
    undeclared = sorted(
        str(term)
        for term in terms
        if any(str(term).startswith(namespace) for namespace in checked_namespaces)
        and term not in declared
    )
    return {
        "conforms": not undeclared,
        "ontology_terms": len(declared),
        "terms_checked": len(terms),
        "checked_namespaces": checked_namespaces,
        "undeclared_terms": undeclared,
    }


def _shacl_check(evidence_graph: Graph, shapes_text: str) -> dict[str, Any]:
    shapes = Graph().parse(data=shapes_text, format="turtle")
    conforms, report_graph, report_text = pyshacl_validate(
        evidence_graph, shacl_graph=shapes, inference="rdfs", serialize_report_graph=False
    )
    violations: list[dict[str, str]] = []
    for result in report_graph.subjects(RDF.type, SH.ValidationResult):
        violations.append(
            {
                "focus_node": str(report_graph.value(result, SH.focusNode) or ""),
                "path": str(report_graph.value(result, SH.resultPath) or ""),
                "message": str(report_graph.value(result, SH.resultMessage) or ""),
                "constraint": str(
                    report_graph.value(result, SH.sourceConstraintComponent) or ""
                ).rsplit("#", 1)[-1],
                "severity": str(report_graph.value(result, SH.resultSeverity) or "").rsplit("#", 1)[
                    -1
                ],
            }
        )
    focus_nodes: set[Any] = set()
    for target_class in shapes.objects(None, SH.targetClass):
        focus_nodes.update(evidence_graph.subjects(RDF.type, target_class))
    return {
        "conforms": bool(conforms),
        "count": len(violations),
        "focus_nodes": len(focus_nodes),
        "violations": violations,
        "text": str(report_text),
    }


def validate_graph(evidence_text: str, profile_text: str, shapes_text: str) -> dict[str, Any]:
    graph = Graph().parse(data=evidence_text, format="turtle")
    shacl = _shacl_check(graph, shapes_text)
    vocabulary = _vocabulary_check(profile_text, graph)
    return {
        "valid": shacl["conforms"] and vocabulary["conforms"],
        "syntax": {"ok": True, "triples": len(graph), "error": None},
        "shacl": shacl,
        "closed_world_vocabulary": vocabulary,
        "artifacts": {
            "profile_sha256": _sha256(profile_text),
            "shapes_sha256": _sha256(shapes_text),
            "evidence_sha256": _sha256(evidence_text),
        },
    }


def _traceability_query() -> str:
    return f"""
PREFIX osc: <{OSC}> PREFIX prov: <{PROV}> PREFIX dcterms: <{DCTERMS}>
PREFIX schema: <{SCHEMA}> PREFIX qudt: <{QUDT}> PREFIX rdfs: <{RDFS}>
SELECT ?finding ?findingId ?title ?evidence ?scope ?unitCount ?hypothesisId ?hypothesisText
       ?analysisId ?analysisDescription ?sourceTitle ?sourceSha
       ?resultLabel ?value ?unit ?paperTitle ?pmid WHERE {{
  ?finding a osc:Finding ; dcterms:identifier ?findingId ;
           dcterms:title ?title ; osc:evidenceText ?evidence ;
           osc:inferenceScope ?scope ; osc:experimentalUnitCount ?unitCount .
  OPTIONAL {{ ?finding osc:addressesHypothesis [ dcterms:identifier ?hypothesisId ;
             dcterms:description ?hypothesisText ] . }}
  OPTIONAL {{ ?finding prov:wasGeneratedBy [ dcterms:identifier ?analysisId ;
             dcterms:description ?analysisDescription ] . }}
  OPTIONAL {{ ?finding prov:wasDerivedFrom ?source . ?source a osc:DataFile ;
             dcterms:title ?sourceTitle ; osc:sha256 ?sourceSha . }}
  OPTIONAL {{ ?finding prov:wasDerivedFrom ?result . ?result a osc:StatisticalResult ;
             rdfs:label ?resultLabel ; schema:value ?value ; qudt:hasUnit ?unit . }}
  OPTIONAL {{ ?finding prov:wasDerivedFrom ?paper . ?paper a osc:LiteratureReference ;
             dcterms:title ?paperTitle . OPTIONAL {{ ?paper osc:pmid ?pmid }} }}
}} ORDER BY ?finding
"""


def _traceability_summary(binding: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(binding["findingId"]),
        "title": str(binding["title"]),
        "evidence": str(binding["evidence"]),
        "scope": str(binding["scope"]),
        "experimental_unit_count": str(binding["unitCount"]),
    }


def _traceability_values(binding: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    source_hash = binding.get("sourceSha")
    result_unit = str(binding.get("unit") or "").rsplit("/", 1)[-1]
    pmid = binding.get("pmid")
    return {
        "hypotheses": (binding.get("hypothesisId"), binding.get("hypothesisText")),
        "analyses": (binding.get("analysisId"), binding.get("analysisDescription")),
        "sources": (
            binding.get("sourceTitle"),
            f"sha256:{source_hash}" if source_hash else None,
        ),
        "results": (
            binding.get("resultLabel"),
            f"{binding.get('value')} {result_unit}",
        ),
        "papers": (
            binding.get("paperTitle"),
            f"PMID:{pmid}" if pmid else None,
        ),
    }


def _record_traceability_values(
    target: dict[str, set[str]], values: dict[str, tuple[Any, Any]]
) -> None:
    for field, (left, right) in values.items():
        if left:
            target[field].add(f"{left} ({right})" if right else str(left))


def _traceability_rows(evidence_text: str) -> list[dict[str, Any]]:
    graph = Graph().parse(data=evidence_text, format="turtle")
    grouped: dict[str, dict[str, Any]] = {}
    sets: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for binding in graph.query(_traceability_query()):
        binding_values = cast(ResultRow, binding).asdict()
        key = str(binding_values["finding"])
        grouped.setdefault(key, _traceability_summary(binding_values))
        _record_traceability_values(sets[key], _traceability_values(binding_values))
    for key, summary in grouped.items():
        for field in ("hypotheses", "analyses", "sources", "results", "papers"):
            summary[field] = sorted(sets[key][field])
    return [grouped[key] for key in sorted(grouped)]


def _escape(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def make_traceability_appendix(evidence_text: str, validation: dict[str, Any]) -> str:
    status = "PASS" if validation["valid"] else "FAIL"
    shacl = validation["shacl"]
    vocabulary = validation["closed_world_vocabulary"]
    lines = [
        APPENDIX_BEGIN,
        "## Traceability appendix",
        "",
        "This appendix was generated from the RDF evidence graph, not by the model that wrote the report.",
        "",
        f"- Overall evidence gate: **{status}**",
        f"- RDF syntax: **PASS** ({validation['syntax']['triples']} triples)",
        f"- SHACL: **{'PASS' if shacl['conforms'] else 'FAIL'}** "
        f"({shacl['focus_nodes']} focus nodes, {shacl['count']} violations)",
        f"- Closed-world vocabulary: **{'PASS' if vocabulary['conforms'] else 'FAIL'}** "
        f"({len(vocabulary['undeclared_terms'])} undeclared terms)",
        f"- Evidence SHA-256: `{validation['artifacts']['evidence_sha256']}`",
        "",
        "### Finding-to-evidence matrix",
        "",
        "| Finding | Scope | Hypothesis | Analysis | Data | Statistical result | Literature |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    rows = _traceability_rows(evidence_text)
    for row in rows:
        values = [
            f"{row['id']}: {row['title']}",
            f"{row['scope']} (n={row['experimental_unit_count']})",
            "; ".join(row["hypotheses"]),
            "; ".join(row["analyses"]),
            "; ".join(row["sources"]),
            "; ".join(row["results"]),
            "; ".join(row["papers"]),
        ]
        lines.append("| " + " | ".join(_escape(value) for value in values) + " |")
    lines.extend(["", "### Evidence statements", ""])
    lines.extend(f"- **{_escape(row['id'])}:** {_escape(row['evidence'])}" for row in rows)
    if not validation["valid"]:
        lines.extend(["", "> **Warning:** The evidence gate failed; inspect `validation.json`."])
    lines.extend(["", APPENDIX_END, ""])
    return "\n".join(lines)


def attach_appendix(report: str, appendix: str) -> str:
    pattern = re.compile(
        re.escape(APPENDIX_BEGIN) + r".*?" + re.escape(APPENDIX_END) + r"\s*", re.DOTALL
    )
    return pattern.sub("", report).rstrip() + "\n\n" + appendix


def _resolve_export_path(path: Path, workspace_root: Path | None, context: str) -> Path:
    """Resolve an explicit path, confining agent-operated calls to a workspace."""
    if workspace_root is None:
        return path.resolve()
    return _resolve_within(workspace_root, path, context)


def _resolve_vocabulary_path(
    path: Path,
    workspace_root: Path | None,
    trusted_default: Path,
    context: str,
) -> Path:
    """Allow package resources while confining caller-selected vocabularies."""
    resolved = path.resolve()
    if resolved == trusted_default.resolve():
        return resolved
    return _resolve_export_path(path, workspace_root, context)


def export_hcmo_evidence(
    snapshot_path: Path,
    output_dir: Path,
    *,
    report_path: Path | None = None,
    source_root: Path | None = None,
    profile_path: Path = DEFAULT_PROFILE,
    shapes_path: Path = DEFAULT_SHAPES,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    """Export and validate an evidence bundle from one normalized job snapshot.

    Agent-operated callers must provide ``workspace_root``. The CLI does so by
    default. ``None`` preserves unrestricted paths for trusted, direct Python
    callers while source files declared by a snapshot remain confined to their
    explicit ``source_root`` in every mode.
    """
    snapshot_path = _resolve_export_path(snapshot_path, workspace_root, "snapshot")
    output_dir = _resolve_export_path(output_dir, workspace_root, "output directory")
    if report_path is not None:
        report_path = _resolve_export_path(report_path, workspace_root, "report")
    profile_path = _resolve_vocabulary_path(
        profile_path,
        workspace_root,
        DEFAULT_PROFILE,
        "vocabulary profile",
    )
    shapes_path = _resolve_vocabulary_path(
        shapes_path,
        workspace_root,
        DEFAULT_SHAPES,
        "SHACL shapes",
    )
    snapshot = _load_snapshot(snapshot_path)
    validate_snapshot(snapshot)
    resolved_source_root = _resolve_export_path(
        source_root or snapshot_path.parent,
        workspace_root,
        "source root",
    )
    verified = verify_source_files(snapshot, resolved_source_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    semantic_manifest_path = output_dir / "semantic-manifest.json"
    semantic_manifest_text = _canonical_json(snapshot["semantic_manifest"])
    semantic_manifest_path.write_text(semantic_manifest_text, encoding="utf-8", newline="\n")
    evidence_text = (
        EvidenceGraphBuilder(snapshot).build().serialize(format="turtle").rstrip() + "\n"
    )
    profile_text = profile_path.read_text(encoding="utf-8")
    shapes_text = shapes_path.read_text(encoding="utf-8")
    validation = validate_graph(evidence_text, profile_text, shapes_text)
    validation["artifacts"]["semantic_manifest"] = semantic_manifest_path.name
    validation["artifacts"]["semantic_manifest_sha256"] = _sha256(semantic_manifest_text)
    validation["source_files"] = verified
    (output_dir / "evidence.ttl").write_text(evidence_text, encoding="utf-8", newline="\n")
    (output_dir / "validation.json").write_text(
        json.dumps(validation, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    appendix = make_traceability_appendix(evidence_text, validation)
    (output_dir / "traceability-appendix.md").write_text(appendix, encoding="utf-8", newline="\n")
    if report_path:
        report = report_path.read_text(encoding="utf-8")
        (output_dir / "final_report_with_traceability.md").write_text(
            attach_appendix(report, appendix), encoding="utf-8", newline="\n"
        )
    return validation


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=Path.cwd(),
        help="allowed root for all caller-selected input and output paths (default: current directory)",
    )
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--shapes", type=Path, default=DEFAULT_SHAPES)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        validation = export_hcmo_evidence(
            args.snapshot,
            args.output_dir,
            report_path=args.report,
            source_root=args.source_root,
            profile_path=args.profile,
            shapes_path=args.shapes,
            workspace_root=args.workspace_root,
        )
    except (EvidenceExportError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"export failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"valid": validation["valid"], "output_dir": str(args.output_dir)}))
    return 0 if validation["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
