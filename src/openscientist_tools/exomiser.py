"""Standalone Exomiser variant-prioritization tool (`run_exomiser`).

Registers only when EXOMISER_PATH points at an install dir containing the CLI
jar, mirroring the in-process gate (and `phenix.py`). The function body is
defined unconditionally so tests can import and call it regardless of env state.

Grounded in the Exomiser v15 CLI: ``analyse --sample <phenopacket>`` with
``--preset`` (EXOME|GENOME|PHENOTYPE_ONLY), optional ``--vcf/--assembly/--ped``,
an optional ``--analysis <yaml>`` escape hatch, and ``--output-directory``. v15
writes ``.parquet`` (+ .jsonl, .html) by default, so we point output at a
run-specific dir and read the parquet there.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from openscientist.exomiser_setup import find_exomiser_jar, setup_exomiser_env
from openscientist.knowledge_state import KnowledgeState
from openscientist.settings import get_settings
from openscientist_tools.server import mcp
from openscientist_tools.state import STATE

logger = logging.getLogger(__name__)

# Exome runs take minutes; genome/WGS longer. Allow generous headroom.
_EXOMISER_TIMEOUT_S = 1800

_PRESETS = {
    "exome": "EXOME",
    "genome": "GENOME",
    "phenotype-only": "PHENOTYPE_ONLY",
    "phenotype_only": "PHENOTYPE_ONLY",
}
_ASSEMBLIES = {"hg19", "hg38", "GRCh37", "GRCh38"}


def _resolve_data_file(filename: str) -> Path | None:
    """Resolve a filename strictly under the job's data directory.

    Rejects absolute paths and ``..`` traversal so inputs cannot escape
    ``job_dir/data``. Returns the resolved path if it exists, else None.
    """
    name = Path(filename)
    if name.is_absolute() or ".." in name.parts:
        return None
    data_dir = (STATE.job_dir / "data").resolve()
    candidate = (data_dir / name).resolve()
    try:
        candidate.relative_to(data_dir)
    except ValueError:
        return None
    return candidate if candidate.exists() else None


def _log_exomiser_execution(input_files: list[str], description: str, success: bool) -> None:
    """Record the Exomiser run in knowledge state (all inputs, like Phenix)."""
    ks = KnowledgeState.load_from_database_sync(STATE.job_id)
    ks.log_analysis(
        action="run_exomiser",
        tool_name="exomiser",
        input_files=input_files,
        description=description,
        success=success,
    )
    ks.save_to_database_sync(STATE.job_id)


def _err(message: str) -> str:
    """Structured error result (same shape the agent parses for success)."""
    return json.dumps({"ok": False, "error": message}, indent=2)


def _extract_hpo_ids(sample_path: Path) -> list[str]:
    """Extract HPO term IDs from a GA4GH phenopacket (v1/v2, YAML or JSON).

    Reads ``phenotypicFeatures[].type.id``, skipping any ``excluded`` features.
    YAML's loader is a JSON superset, so this handles both ``.yml`` and ``.json``.
    """
    try:
        with sample_path.open() as fh:
            pp = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(pp, dict):
        return []
    ids: list[str] = []
    for pf in pp.get("phenotypicFeatures") or []:
        if not isinstance(pf, dict) or pf.get("excluded"):
            continue
        term_id = (pf.get("type") or {}).get("id")
        if isinstance(term_id, str) and term_id.startswith("HP:"):
            ids.append(term_id)
    return ids


def _write_phenotype_only_analysis(run_dir: Path, assembly: str, hpo_ids: list[str]) -> Path:
    """Write a self-contained Exomiser analysis YAML pinned to ``assembly``.

    The bare ``--preset PHENOTYPE_ONLY`` path defaults the genome assembly to hg19
    (the CLI ``--assembly`` flag only applies alongside a VCF), which fails on an
    hg38-only data install. A self-contained analysis lets us force the assembly and
    embed the patient's HPO terms; with no VCF this runs phenotype-only.
    """
    analysis = {
        "analysis": {
            "genomeAssembly": assembly,
            "hpoIds": hpo_ids,
            "inheritanceModes": {},
            "analysisMode": "PASS_ONLY",
            "frequencySources": [],
            "pathogenicitySources": [],
            "steps": [{"omimPrioritiser": {}}, {"hiPhivePrioritiser": {}}],
        },
        "outputOptions": {
            "outputContributingVariantsOnly": False,
            "numGenes": 0,
            "outputFormats": ["PARQUET", "TSV_GENE", "JSON"],
        },
    }
    analysis_path = run_dir / "phenotype_only_analysis.yml"
    with analysis_path.open("w") as fh:
        yaml.safe_dump(analysis, fh, sort_keys=False)
    return analysis_path


def _baseline_run_metadata(
    sample: str, assembly: str, jar: str, exomiser_path: str
) -> dict[str, Any]:
    """Best-effort run metadata for the deterministic ranking file."""
    # Exomiser version from the jar filename: exomiser-cli-15.0.0.jar -> 15.0.0
    version = Path(jar).stem.replace("exomiser-cli-", "") or None
    data_version: str | None = None
    manifest = Path(exomiser_path) / "openscientist-exomiser-manifest.txt"
    try:
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if line.startswith("exomiser_data_version="):
                data_version = line.split("=", 1)[1].strip() or None
                break
    except OSError:
        pass
    return {
        "sampleId": Path(sample).stem,
        "assembly": assembly,
        "exomiserVersion": version,
        "dataVersion": data_version,
    }


def _gene_ranking_entry(g: dict[str, Any]) -> dict[str, Any]:
    """Build one gene-ranking row from an Exomiser JSONL gene record."""
    gene_scores = g.get("geneScores") or []
    gs = gene_scores[0] if gene_scores else {}
    ident = g.get("geneIdentifier") or {}
    acmg: str | None = None
    for a in gs.get("acmgAssignments") or []:
        acmg = a.get("acmgClassification")
        if acmg:
            break
    variants: list[str] = []
    for v in gs.get("contributingVariants") or []:
        contig, start, ref, alt = v.get("contigName"), v.get("start"), v.get("ref"), v.get("alt")
        if contig and start and ref and alt:
            variants.append(f"{contig}-{start}-{ref}-{alt}")
    matches = ((g.get("priorityResults") or {}).get("HIPHIVE_PRIORITY") or {}).get(
        "diseaseMatches"
    ) or []
    top_disease: str | None = None
    if matches:
        d = (matches[0].get("model") or {}).get("disease") or {}
        if d.get("diseaseId"):
            top_disease = f"{d['diseaseId']} {d.get('diseaseName') or ''}".strip()
    return {
        "geneSymbol": g.get("geneSymbol"),
        "geneId": ident.get("ensemblId") or ident.get("geneId"),
        "entrezId": ident.get("entrezId"),
        "moi": gs.get("modeOfInheritance"),
        "geneCombinedScore": g.get("combinedScore"),
        "genePhenotypeScore": g.get("priorityScore"),
        "geneVariantScore": g.get("variantScore"),
        "acmgClassification": acmg,
        "variants": variants,
        "topDiseaseMatch": top_disease,
    }


def _write_baseline_ranking(
    run_dir: Path,
    jsonl_files: list[str],
    *,
    sample: str,
    assembly: str,
    jar: str,
    exomiser_path: str,
    max_genes: int = 100,
    max_diseases: int = 50,
) -> Path | None:
    """Emit the deterministic baseline ranking (genes + phenotype-driven diseases).

    Parses Exomiser's gene-level JSONL so the immutable baseline never depends on the
    agent (it's a pure data transform). Diseases are the hiPhive phenotype matches
    aggregated across genes, keeping the best score per disease. Gene/disease lists are
    capped (``totalGenes``/``totalDiseases`` record the full counts). Returns the
    written path, or None if there is no JSONL to parse.
    """
    if not jsonl_files:
        return None
    genes: list[dict[str, Any]] = []
    diseases: dict[str, dict[str, Any]] = {}
    for jf in jsonl_files:
        try:
            with open(jf, encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line:
                        continue
                    g = json.loads(line)
                    genes.append(g)
                    hiphive = (g.get("priorityResults") or {}).get("HIPHIVE_PRIORITY") or {}
                    for dm in hiphive.get("diseaseMatches") or []:
                        disease = (dm.get("model") or {}).get("disease") or {}
                        disease_id, score = disease.get("diseaseId"), dm.get("score")
                        if not disease_id or score is None:
                            continue
                        cur = diseases.get(disease_id)
                        if cur is None or score > cur["phenotypeScore"]:
                            diseases[disease_id] = {
                                "diseaseId": disease_id,
                                "diseaseName": disease.get("diseaseName"),
                                "phenotypeScore": score,
                                "topGene": g.get("geneSymbol"),
                            }
        except (OSError, json.JSONDecodeError):
            continue

    genes.sort(
        key=lambda g: (
            -(g.get("combinedScore") or 0.0),
            g.get("geneSymbol") or "",
            ((g.get("geneScores") or [{}])[0] or {}).get("modeOfInheritance") or "",
        )
    )
    gene_ranking = [
        {"rank": i, **_gene_ranking_entry(g)} for i, g in enumerate(genes[:max_genes], start=1)
    ]
    disease_sorted = sorted(
        diseases.values(), key=lambda d: (-(d["phenotypeScore"] or 0.0), d["diseaseId"])
    )
    disease_ranking = [
        {"rank": i, **d} for i, d in enumerate(disease_sorted[:max_diseases], start=1)
    ]

    payload = {
        "schemaVersion": "1",
        "runMetadata": _baseline_run_metadata(sample, assembly, jar, exomiser_path),
        "totalGenes": len(genes),
        "totalDiseases": len(diseases),
        "geneRanking": gene_ranking,
        "diseaseRanking": disease_ranking,
    }
    path = run_dir / "exomiser_ranking.json"
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    return path


def _run_exomiser_impl(
    sample: str,
    vcf: str | None = None,
    assembly: str = "hg38",
    ped: str | None = None,
    preset: str = "exome",
    analysis_yaml: str | None = None,
    max_heap_gb: int = 4,
    description: str = "",
) -> str:
    """Run Exomiser ``analyse`` and return a structured JSON result."""
    env = setup_exomiser_env()
    if env is None:
        return _err("EXOMISER_PATH not configured.")
    exomiser_path = get_settings().exomiser.exomiser_path
    if not exomiser_path:
        return _err("EXOMISER_PATH not configured.")
    jar = find_exomiser_jar(exomiser_path)
    if not jar:
        return _err("Exomiser CLI jar not found under EXOMISER_PATH.")

    preset_key = preset.strip().lower()
    if preset_key not in _PRESETS:
        return _err(f"Invalid preset '{preset}'. Use one of: exome, genome, phenotype-only.")
    preset_value = _PRESETS[preset_key]
    # Phenotype-only (with no custom analysis YAML) is special-cased: it needs a
    # generated analysis YAML so the assembly can be pinned — see below.
    phenotype_only = preset_value == "PHENOTYPE_ONLY" and not analysis_yaml
    # assembly applies to a separately-supplied VCF and to phenotype-only (pinned in
    # the generated YAML); validate it whenever it will actually be used.
    if (vcf or phenotype_only) and assembly not in _ASSEMBLIES:
        return _err(f"Invalid assembly '{assembly}'. Use one of: {', '.join(sorted(_ASSEMBLIES))}.")
    if max_heap_gb < 1:
        return _err("max_heap_gb must be >= 1.")

    sample_path = _resolve_data_file(sample)
    if sample_path is None:
        return _err(f"Sample/phenopacket not found under data dir (or path not allowed): {sample}")

    # Run-specific output dir: avoids stale results, gives a known location to read
    # output from, and holds any generated analysis YAML.
    run_dir = STATE.job_dir / "data" / "exomiser_results" / uuid.uuid4().hex[:12]
    run_dir.mkdir(parents=True, exist_ok=True)

    input_files = [sample]
    install_dir = Path(exomiser_path)
    data_dir = str(install_dir / "data")
    cmd = [
        "java",
        f"-Xmx{max_heap_gb}g",
        # Exomiser reads application.properties from the CWD by default, but we run with
        # cwd=job_dir/data; point Spring at the install dir so it finds the configured
        # assembly/phenotype data-versions (else: "No GenomeAnalysisService instance").
        f"-Dspring.config.additional-location=file:{install_dir}/",
        # Per-assembly data paths are built from `exomiser.data-directory`, which the
        # shipped application.properties leaves unset — pin it or the
        # `${exomiser.data-directory}` placeholder stays literal and data isn't found.
        f"-Dexomiser.data-directory={data_dir}",
        "-jar",
        jar,
        "analyse",
    ]

    if phenotype_only:
        hpo_ids = _extract_hpo_ids(sample_path)
        if not hpo_ids:
            return _err(
                "phenotype-only needs HPO terms in the phenopacket "
                f"(phenotypicFeatures[].type.id); none found in {sample}."
            )
        generated_analysis = _write_phenotype_only_analysis(run_dir, assembly, hpo_ids)
        cmd += ["--analysis", str(generated_analysis)]
    else:
        cmd += ["--sample", str(sample_path)]
        if vcf:
            vcf_path = _resolve_data_file(vcf)
            if vcf_path is None:
                return _err(f"VCF not found under data dir (or path not allowed): {vcf}")
            # --assembly is needed when the VCF is supplied separately; when the VCF
            # comes from the phenopacket, the assembly comes with it.
            cmd += ["--vcf", str(vcf_path), "--assembly", assembly]
            input_files.append(vcf)
        if ped:
            ped_path = _resolve_data_file(ped)
            if ped_path is None:
                return _err(f"PED not found under data dir (or path not allowed): {ped}")
            cmd += ["--ped", str(ped_path)]
            input_files.append(ped)
        # A custom analysis YAML replaces the built-in preset (full config escape hatch);
        # otherwise use the recommended preset (tuned on the 100k Genomes cohort).
        if analysis_yaml:
            analysis_path = _resolve_data_file(analysis_yaml)
            if analysis_path is None:
                return _err(
                    f"analysis_yaml not found under data dir (or path not allowed): {analysis_yaml}"
                )
            cmd += ["--analysis", str(analysis_path)]
            input_files.append(analysis_yaml)
        else:
            cmd += ["--preset", preset_value]

    cmd += ["--output-directory", str(run_dir)]

    try:
        result = subprocess.run(
            cmd,
            env=env,
            cwd=str(STATE.job_dir / "data"),
            capture_output=True,
            text=True,
            timeout=_EXOMISER_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _log_exomiser_execution(input_files, description, success=False)
        return _err(f"Exomiser timed out after {_EXOMISER_TIMEOUT_S}s")
    except FileNotFoundError:
        return _err("'java' not found — the agent image needs a Java 21+ runtime.")
    except (OSError, subprocess.SubprocessError) as e:
        return _err(f"Error running Exomiser: {e}")

    _log_exomiser_execution(input_files, description, success=(result.returncode == 0))

    parquet = sorted(str(p) for p in run_dir.glob("*.parquet"))
    jsonl = sorted(str(p) for p in run_dir.glob("*.jsonl"))
    # Emit the deterministic baseline ranking ourselves so it's guaranteed, not left to
    # the agent. Genes (by geneCombinedScore) and phenotype-driven diseases.
    ranking_path = (
        _write_baseline_ranking(
            run_dir, jsonl, sample=sample, assembly=assembly, jar=jar, exomiser_path=exomiser_path
        )
        if result.returncode == 0
        else None
    )
    return json.dumps(
        {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "command": cmd,
            "output_directory": str(run_dir),
            "parquet": parquet,
            "jsonl": jsonl,
            "exomiser_ranking": str(ranking_path) if ranking_path else None,
            "stdout_tail": result.stdout[-2000:],
            "stderr_tail": result.stderr[-1000:],
            "note": (
                "`exomiser_ranking` is the deterministic baseline (geneRanking + "
                "diseaseRanking) emitted for you — treat it as the immutable Exomiser ranking; "
                "do not re-order it. For an evidence-cited re-ranking, write reranked.json "
                "citing sources. parquet/jsonl hold the full per-variant detail."
            ),
        },
        indent=2,
    )


def run_exomiser(
    sample: str,
    vcf: str | None = None,
    assembly: str = "hg38",
    ped: str | None = None,
    preset: str = "exome",
    analysis_yaml: str | None = None,
    max_heap_gb: int = 4,
    description: str = "",
) -> str:
    """
    Run Exomiser phenotype-driven variant prioritization. Returns a JSON object
    with the output paths (parse the parquet file).

    Args:
        sample: Phenopacket file (relative to the job data directory) with the
            patient's HPO terms; may also reference the VCF + its assembly.
        vcf: Optional VCF file (relative to data dir) if not in the phenopacket.
            When set, `assembly` is passed to Exomiser; otherwise assembly comes
            from the phenopacket.
        assembly: Genome assembly for a separately-supplied VCF: hg19/hg38
            (GRCh37/GRCh38 also accepted).
        ped: Optional PED file (relative to data dir) for family/trio analysis.
        preset: "exome" (default), "genome" (needs REMM data), or
            "phenotype-only". Ignored if analysis_yaml is given.
        analysis_yaml: Optional Exomiser analysis YAML (relative to data dir) for
            full control (frequency/pathogenicity sources, inheritance modes,
            filters, etc.). Replaces the preset. Use the recommended presets
            unless you have a specific reason to customize.
        max_heap_gb: JVM heap in GB (default 4; use 12+ for genome/WGS).
        description: What you're investigating.

    Returns:
        JSON: {ok, returncode, command, output_directory, parquet, jsonl,
        stdout_tail, stderr_tail}. Read the `parquet` path for results.
    """
    return _run_exomiser_impl(
        sample=sample,
        vcf=vcf,
        assembly=assembly,
        ped=ped,
        preset=preset,
        analysis_yaml=analysis_yaml,
        max_heap_gb=max_heap_gb,
        description=description,
    )


def _exomiser_installed() -> bool:
    """Direct EXOMISER_PATH env-var probe.

    Avoids `openscientist.exomiser_setup.check_exomiser_available`, which routes
    through `get_settings()` and crashes at module import when `DATABASE_URL` is
    not set yet (mirrors `phenix._phenix_installed`).
    """
    exomiser_path = os.environ.get("EXOMISER_PATH")
    if not exomiser_path or not os.path.isdir(exomiser_path):
        return False
    return bool(glob.glob(os.path.join(exomiser_path, "exomiser-cli-*.jar")))


if _exomiser_installed():
    mcp.tool()(run_exomiser)
