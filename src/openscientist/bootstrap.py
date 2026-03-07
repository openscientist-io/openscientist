"""
Bootstrap helpers for migrating legacy filesystem jobs into the database.

This migration is designed to be idempotent:
- missing jobs are created
- existing jobs are reused
- data files/plot metadata are only inserted when absent
- knowledge-state sync reuses KnowledgeState.save_to_database de-dup logic
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import chain
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.database.models import Job, JobDataFile, Plot, User
from openscientist.database.session import get_admin_session
from openscientist.file_loader import get_file_info
from openscientist.knowledge_state import KS_FILENAME, KnowledgeState

logger = logging.getLogger(__name__)

_VALID_JOB_STATUSES = {
    "pending",
    "queued",
    "running",
    "awaiting_feedback",
    "generating_report",
    "completed",
    "failed",
    "cancelled",
}
_LEGACY_STATUS_MAP = {
    "created": "pending",
    "canceled": "cancelled",
    "cancelled": "cancelled",
    "error": "failed",
}


@dataclass
class BootstrapResult:
    """Summary for a bootstrap run."""

    scanned_directories: int = 0
    skipped_invalid_job_id: int = 0
    skipped_empty_directory: int = 0
    created_jobs: int = 0
    existing_jobs: int = 0
    orphan_jobs: int = 0
    synced_knowledge_state: int = 0
    data_files_added: int = 0
    plots_added: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-serializable dictionary."""
        return asdict(self)


@dataclass
class MigrationTarget:
    """Resolved migration target for a job directory."""

    job_id: UUID
    legacy_ids: set[str]
    rename_directory: bool = False


def _to_string(value: Any) -> str:
    """
    Coerce any value into a trimmed string.

    Args:
        value: Value to normalize.

    Returns:
        Trimmed string representation, or empty string for ``None``.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _to_optional_string(value: Any) -> str | None:
    """
    Coerce a value to string and normalize empty values to ``None``.

    Args:
        value: Value to normalize.

    Returns:
        Normalized string or ``None`` when empty.
    """
    text = _to_string(value)
    return text if text else None


def _to_list(value: Any) -> list[Any]:
    """
    Normalize dynamic payload values to lists.

    Args:
        value: Value expected to be a list.

    Returns:
        Original list value, or an empty list for non-list input.
    """
    return value if isinstance(value, list) else []


def _coerce_int(value: Any, default: int, minimum: int = 0) -> int:
    """
    Parse an integer with fallback and lower-bound enforcement.

    Args:
        value: Candidate integer-like value.
        default: Fallback value when parsing fails or bound is violated.
        minimum: Inclusive lower bound.

    Returns:
        Parsed integer meeting the bound, or ``default``.
    """
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def _parse_uuid(value: Any) -> UUID | None:
    """
    Parse a UUID-like value.

    Args:
        value: Candidate UUID value.

    Returns:
        Parsed UUID, or ``None`` if input is empty/invalid.
    """
    if value is None:
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    """
    Parse timestamps from legacy payloads.

    Supports datetime objects and ISO strings (including trailing ``Z``).
    Naive timestamps are interpreted as UTC.

    Args:
        value: Candidate datetime payload.

    Returns:
        Timezone-aware datetime in UTC, or ``None`` if invalid/missing.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            return None
    else:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _normalize_status(raw: Any) -> str:
    """
    Normalize legacy job statuses to the current enum values.

    Args:
        raw: Raw status value from legacy config.

    Returns:
        Valid normalized status; defaults to ``pending`` for unknown values.
    """
    status = _to_string(raw).lower()
    if not status:
        return "pending"
    status = _LEGACY_STATUS_MAP.get(status, status)
    return status if status in _VALID_JOB_STATUSES else "pending"


def _load_json(path: Path, result: BootstrapResult, context: str) -> dict[str, Any] | None:
    """
    Safely load a JSON object from disk and record parse errors.

    Args:
        path: JSON file path.
        result: Aggregate bootstrap result used for error collection.
        context: Human-readable context prefix for logs/errors.

    Returns:
        Parsed JSON object, ``None`` when file is missing or invalid.
    """
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        msg = f"{context}: failed to read {path}: {e}"
        result.errors.append(msg)
        logger.warning(msg)
        return None
    if not isinstance(payload, dict):
        msg = f"{context}: expected JSON object in {path}"
        result.errors.append(msg)
        logger.warning(msg)
        return None
    return payload


def _extract_id_candidates(
    job_dir: Path,
    config: dict[str, Any] | None,
    ks_data: dict[str, Any] | None,
) -> dict[str, str | None]:
    """
    Extract potential ID values from directory/config/knowledge-state.

    Args:
        job_dir: Job directory being migrated.
        config: Optional parsed ``config.json`` payload.
        ks_data: Optional parsed ``knowledge_state.json`` payload.

    Returns:
        Candidate ID values by source.
    """
    ks_config = _extract_ks_config(ks_data)
    return {
        "dir_name": _to_optional_string(job_dir.name),
        "config_job_id": _to_optional_string((config or {}).get("job_id")),
        "ks_config_job_id": _to_optional_string(ks_config.get("job_id")),
        "ks_job_id": _to_optional_string((ks_data or {}).get("job_id")),
    }


def _extract_legacy_ids(candidates: dict[str, str | None]) -> set[str]:
    """
    Extract non-UUID legacy identifiers from candidate values.

    Args:
        candidates: Candidate ID values by source.

    Returns:
        Set of non-empty non-UUID identifier strings.
    """
    legacy_ids: set[str] = set()
    for value in candidates.values():
        if value is None:
            continue
        if _parse_uuid(value) is None:
            legacy_ids.add(value)
    return legacy_ids


