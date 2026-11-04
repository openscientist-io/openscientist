"""Strict validation of web requests; every unknown field is refused."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from openscientist_launcher.config import LauncherConfig

JOB_ID = re.compile(r"^[0-9a-f-]{36}$")
LOGIN_ROLE = re.compile(r"^agent_[0-9a-f]{32}$")
EGRESS_ENTRY = re.compile(r"^[A-Za-z0-9.-]+:\d{1,5}$")

#: The only environment keys the agent template passes through. The provider
#: keys are the union every provider can emit into a job container; the rest
#: are the per-run values the web builds for each launch.
AGENT_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_BEDROCK_BASE_URL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_FOUNDRY_API_KEY",
        "ANTHROPIC_FOUNDRY_BASE_URL",
        "ANTHROPIC_FOUNDRY_RESOURCE",
        "ANTHROPIC_SMALL_FAST_MODEL",
        "ANTHROPIC_VERTEX_BASE_URL",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "AWS_ACCESS_KEY_ID",
        "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_PROFILE",
        "AWS_REGION",
        "AWS_SECRET_ACCESS_KEY",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_API_VERSION",
        "AZURE_OPENAI_RESOURCE",
        "AZURE_OPENAI_STREAM_MAX_RETRIES",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_SKIP_VERTEX_AUTH",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_VERTEX",
        "CLOUD_ML_REGION",
        "CODEX_API_KEY",
        "CODEX_AUTH_HOST_PATH",
        "DATABASE_URL",
        "GCP_BILLING_ACCOUNT_ID",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "JOB_DIR",
        "JOB_ID",
        "LLAMACPP_API_KEY",
        "LLAMACPP_BASE_URL",
        "OLLAMA_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENSCIENTIST_AIRGAPPED",
        "OPENSCIENTIST_CHAT_TURN",
        "OPENSCIENTIST_CODEX_TURN_TIMEOUT",
        "OPENSCIENTIST_CONTAINER_APP_DIR",
        "OPENSCIENTIST_DB_CONNECT_RETRY_SECONDS",
        "OPENSCIENTIST_EXEC_BROKER_URL",
        "OPENSCIENTIST_EXEC_TOKEN",
        "OPENSCIENTIST_HARNESS",
        "OPENSCIENTIST_HOST_PROJECT_DIR",
        "OPENSCIENTIST_LLM_PROXY_URL",
        "OPENSCIENTIST_MODEL",
        "OPENSCIENTIST_MODEL_CONTEXT_TOKENS",
        "OPENSCIENTIST_PROVIDER",
        "OPENSCIENTIST_RUN_MODE",
        "OPENSCIENTIST_SECRET_KEY",
        "PHENIX_PATH",
        "VERTEX_REGION_CLAUDE_4_5_HAIKU",
        "VERTEX_REGION_CLAUDE_4_5_SONNET",
        "VLLM_API_KEY",
        "VLLM_BASE_URL",
    }
)

AGENT_MODES = ("discovery", "report_only", "chat")

#: The executor payload is a template constant with these fields, and these only.
EXECUTOR_INPUT_FIELDS = (
    "code",
    "language",
    "data_path",
    "output_dir",
    "timeout",
    "description",
    "iteration",
    "data_files",
)


class ValidationError(Exception):
    """A request field the launcher refuses; the API answers 400 bad_request."""


@dataclass(frozen=True)
class AgentLaunch:
    job_id: str
    mode: str
    chat_turn: str | None
    lifetime_seconds: int
    env: dict[str, str]
    db_login: str | None
    run_home: tuple[Path, str] | None
    phenix: bool
    memory_mib: int
    cpu: float
    airgapped: bool
    egress_allow: tuple[str, ...]


@dataclass(frozen=True)
class ExecRequest:
    job_id: str
    timeout_seconds: int
    memory_mib: int
    cpu: float
    airgapped: bool
    egress_allow: tuple[str, ...]
    code: str
    language: str
    data_path: Path | None
    output_dir: Path
    payload_timeout: int
    description: str
    iteration: int
    data_files: tuple[dict[str, object], ...]


def _as_mapping(body: object, key: str) -> dict[str, object]:
    value = body.get(key) if isinstance(body, dict) else None
    if not isinstance(value, dict):
        raise ValidationError(f"{key} must be an object")
    return value


def _as_str(body: dict[str, object], key: str, *, required: bool = True) -> str | None:
    value = body.get(key)
    if value is None:
        if required:
            raise ValidationError(f"{key} is required")
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{key} must be a string")
    return value


def _as_required_str(body: dict[str, object], key: str) -> str:
    value = _as_str(body, key)
    if value is None:
        raise ValidationError(f"{key} is required")
    return value


def _as_bool(body: dict[str, object], key: str, *, required: bool = False) -> bool:
    value = body.get(key)
    if value is None:
        if required:
            raise ValidationError(f"{key} is required")
        return False
    if not isinstance(value, bool):
        raise ValidationError(f"{key} must be a boolean")
    return value


def _as_int(body: dict[str, object], key: str, *, minimum: int = 1, required: bool = True) -> int:
    value = body.get(key)
    if value is None:
        if required:
            raise ValidationError(f"{key} is required")
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{key} must be an integer")
    if value < minimum:
        raise ValidationError(f"{key} must be at least {minimum}")
    return value


def _as_number(body: dict[str, object], key: str, *, maximum: float) -> float:
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{key} must be a number")
    number = float(value)
    if number <= 0:
        raise ValidationError(f"{key} must be positive")
    if number > maximum:
        raise ValidationError(f"{key} exceeds the launcher's bound")
    return number


def _unknown_fields(body: dict[str, object], allowed: tuple[str, ...]) -> list[str]:
    return sorted(key for key in body if key not in allowed)


def _job_id(body: dict[str, object], cfg: LauncherConfig) -> str:
    job_id = _as_str(body, "job_id")
    if job_id is None:
        raise ValidationError("job_id is required")
    if not JOB_ID.match(job_id):
        raise ValidationError("job_id is not a job id")
    if not (cfg.jobs_root / job_id).is_dir():
        raise ValidationError("job_id names no job directory")
    return job_id


def _under_job_dir(path_str: str, job_dir: Path, *, what: str) -> Path:
    resolved = Path(path_str).resolve()
    try:
        resolved.relative_to(job_dir.resolve())
    except ValueError as exc:
        raise ValidationError(f"{what} is outside the job directory") from exc
    return resolved


def _under_jobs_root(path_str: str, cfg: LauncherConfig, *, what: str) -> Path:
    resolved = Path(path_str).resolve()
    try:
        resolved.relative_to(cfg.jobs_root.resolve())
    except ValueError as exc:
        raise ValidationError(f"{what} is outside the jobs root") from exc
    if not resolved.is_dir():
        raise ValidationError(f"{what} is not a directory")
    return resolved


def _run_home(body: dict[str, object], cfg: LauncherConfig) -> tuple[Path, str] | None:
    raw = body.get("run_home")
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {"path", "name"}:
        raise ValidationError("run_home must carry path and name")
    path_str = raw["path"]
    name = raw["name"]
    if not isinstance(path_str, str) or not isinstance(name, str):
        raise ValidationError("run_home path and name must be strings")
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise ValidationError("run_home name must be a single path component")
    return _under_jobs_root(path_str, cfg, what="run_home"), name


def validate_agent_request(body: object, cfg: LauncherConfig) -> AgentLaunch:
    if not isinstance(body, dict):
        raise ValidationError("the request body must be an object")
    unknown = _unknown_fields(
        body,
        (
            "job_id",
            "mode",
            "chat_turn",
            "lifetime_seconds",
            "env",
            "db_login",
            "run_home",
            "phenix",
            "memory_mib",
            "cpu",
            "airgapped",
            "egress_allow",
        ),
    )
    if unknown:
        raise ValidationError(f"unknown fields: {', '.join(unknown)}")

    job_id = _job_id(body, cfg)
    mode = _as_str(body, "mode")
    if mode not in AGENT_MODES:
        raise ValidationError("mode must be one of discovery, report_only, chat")
    chat_turn = _as_str(body, "chat_turn", required=False)
    if mode == "chat" and not chat_turn:
        raise ValidationError("chat mode requires chat_turn")
    if mode != "chat" and chat_turn is not None:
        raise ValidationError("chat_turn is only allowed in chat mode")

    lifetime = _as_int(body, "lifetime_seconds")
    if lifetime > cfg.max_lifetime_seconds:
        raise ValidationError("lifetime_seconds exceeds the launcher's bound")

    env: dict[str, str] = {}
    raw_env = body.get("env")
    if raw_env is not None:
        if not isinstance(raw_env, dict):
            raise ValidationError("env must be an object")
        for key, value in raw_env.items():
            if not isinstance(key, str) or key not in AGENT_ENV_ALLOWLIST:
                raise ValidationError(f"env key {key!r} is not allowed")
            if not isinstance(value, str):
                raise ValidationError(f"env value for {key} must be a string")
            env[key] = value

    db_login = _as_str(body, "db_login", required=False)
    if db_login is not None and not LOGIN_ROLE.match(db_login):
        raise ValidationError("db_login is not a database login role")

    phenix = _as_bool(body, "phenix")
    if phenix and not cfg.phenix_host_path:
        raise ValidationError("phenix is not configured on the launcher")

    memory_mib = _as_int(body, "memory_mib")
    if memory_mib > cfg.agent_max_memory_mib:
        raise ValidationError("memory_mib exceeds the launcher's bound")
    cpu = _as_number(body, "cpu", maximum=cfg.agent_max_cpu)

    airgapped = _as_bool(body, "airgapped")
    # The container starts behind the firewall in every mode, so the allow
    # list is required, air-gapped or not.
    raw_egress = body.get("egress_allow")
    if raw_egress is None or not isinstance(raw_egress, list) or not raw_egress:
        raise ValidationError("egress_allow is required")
    if len(raw_egress) > cfg.max_egress_entries:
        raise ValidationError("egress_allow exceeds the launcher's entry bound")
    egress_allow: tuple[str, ...]
    for entry in raw_egress:
        if not isinstance(entry, str) or not EGRESS_ENTRY.match(entry):
            raise ValidationError("egress_allow entry is not a valid host:port")
    egress_allow = tuple(raw_egress)

    return AgentLaunch(
        job_id=job_id,
        mode=mode,
        chat_turn=chat_turn,
        lifetime_seconds=lifetime,
        env=env,
        db_login=db_login,
        run_home=_run_home(body, cfg),
        phenix=phenix,
        memory_mib=memory_mib,
        cpu=cpu,
        airgapped=airgapped,
        egress_allow=egress_allow,
    )


def validate_exec_request(body: object, cfg: LauncherConfig) -> ExecRequest:
    if not isinstance(body, dict):
        raise ValidationError("the request body must be an object")
    unknown = _unknown_fields(
        body,
        ("job_id", "timeout_seconds", "memory_mib", "cpu", "airgapped", "egress_allow", "input"),
    )
    if unknown:
        raise ValidationError(f"unknown fields: {', '.join(unknown)}")

    job_id = _job_id(body, cfg)
    job_dir = cfg.jobs_root / job_id
    timeout_seconds = _as_int(body, "timeout_seconds")
    memory_mib = _as_int(body, "memory_mib")
    if memory_mib > cfg.executor_max_memory_mib:
        raise ValidationError("memory_mib exceeds the launcher's bound")
    cpu = _as_number(body, "cpu", maximum=cfg.executor_max_cpu)
    airgapped = _as_bool(body, "airgapped")

    # An open executor starts behind the restrict firewall, so it must be
    # told which endpoints to keep; an air-gapped one has no network at all.
    egress_allow: tuple[str, ...] = ()
    raw_egress = body.get("egress_allow")
    if airgapped:
        if raw_egress is not None:
            raise ValidationError("egress_allow is not allowed when airgapped")
    else:
        if raw_egress is None or not isinstance(raw_egress, list) or not raw_egress:
            raise ValidationError("egress_allow is required when not airgapped")
        if len(raw_egress) > cfg.max_egress_entries:
            raise ValidationError("egress_allow exceeds the launcher's entry bound")
        for entry in raw_egress:
            if not isinstance(entry, str) or not EGRESS_ENTRY.match(entry):
                raise ValidationError("egress_allow entry is not a valid host:port")
        egress_allow = tuple(raw_egress)

    input_body = _as_mapping(body, "input")
    unknown_input = _unknown_fields(input_body, EXECUTOR_INPUT_FIELDS)
    if unknown_input:
        raise ValidationError(f"unknown input fields: {', '.join(unknown_input)}")
    code = _as_required_str(input_body, "code")
    language = _as_required_str(input_body, "language")
    data_path_str = _as_str(input_body, "data_path", required=False)
    output_dir = _under_job_dir(
        _as_required_str(input_body, "output_dir"), job_dir, what="output_dir"
    )
    payload_timeout = _as_int(input_body, "timeout")
    description = _as_str(input_body, "description", required=False) or ""
    iteration = _as_int(input_body, "iteration", minimum=0)

    data_path = _under_job_dir(data_path_str, job_dir, what="data_path") if data_path_str else None

    raw_files = input_body.get("data_files")
    if raw_files is None:
        raw_files = []
    if not isinstance(raw_files, list):
        raise ValidationError("data_files must be a list")
    data_files: list[dict[str, object]] = []
    for entry in raw_files:
        if not isinstance(entry, dict):
            raise ValidationError("data_files entries must be objects")
        path = entry.get("path")
        if not isinstance(path, str):
            raise ValidationError("each data_files entry needs a path")
        if not _under_job_dir(path, job_dir, what="a data file path").is_file():
            raise ValidationError("a data file is missing")
        data_files.append(dict(entry))
    if data_path is not None:
        parents = {Path(str(file["path"])).resolve().parent for file in data_files}
        if data_path.resolve().parent not in parents:
            raise ValidationError("data_path is not under any mounted data directory")

    return ExecRequest(
        job_id=job_id,
        timeout_seconds=timeout_seconds,
        memory_mib=memory_mib,
        cpu=cpu,
        airgapped=airgapped,
        egress_allow=egress_allow,
        code=code,
        language=language,
        data_path=data_path,
        output_dir=output_dir,
        payload_timeout=payload_timeout,
        description=description,
        iteration=iteration,
        data_files=tuple(data_files),
    )