def _resolve_uuid_candidate(candidates: dict[str, str | None]) -> UUID | None:
    """
    Resolve UUID candidate from known ID sources in priority order.

    Priority: directory name, config.job_id, ks.config.job_id, ks.job_id.

    Args:
        candidates: Candidate ID values by source.

    Returns:
        Parsed UUID, or ``None`` when none are valid UUIDs.
    """
    for key in ("dir_name", "config_job_id", "ks_config_job_id", "ks_job_id"):
        value = candidates.get(key)
        parsed = _parse_uuid(value)
        if parsed is not None:
            return parsed
    return None


def _derive_uuidv7_seed_time_from_filesystem(job_dir: Path) -> datetime | None:
    """
    Derive a best-effort job creation time from filesystem metadata.

    Preference order:
    1. Earliest available ``st_birthtime`` across the job tree.
    2. Earliest ``min(st_mtime, st_ctime)`` across the job tree.

    Args:
        job_dir: Job directory being migrated.

    Returns:
        Timezone-aware UTC timestamp or ``None`` if no stat metadata is readable.
    """
    birth_times: list[float] = []
    fallback_times: list[float] = []

    for path in chain((job_dir,), job_dir.rglob("*")):
        try:
            stat = path.stat()
        except OSError:
            continue

        birth_time = getattr(stat, "st_birthtime", None)
        if isinstance(birth_time, (int, float)):
            birth_times.append(float(birth_time))
        fallback_times.append(min(stat.st_mtime, stat.st_ctime))

    timestamp = min(birth_times) if birth_times else min(fallback_times, default=None)
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=UTC)


def _offset_colliding_seed_time(
    seed_time: datetime, seed_time_counts_by_ms: dict[int, int]
) -> datetime:
    """
    Ensure deterministic ordering when multiple jobs resolve to the same ms seed.

    Args:
        seed_time: Candidate seed timestamp.
        seed_time_counts_by_ms: Per-millisecond collision counters for this run.

    Returns:
        Original timestamp or timestamp shifted by a deterministic millisecond offset.
    """
    normalized = seed_time.astimezone(UTC) if seed_time.tzinfo else seed_time.replace(tzinfo=UTC)
    seed_ms = int(normalized.timestamp() * 1000)
    offset_ms = seed_time_counts_by_ms.get(seed_ms, 0)
    seed_time_counts_by_ms[seed_ms] = offset_ms + 1
    if offset_ms == 0:
        return normalized
    return normalized + timedelta(milliseconds=offset_ms)


async def _generate_uuidv7(session: AsyncSession, seed_time: datetime | None = None) -> UUID:
    """
    Generate a UUIDv7 value from PostgreSQL.

    Args:
        session: Active DB session.
        seed_time: Optional UTC timestamp to seed UUIDv7 time component.

    Returns:
        Generated UUIDv7 value.

    Raises:
        ValueError: If database result cannot be parsed as UUID.
    """
    if seed_time is None:
        generated = (await session.execute(text("SELECT uuidv7()"))).scalar_one()
    else:
        normalized = (
            seed_time.astimezone(UTC) if seed_time.tzinfo else seed_time.replace(tzinfo=UTC)
        )
        generated = (
            await session.execute(
                text("SELECT uuidv7(CAST(:seed_time AS timestamptz) - statement_timestamp())"),
                {"seed_time": normalized},
            )
        ).scalar_one()

    parsed = _parse_uuid(generated)
    if parsed is None:
        raise ValueError(f"uuidv7() returned non-UUID value: {generated!r}")
    return parsed


def _rewrite_legacy_path_prefixes(value: str, legacy_ids: set[str], new_job_id: str) -> str:
    """
    Rewrite embedded ``jobs/<legacy_id>/...`` path prefixes to new UUID.

    Args:
        value: Candidate string value.
        legacy_ids: Legacy identifiers to replace.
        new_job_id: Canonical UUID string.

    Returns:
        Rewritten string value.
    """
    updated = value
    for legacy_id in legacy_ids:
        updated = updated.replace(f"jobs/{legacy_id}/", f"jobs/{new_job_id}/")
        updated = updated.replace(f"/jobs/{legacy_id}/", f"/jobs/{new_job_id}/")
    return updated


def _rewrite_payload_paths(value: Any, legacy_ids: set[str], new_job_id: str) -> tuple[Any, bool]:
    """
    Recursively rewrite legacy ``jobs/<id>/...`` path prefixes in payloads.

    Args:
        value: Any JSON-serializable payload value.
        legacy_ids: Legacy identifiers to replace.
        new_job_id: Canonical UUID string.

    Returns:
        Tuple of ``(rewritten_value, changed)``.
    """
    if isinstance(value, dict):
        changed = False
        rewritten: dict[str, Any] = {}
        for key, item in value.items():
            updated_item, item_changed = _rewrite_payload_paths(item, legacy_ids, new_job_id)
            rewritten[key] = updated_item
            changed = changed or item_changed
        return rewritten, changed

    if isinstance(value, list):
        changed = False
        rewritten_list: list[Any] = []
        for item in value:
            updated_item, item_changed = _rewrite_payload_paths(item, legacy_ids, new_job_id)
            rewritten_list.append(updated_item)
            changed = changed or item_changed
        return rewritten_list, changed

    if isinstance(value, str):
        updated = _rewrite_legacy_path_prefixes(value, legacy_ids, new_job_id)
        return updated, updated != value

    return value, False


def _write_json(
    path: Path,
    payload: dict[str, Any],
    result: BootstrapResult,
    context: str,
) -> bool:
    """
    Persist JSON payload to disk and record errors.

    Args:
        path: JSON file path.
        payload: Parsed JSON object to write.
        result: Aggregate bootstrap result for error collection.
        context: Context string for logs/errors.

    Returns:
        ``True`` on success, ``False`` on write error.
    """
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        return True
    except OSError as e:
        msg = f"{context}: failed to write {path}: {e}"
        result.errors.append(msg)
        logger.error(msg, exc_info=True)
        return False


def _rename_job_dir(
    *,
    job_dir: Path,
    target_job_id: UUID,
    result: BootstrapResult,
    context: str,
) -> Path | None:
    """
    Rename a legacy job directory to canonical UUID directory name.

    Args:
        job_dir: Current job directory.
        target_job_id: Canonical UUID target.
        result: Aggregate bootstrap result for error collection.
        context: Context string for logs/errors.

    Returns:
        New job directory path or ``None`` on conflict/error.
    """
    target_dir = job_dir.with_name(str(target_job_id))
    if target_dir == job_dir:
        return job_dir
    if target_dir.exists():
        msg = f"{context}: cannot rename {job_dir} -> {target_dir} (target exists)"
        result.errors.append(msg)
        logger.warning(msg)
        return None
    try:
        job_dir.rename(target_dir)
    except OSError as e:
        msg = f"{context}: failed to rename {job_dir} -> {target_dir}: {e}"
        result.errors.append(msg)
        logger.error(msg, exc_info=True)
        return None
    return target_dir


def _derive_updated_at(config: dict[str, Any], created_at: datetime | None) -> datetime | None:
    """
    Derive best-effort ``updated_at`` timestamp from legacy fields.

    Args:
        config: Legacy job config payload.
        created_at: Parsed ``created_at`` fallback.

    Returns:
        Most relevant activity timestamp, or ``created_at`` when no better
        candidate exists.
    """
    for key in ("completed_at", "failed_at", "cancelled_at", "started_at"):
        parsed = _parse_datetime(config.get(key))
        if parsed is not None:
            return parsed
    return created_at


def _normalize_relative_path(job_id: UUID, raw_path: str) -> str:
    """
    Normalize legacy path values into job-relative POSIX paths.

    Args:
        job_id: Job UUID used to trim absolute legacy prefixes.
        raw_path: Raw path stored in historical metadata.

    Returns:
        Normalized relative path.
    """
    cleaned = raw_path.strip().replace("\\", "/")
    if not cleaned:
        return cleaned
    prefix = f"jobs/{job_id}/"
    if cleaned.startswith(prefix):
        return cleaned[len(prefix) :]
    # Legacy payloads may still contain `jobs/<legacy-id>/...` paths.
    if cleaned.startswith("/"):
        cleaned = cleaned[1:]
    if cleaned.startswith("jobs/"):
        parts = cleaned.split("/", 2)
        if len(parts) == 3:
            return parts[2]
        if len(parts) == 2:
            return parts[1]
    return cleaned.lstrip("/")


def _resolve_plot_path(job_dir: Path, filename: str | None, raw_path: str | None) -> str | None:
    """
    Resolve canonical relative plot path from legacy metadata.

    Args:
        job_dir: Job directory being migrated.
        filename: Legacy filename field.
        raw_path: Legacy path field, if already provided.

    Returns:
        Best-effort relative plot path, or ``None`` when unavailable.
    """
    if raw_path:
        return raw_path
    if not filename:
        return None

    provenance = job_dir / "provenance" / filename
    old_plots = job_dir / "plots" / filename
    if provenance.exists():
        return f"provenance/{filename}"
    if old_plots.exists():
        return f"plots/{filename}"
    return f"provenance/{filename}"


def _plot_candidate_from_plot_entry(
    job_id: UUID,
    job_dir: Path,
    raw_plot: dict[str, Any],
) -> dict[str, Any] | None:
    """
    Build normalized plot metadata from a ``plots`` array entry.

    Args:
        job_id: Job UUID.
        job_dir: Job directory being migrated.
        raw_plot: Raw plot entry from knowledge state.

    Returns:
        Normalized plot metadata dict, or ``None`` when the entry is unusable.
    """
    filename = _to_optional_string(raw_plot.get("filename"))
    raw_path = _to_optional_string(raw_plot.get("file_path"))
    resolved_path = _resolve_plot_path(job_dir, filename, raw_path)
    if not resolved_path:
        return None

    relative_path = _normalize_relative_path(job_id, resolved_path)
    if not relative_path:
        return None

    return {
        "iteration": _coerce_int(raw_plot.get("iteration"), default=1, minimum=1),
        "title": _to_string(raw_plot.get("title") or raw_plot.get("description") or filename)
        or Path(relative_path).name,
        "description": _to_optional_string(raw_plot.get("description")),
        "plot_type": _to_optional_string(raw_plot.get("plot_type")),
        "file_path": relative_path,
    }


def _plot_candidate_from_finding_ref(
    job_id: UUID,
    iteration: int,
    ref_path: str,
) -> dict[str, Any] | None:
    """
    Build normalized plot metadata from a finding-level plot reference.

    Args:
        job_id: Job UUID.
        iteration: Iteration associated with the finding.
        ref_path: Referenced plot path.

    Returns:
        Normalized plot metadata dict, or ``None`` when path is unusable.
    """
    relative_path = _normalize_relative_path(job_id, ref_path)
    if not relative_path:
        return None
    return {
        "iteration": iteration,
        "title": Path(relative_path).name,
        "description": "Migrated from finding plot reference",
        "plot_type": None,
        "file_path": relative_path,
    }


def _extract_plot_candidates_from_plots(
    job_id: UUID, job_dir: Path, ks_data: dict[str, Any]
) -> list[dict[str, Any]]:
    """
    Extract normalized plot candidates from explicit ``plots`` entries.

    Args:
        job_id: Job UUID.
        job_dir: Job directory being migrated.
        ks_data: Parsed knowledge-state payload.

    Returns:
        List of normalized plot metadata dictionaries.
    """
    candidates: list[dict[str, Any]] = []
    for raw_plot in _to_list(ks_data.get("plots")):
        if not isinstance(raw_plot, dict):
            continue
        candidate = _plot_candidate_from_plot_entry(job_id, job_dir, raw_plot)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _extract_plot_candidates_from_findings(
    job_id: UUID, ks_data: dict[str, Any]
) -> list[dict[str, Any]]:
    """
    Extract normalized plot candidates from finding ``plots`` references.

    Args:
        job_id: Job UUID.
        ks_data: Parsed knowledge-state payload.

    Returns:
        List of normalized plot metadata dictionaries.
    """
    candidates: list[dict[str, Any]] = []
    for raw_finding in _to_list(ks_data.get("findings")):
        if not isinstance(raw_finding, dict):
            continue
        iteration = _coerce_int(
            raw_finding.get("iteration_discovered") or raw_finding.get("iteration"),
            default=1,
            minimum=1,
        )
        for ref in _to_list(raw_finding.get("plots")):
            ref_path = _to_optional_string(ref)
            if not ref_path:
                continue
            candidate = _plot_candidate_from_finding_ref(job_id, iteration, ref_path)
            if candidate is not None:
                candidates.append(candidate)
    return candidates


def _extract_plot_candidates(
    job_id: UUID, job_dir: Path, ks_data: dict[str, Any]
) -> list[dict[str, Any]]:
    """
    Combine plot candidates discovered from all supported legacy locations.

    Args:
        job_id: Job UUID.
        job_dir: Job directory being migrated.
        ks_data: Parsed knowledge-state payload.

    Returns:
        Combined normalized plot metadata entries.
    """
    return [
        *_extract_plot_candidates_from_plots(job_id, job_dir, ks_data),
        *_extract_plot_candidates_from_findings(job_id, ks_data),
    ]


def _normalize_ks_config(
    raw_ks: dict[str, Any],
    job_id: str,
    research_question: str,
    max_iterations: int,
) -> dict[str, Any]:
    """
    Normalize the ``config`` section of knowledge-state payloads.

    Args:
        raw_ks: Raw knowledge-state JSON payload.
        job_id: Canonical job identifier.
        research_question: Fallback research question from job metadata.
        max_iterations: Fallback max-iterations value.

    Returns:
        Normalized config dictionary.
    """
    raw_config_obj = raw_ks.get("config")
    raw_config: dict[str, Any] = raw_config_obj if isinstance(raw_config_obj, dict) else {}
    return {
        "job_id": job_id,
        "research_question": _to_string(raw_config.get("research_question")) or research_question,
        "max_iterations": _coerce_int(
            raw_config.get("max_iterations"),
            default=max_iterations,
            minimum=1,
        ),
        "use_skills": bool(raw_config.get("use_skills", True)),
        "started_at": _to_optional_string(raw_config.get("started_at")),
    }


def _normalize_hypotheses(raw_ks: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Normalize legacy hypothesis records to the current schema shape.

    Args:
        raw_ks: Raw knowledge-state JSON payload.

    Returns:
        Normalized hypothesis list.
    """
    hypotheses: list[dict[str, Any]] = []
    for idx, raw_h in enumerate(_to_list(raw_ks.get("hypotheses")), start=1):
        if not isinstance(raw_h, dict):
            continue
        statement = _to_string(raw_h.get("statement") or raw_h.get("text"))
        if not statement:
            continue
        hypotheses.append(
            {
                "id": _to_string(raw_h.get("id")) or f"H{idx:03d}",
                "iteration_proposed": _coerce_int(
                    raw_h.get("iteration_proposed") or raw_h.get("iteration"),
                    default=1,
                    minimum=1,
                ),
                "statement": statement,
                "status": _to_string(raw_h.get("status")) or "pending",
                "proposed_by": _to_string(raw_h.get("proposed_by")) or "legacy_migration",
                "test_code": _to_optional_string(
                    raw_h.get("test_code") or raw_h.get("test_strategy")
                ),
                "result": raw_h.get("result"),
                "rationale": _to_optional_string(raw_h.get("rationale")),
            }
        )
    return hypotheses


def _normalize_findings(raw_ks: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Normalize legacy finding records to the current schema shape.

    Args:
        raw_ks: Raw knowledge-state JSON payload.

    Returns:
        Normalized findings list.
    """
    findings: list[dict[str, Any]] = []
    for idx, raw_f in enumerate(_to_list(raw_ks.get("findings")), start=1):
        if not isinstance(raw_f, dict):
            continue
        title = _to_string(raw_f.get("title") or raw_f.get("content"))
        if not title:
            continue
        findings.append(
            {
                "id": _to_string(raw_f.get("id")) or f"F{idx:03d}",
                "iteration_discovered": _coerce_int(
                    raw_f.get("iteration_discovered") or raw_f.get("iteration"),
                    default=1,
                    minimum=1,
                ),
                "title": title,
                "evidence": _to_string(raw_f.get("evidence")),
                "supporting_hypotheses": _to_list(raw_f.get("supporting_hypotheses")),
                "literature_support": _to_list(raw_f.get("literature_support")),
                "plots": _to_list(raw_f.get("plots")),
                "biological_interpretation": _to_string(raw_f.get("biological_interpretation")),
                "finding_type": _to_string(raw_f.get("finding_type") or raw_f.get("significance"))
                or "observation",
            }
        )
    return findings


def _normalize_literature(raw_ks: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Normalize legacy literature records to the current schema shape.

    Args:
        raw_ks: Raw knowledge-state JSON payload.

    Returns:
        Normalized literature list.
    """
    literature: list[dict[str, Any]] = []
    for idx, raw_l in enumerate(_to_list(raw_ks.get("literature")), start=1):
        if not isinstance(raw_l, dict):
            continue
        title = _to_string(raw_l.get("title"))
        if not title:
            continue
        literature.append(
            {
                "id": _to_string(raw_l.get("id")) or f"L{idx:03d}",
                "pmid": _to_optional_string(raw_l.get("pmid")),
                "title": title,
                "abstract": _to_optional_string(raw_l.get("abstract")),
                "relevance_to": _to_list(raw_l.get("relevance_to")),
                "retrieved_at_iteration": _coerce_int(
                    raw_l.get("retrieved_at_iteration") or raw_l.get("iteration"),
                    default=1,
                    minimum=1,
                ),
                "search_query": _to_optional_string(raw_l.get("search_query")),
                "authors": _to_optional_string(raw_l.get("authors")),
                "year": raw_l.get("year") if isinstance(raw_l.get("year"), int) else None,
                "doi": _to_optional_string(raw_l.get("doi")),
                "journal": _to_optional_string(raw_l.get("journal")),
                "relevance_score": raw_l.get("relevance_score"),
            }
        )
    return literature


def _normalize_analysis_log(raw_ks: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Normalize legacy analysis-log entries to the current schema shape.

    Args:
        raw_ks: Raw knowledge-state JSON payload.

    Returns:
        Normalized analysis-log list.
    """
    now_iso = datetime.now(UTC).isoformat()
    analysis_log: list[dict[str, Any]] = []
    for raw_log in _to_list(raw_ks.get("analysis_log")):
        if not isinstance(raw_log, dict):
            continue
        analysis_log.append(
            {
                "iteration": _coerce_int(raw_log.get("iteration"), default=1, minimum=1),
                "action": _to_string(raw_log.get("action")) or "legacy_action",
                "timestamp": _to_optional_string(raw_log.get("timestamp")) or now_iso,
                "code": _to_optional_string(raw_log.get("code")),
                "output": _to_optional_string(raw_log.get("output")),
                "details": raw_log.get("details"),
                "status": _to_optional_string(raw_log.get("status")),
            }
        )
    return analysis_log


def _normalize_iteration_summaries(raw_ks: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Normalize legacy iteration summary entries.

    Args:
        raw_ks: Raw knowledge-state JSON payload.

    Returns:
        Normalized iteration summary list.
    """
    summaries: list[dict[str, Any]] = []
    for raw_summary in _to_list(raw_ks.get("iteration_summaries")):
        if not isinstance(raw_summary, dict):
            continue
        summary = _to_string(raw_summary.get("summary"))
        if not summary:
            continue
        summaries.append(
            {
                "iteration": _coerce_int(raw_summary.get("iteration"), default=1, minimum=1),
                "summary": summary,
            }
        )
    return summaries


def _normalize_feedback_history(raw_ks: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Normalize legacy feedback history entries.

    Args:
        raw_ks: Raw knowledge-state JSON payload.

    Returns:
        Normalized feedback-history list.
    """
    feedback_history: list[dict[str, Any]] = []
    for raw_feedback in _to_list(raw_ks.get("feedback_history")):
        if not isinstance(raw_feedback, dict):
            continue
        feedback_text = _to_string(raw_feedback.get("text") or raw_feedback.get("feedback"))
        if not feedback_text:
            continue
        feedback_history.append(
            {
                "after_iteration": _coerce_int(
                    raw_feedback.get("after_iteration") or raw_feedback.get("iteration"),
                    default=1,
                    minimum=1,
                ),
                "text": feedback_text,
            }
        )
    return feedback_history


def _normalize_knowledge_state(
    raw_ks: dict[str, Any],
    job_id: str,
    research_question: str,
    max_iterations: int,
) -> dict[str, Any]:
    """
    Normalize both modern and legacy knowledge-state structures.

    Args:
        raw_ks: Raw knowledge-state payload.
        job_id: Canonical job identifier.
        research_question: Canonical research question fallback.
        max_iterations: Canonical max-iterations fallback.

    Returns:
        Fully normalized knowledge-state dictionary.
    """
    return {
        "config": _normalize_ks_config(raw_ks, job_id, research_question, max_iterations),
        "data_summary": raw_ks.get("data_summary")
        if isinstance(raw_ks.get("data_summary"), dict)
        else {},
        "iteration": _coerce_int(raw_ks.get("iteration"), default=1, minimum=1),
        "hypotheses": _normalize_hypotheses(raw_ks),
        "findings": _normalize_findings(raw_ks),
        "literature": _normalize_literature(raw_ks),
        "analysis_log": _normalize_analysis_log(raw_ks),
        "iteration_summaries": _normalize_iteration_summaries(raw_ks),
        "feedback_history": _normalize_feedback_history(raw_ks),
        "consensus_answer": _to_optional_string(raw_ks.get("consensus_answer")),
    }


async def _sync_data_files(session: AsyncSession, job: Job, job_dir: Path) -> int:
    """
    Insert missing ``job_data_files`` rows from filesystem contents.

    Args:
        session: Active DB session.
        job: Job model receiving data file metadata.
        job_dir: Filesystem job directory.

    Returns:
        Number of new ``JobDataFile`` rows added.
    """
    data_dir = job_dir / "data"
    if not data_dir.exists():
        return 0

    existing_paths = set(
        (
            await session.execute(select(JobDataFile.file_path).where(JobDataFile.job_id == job.id))
        ).scalars()
    )

    added = 0
    for file_path in sorted(data_dir.rglob("*")):
        if not file_path.is_file():
            continue
        relative_path = file_path.relative_to(job_dir).as_posix()
        if relative_path in existing_paths:
            continue

        try:
            info = get_file_info(file_path)
            file_size = _coerce_int(info.get("size"), default=file_path.stat().st_size, minimum=0)
            file_type = _to_string(info.get("file_type")) or "unknown"
            mime_type = _to_optional_string(info.get("mime_type"))
        except Exception:
            logger.warning("Fallback metadata for data file %s", file_path, exc_info=True)
            stat = file_path.stat()
            file_size = stat.st_size
            file_type = "unknown"
            mime_type = None

        session.add(
            JobDataFile(
                job_id=job.id,
                filename=file_path.name,
                file_path=relative_path,
                file_type=file_type,
                file_size=file_size,
                mime_type=mime_type,
            )
        )
        existing_paths.add(relative_path)
        added += 1

    return added


async def _sync_plots(
    session: AsyncSession, job: Job, job_dir: Path, ks_data: dict[str, Any]
) -> int:
    """
    Insert missing ``plots`` rows derived from knowledge-state metadata.

    Args:
        session: Active DB session.
        job: Job model receiving plot metadata.
        job_dir: Filesystem job directory.
        ks_data: Parsed knowledge-state payload.

    Returns:
        Number of new ``Plot`` rows added.
    """
    existing_paths = set(
        (await session.execute(select(Plot.file_path).where(Plot.job_id == job.id))).scalars()
    )
    added = 0
    for candidate in _extract_plot_candidates(job.id, job_dir, ks_data):
        file_path = _to_string(candidate.get("file_path"))
        if not file_path or file_path in existing_paths:
            continue
        session.add(
            Plot(
                job_id=job.id,
                iteration=_coerce_int(candidate.get("iteration"), default=1, minimum=1),
                title=_to_string(candidate.get("title")) or Path(file_path).name,
                description=_to_optional_string(candidate.get("description")),
                file_path=file_path,
                plot_type=_to_optional_string(candidate.get("plot_type")),
            )
        )
        existing_paths.add(file_path)
        added += 1
    return added


def _extract_ks_config(ks_data: dict[str, Any] | None) -> dict[str, Any]:
    """
    Extract normalized ``config`` dict from optional knowledge-state payload.

    Args:
        ks_data: Parsed knowledge-state payload or ``None``.

    Returns:
        Config dictionary or empty dict when unavailable.
    """
    if not isinstance(ks_data, dict):
        return {}
    raw_config = ks_data.get("config")
    return raw_config if isinstance(raw_config, dict) else {}


def _resolve_owner_id(config: dict[str, Any], users_by_id: dict[UUID, User]) -> UUID | None:
    """
    Resolve and validate owner ID from legacy config payload.

    Args:
        config: Legacy config payload.
        users_by_id: Existing users indexed by UUID.

    Returns:
        Owner UUID if present and valid; otherwise ``None``.
    """
    owner_id = _parse_uuid(config.get("owner_id"))
    return owner_id if owner_id in users_by_id else None


def _derive_job_title(job_id: UUID, config: dict[str, Any], ks_config: dict[str, Any]) -> str:
    """
    Build a fallback-safe job title from legacy payloads.

    Args:
        job_id: Job UUID.
        config: Legacy config payload.
        ks_config: Extracted config section from knowledge-state payload.

    Returns:
        Best available title string.
    """
    title = _to_string(config.get("research_question")) or _to_string(
        ks_config.get("research_question")
    )
    return title or f"Legacy job {job_id}"


def _build_new_job(
    job_id: UUID,
    config: dict[str, Any],
    ks_data: dict[str, Any] | None,
    users_by_id: dict[UUID, User],
) -> tuple[Job, bool]:
    """
    Construct a new ``Job`` model from legacy payloads.

    Args:
        job_id: Canonical job UUID.
        config: Legacy config payload.
        ks_data: Parsed knowledge-state payload.
        users_by_id: Existing users indexed by UUID.

    Returns:
        Tuple of ``(job_model, is_orphan)``.
    """
    ks_config = _extract_ks_config(ks_data)
    owner_id = _resolve_owner_id(config, users_by_id)
    title = _derive_job_title(job_id, config, ks_config)
    created_at = _parse_datetime(config.get("created_at")) or _parse_datetime(
        ks_config.get("started_at")
    )
    updated_at = _derive_updated_at(config, created_at)

    model_name = _to_optional_string(config.get("model"))
    llm_config: dict[str, Any] | None = {"model": model_name} if model_name else None

    current_iteration_source = ks_data if isinstance(ks_data, dict) else {}
    job = Job(
        id=job_id,
        owner_id=owner_id,
        title=title,
        description=_to_optional_string(config.get("description")) or title,
        investigation_mode=_to_string(
            config.get("investigation_mode") or ks_config.get("investigation_mode")
        )
        or "autonomous",
        status=_normalize_status(config.get("status")),
        max_iterations=_coerce_int(
            config.get("max_iterations") or ks_config.get("max_iterations"),
            default=10,
            minimum=1,
        ),
        current_iteration=_coerce_int(
            current_iteration_source.get("iteration"),
            default=0,
            minimum=0,
        ),
        short_title=_to_optional_string(config.get("short_title")),
        llm_provider=_to_string(config.get("provider")).lower() or "vertex",
        llm_config=llm_config,
        error_message=_to_optional_string(config.get("error") or config.get("error_message")),
        cancellation_reason=_to_optional_string(config.get("cancellation_reason")),
    )

    if created_at is not None:
        job.created_at = created_at
    if updated_at is not None:
        job.updated_at = updated_at
    if isinstance(ks_data, dict):
        consensus = _to_optional_string(ks_data.get("consensus_answer"))
        if consensus:
            job.consensus_answer = consensus

    return job, owner_id is None


def _load_job_payload(
    job_dir: Path,
    result: BootstrapResult,
    context: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None] | None:
    """
    Load per-job migration payloads from filesystem.

    Args:
        job_dir: Job directory to scan.
        result: Aggregate bootstrap result (for counters/errors).
        context: Context string for logging/error collection.

    Returns:
        Tuple of ``(config_json, knowledge_state_json)`` or ``None`` when the
        directory does not contain migratable payload files.
    """
    config = _load_json(job_dir / "config.json", result, context=context)
    ks_data = _load_json(job_dir / KS_FILENAME, result, context=context)
    if config is None and ks_data is None:
        result.skipped_empty_directory += 1
        return None
    return config, ks_data


async def _resolve_migration_target(
    session: AsyncSession,
    *,
    job_dir: Path,
    config: dict[str, Any] | None,
    ks_data: dict[str, Any] | None,
    legacy_id_map: dict[str, UUID],
    seed_time_counts_by_ms: dict[int, int],
    result: BootstrapResult,
    context: str,
) -> MigrationTarget | None:
    """
    Resolve canonical migration target for mixed UUID/legacy job IDs.

    Args:
        session: Active DB session.
        job_dir: Job directory being migrated.
        config: Optional parsed ``config.json`` payload.
        ks_data: Optional parsed ``knowledge_state.json`` payload.
        legacy_id_map: In-memory legacy-id to UUID map for this run.
        seed_time_counts_by_ms: In-memory seed-time collision counters for this run.
        result: Aggregate bootstrap result (for counters/errors).
        context: Context string for logging/error collection.

    Returns:
        Resolved migration target, or ``None`` when no usable identifier exists.
    """
    candidates = _extract_id_candidates(job_dir, config, ks_data)
    legacy_ids = _extract_legacy_ids(candidates)
    job_id = _resolve_uuid_candidate(candidates)
    dir_is_uuid = _parse_uuid(job_dir.name) is not None

    if job_id is not None:
        # Bind legacy aliases to the resolved UUID for consistency within this run.
        for legacy_id in legacy_ids:
            legacy_id_map.setdefault(legacy_id, job_id)
        return MigrationTarget(
            job_id=job_id, legacy_ids=legacy_ids, rename_directory=not dir_is_uuid
        )

    if not legacy_ids:
        result.skipped_invalid_job_id += 1
        msg = f"{context}: skipped (missing/invalid job id)"
        result.errors.append(msg)
        logger.warning(msg)
        return None

    mapped_id = next((legacy_id_map[item] for item in legacy_ids if item in legacy_id_map), None)
    if mapped_id is None:
        seed_time = _derive_uuidv7_seed_time_from_filesystem(job_dir)
        if seed_time is not None:
            seed_time = _offset_colliding_seed_time(seed_time, seed_time_counts_by_ms)
        try:
            mapped_id = await _generate_uuidv7(session, seed_time=seed_time)
        except Exception as e:
            result.skipped_invalid_job_id += 1
            msg = f"{context}: skipped (failed to allocate uuidv7): {e}"
            result.errors.append(msg)
            logger.error(msg, exc_info=True)
            return None

    for legacy_id in legacy_ids:
        legacy_id_map[legacy_id] = mapped_id

    return MigrationTarget(job_id=mapped_id, legacy_ids=legacy_ids, rename_directory=True)


def _prepare_migration_filesystem(
    *,
    job_dir: Path,
    config: dict[str, Any] | None,
    ks_data: dict[str, Any] | None,
    target: MigrationTarget,
    result: BootstrapResult,
    context: str,
) -> tuple[Path, dict[str, Any] | None, dict[str, Any] | None] | None:
    """
    Apply legacy filesystem/payload rewrites needed before metadata sync.

    Args:
        job_dir: Current job directory.
        config: Optional parsed ``config.json`` payload.
        ks_data: Optional parsed ``knowledge_state.json`` payload.
        target: Resolved migration target.
        result: Aggregate bootstrap result (for counters/errors).
        context: Context string for logging/error collection.

    Returns:
        Tuple of ``(effective_job_dir, config, ks_data)`` on success, else ``None``.
    """
    effective_job_dir = job_dir
    target_id_text = str(target.job_id)

    if target.rename_directory:
        renamed = _rename_job_dir(
            job_dir=job_dir,
            target_job_id=target.job_id,
            result=result,
            context=context,
        )
        if renamed is None:
            return None
        effective_job_dir = renamed

    config_changed = False
    ks_changed = False

    if config is not None:
        if _to_optional_string(config.get("job_id")) != target_id_text:
            config["job_id"] = target_id_text
            config_changed = True
        rewritten_config, rewrote_config_paths = _rewrite_payload_paths(
            config,
            target.legacy_ids,
            target_id_text,
        )
        config = rewritten_config
        config_changed = config_changed or rewrote_config_paths

    if ks_data is not None:
        if _to_optional_string(ks_data.get("job_id")) != target_id_text:
            ks_data["job_id"] = target_id_text
            ks_changed = True

        ks_config = ks_data.get("config")
        if not isinstance(ks_config, dict):
            ks_config = {}
            ks_data["config"] = ks_config
            ks_changed = True
        if _to_optional_string(ks_config.get("job_id")) != target_id_text:
            ks_config["job_id"] = target_id_text
            ks_changed = True

        rewritten_ks, rewrote_ks_paths = _rewrite_payload_paths(
            ks_data,
            target.legacy_ids,
            target_id_text,
        )
        ks_data = rewritten_ks
        ks_changed = ks_changed or rewrote_ks_paths

    if config is not None and config_changed:
        if not _write_json(effective_job_dir / "config.json", config, result, context):
            return None

    if ks_data is not None and ks_changed:
        if not _write_json(effective_job_dir / KS_FILENAME, ks_data, result, context):
            return None

    return effective_job_dir, config, ks_data


async def _get_or_create_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    config: dict[str, Any] | None,
    ks_data: dict[str, Any] | None,
    users_by_id: dict[UUID, User],
    dry_run: bool,
    result: BootstrapResult,
) -> Job:
    """
    Fetch an existing job or create a new model from legacy payloads.

    Args:
        session: Active DB session.
        job_id: Canonical job UUID.
        config: Optional parsed config payload.
        ks_data: Optional parsed knowledge-state payload.
        users_by_id: Existing users indexed by UUID.
        dry_run: If true, avoid DB writes for new jobs.
        result: Aggregate bootstrap result (for counters).

    Returns:
        Existing or newly constructed job model.
    """
    job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if job is not None:
        result.existing_jobs += 1
        if job.owner_id is None:
            result.orphan_jobs += 1
        return job

    job, is_orphan = _build_new_job(
        job_id=job_id,
        config=config or {},
        ks_data=ks_data,
        users_by_id=users_by_id,
    )
    if is_orphan:
        result.orphan_jobs += 1
    if not dry_run:
        session.add(job)
        await session.flush()
    result.created_jobs += 1
    return job


async def _persist_job_metadata(
    session: AsyncSession,
    *,
    result: BootstrapResult,
    job: Job,
    job_dir: Path,
    ks_data: dict[str, Any] | None,
    context: str,
) -> bool:
    """
    Persist data-file and plot metadata for a migrated job.

    Args:
        session: Active DB session.
        result: Aggregate bootstrap result (for counters/errors).
        job: Job model receiving metadata.
        job_dir: Filesystem job directory.
        ks_data: Optional parsed knowledge-state payload.
        context: Context string for logging/error collection.

    Returns:
        ``True`` when metadata persisted successfully, else ``False``.
    """
    try:
        result.data_files_added += await _sync_data_files(session, job, job_dir)
        if ks_data:
            result.plots_added += await _sync_plots(session, job, job_dir, ks_data)
        await session.commit()
        return True
    except Exception as e:
        await session.rollback()
        msg = f"{context}: failed while persisting job metadata: {e}"
        result.errors.append(msg)
        logger.error(msg, exc_info=True)
        return False


async def _sync_knowledge_state_payload(
    session: AsyncSession,
    *,
    result: BootstrapResult,
    job: Job,
    ks_data: dict[str, Any],
    context: str,
) -> None:
    """
    Normalize and persist legacy knowledge-state payload into DB tables.

    Args:
        session: Active DB session.
        result: Aggregate bootstrap result (for counters/errors).
        job: Target job model.
        ks_data: Raw knowledge-state payload.
        context: Context string for logging/error collection.
    """
    try:
        normalized_ks = _normalize_knowledge_state(
            raw_ks=ks_data,
            job_id=str(job.id),
            research_question=job.title,
            max_iterations=job.max_iterations,
        )
        ks = KnowledgeState.__new__(KnowledgeState)
        ks.data = normalized_ks
        await ks.save_to_database(str(job.id), job.owner_id, session=session)
        result.synced_knowledge_state += 1
    except Exception as e:
        await session.rollback()
        msg = f"{context}: failed while syncing knowledge state: {e}"
        result.errors.append(msg)
        logger.error(msg, exc_info=True)


async def bootstrap_jobs_from_filesystem(
    jobs_dir: Path = Path("jobs"),
    dry_run: bool = False,
) -> BootstrapResult:
    """
    Bootstrap legacy jobs from filesystem into the database.

    Args:
        jobs_dir: Directory containing legacy job folders.
        dry_run: Report actions without writing DB changes.

    Returns:
        Aggregate migration result counters and collected errors.
    """
    result = BootstrapResult()
    if not jobs_dir.exists():
        return result

    async with get_admin_session() as session:
        users = list((await session.execute(select(User))).scalars())
        users_by_id = {u.id: u for u in users}
        legacy_id_map: dict[str, UUID] = {}
        seed_time_counts_by_ms: dict[int, int] = {}

        for job_dir in sorted(p for p in jobs_dir.iterdir() if p.is_dir()):
            result.scanned_directories += 1
            context = f"job_dir={job_dir}"

            payload = _load_job_payload(job_dir, result, context)
            if payload is None:
                continue
            config, ks_data = payload

            target = await _resolve_migration_target(
                session,
                job_dir=job_dir,
                config=config,
                ks_data=ks_data,
                legacy_id_map=legacy_id_map,
                seed_time_counts_by_ms=seed_time_counts_by_ms,
                result=result,
                context=context,
            )
            if target is None:
                continue

            effective_job_dir = job_dir
            if not dry_run:
                prepared = _prepare_migration_filesystem(
                    job_dir=job_dir,
                    config=config,
                    ks_data=ks_data,
                    target=target,
                    result=result,
                    context=context,
                )
                if prepared is None:
                    continue
                effective_job_dir, config, ks_data = prepared

            job = await _get_or_create_job(
                session,
                job_id=target.job_id,
                config=config,
                ks_data=ks_data,
                users_by_id=users_by_id,
                dry_run=dry_run,
                result=result,
            )

            if dry_run:
                continue

            metadata_ok = await _persist_job_metadata(
                session,
                result=result,
                job=job,
                job_dir=effective_job_dir,
                ks_data=ks_data,
                context=context,
            )
            if not metadata_ok:
                continue

            if ks_data:
                await _sync_knowledge_state_payload(
                    session,
                    result=result,
                    job=job,
                    ks_data=ks_data,
                    context=context,
                )

    return result


def bootstrap_jobs_from_filesystem_sync(
    jobs_dir: Path = Path("jobs"),
    dry_run: bool = False,
) -> BootstrapResult:
    """
    Synchronous wrapper around ``bootstrap_jobs_from_filesystem``.

    Args:
        jobs_dir: Directory containing legacy job folders.
        dry_run: Report actions without writing DB changes.

    Returns:
        Aggregate migration result counters and collected errors.
    """
    return asyncio.run(
        bootstrap_jobs_from_filesystem(
            jobs_dir=jobs_dir,
            dry_run=dry_run,
        )
    )
