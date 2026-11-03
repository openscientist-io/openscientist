"""Supply-chain pinning for CI workflows and container images.

Parses the repository's own workflow files, Dockerfiles and compose file,
and asserts that build inputs stay pinned: workflow tokens scoped to
`contents: read`, no checkout credentials on disk, every uv invocation
locked, and every registry image referenced by digest.
"""

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
WORKFLOWS = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
DOCKERFILES = sorted(p for p in REPO_ROOT.glob("Dockerfile*") if p.is_file())

# Images built from this repository. They cannot carry a registry digest.
LOCAL_BUILT_IMAGES = {
    "openscientist",
    "openscientist-base",
    "openscientist-agent",
    "openscientist-executor",
    "openscientist-launcher",
}

DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}\Z")
FROM_RE = re.compile(r"^\s*FROM\s+(?:--[^\s]+\s+)*(\S+)", re.MULTILINE)
COPY_FROM_RE = re.compile(r"^\s*COPY\s+--from=(\S+)", re.MULTILINE)
UV_RE = re.compile(r"\buv\s+(?:sync|run|export)\b")


def _image_name(ref: str) -> str:
    return ref.split("/")[0].split(":")[0]


def _assert_digest(ref: str, where: str) -> None:
    if _image_name(ref) in LOCAL_BUILT_IMAGES:
        return
    assert DIGEST_RE.search(ref), f"{where}: {ref!r} must be pinned by digest"


def _load(path: Path) -> dict:
    with path.open() as fh:
        loaded = yaml.safe_load(fh)
    if not isinstance(loaded, dict):
        raise ValueError(f"{path.name} is not a YAML mapping")
    return loaded


def test_workflow_files_exist() -> None:
    assert WORKFLOWS


def test_dockerfiles_exist() -> None:
    assert DOCKERFILES


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_top_level_permissions(path: Path) -> None:
    wf = _load(path)

    permissions = wf.get("permissions")
    assert isinstance(permissions, dict), f"{path.name}: missing top-level permissions"
    assert permissions.get("contents") == "read"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_job_permissions_keep_contents_read(path: Path) -> None:
    wf = _load(path)

    for job in wf.get("jobs", {}).values():
        permissions = job.get("permissions")
        if isinstance(permissions, dict):
            assert permissions.get("contents") == "read", (
                f"{path.name}: job permissions may grant more but never drop contents: read"
            )


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_checkout_does_not_persist_credentials(path: Path) -> None:
    wf = _load(path)

    for job in wf.get("jobs", {}).values():
        for step in job.get("steps", []):
            uses = step.get("uses", "")
            if not uses.startswith("actions/checkout"):
                continue
            with_ = step.get("with", {}) or {}
            assert with_.get("persist-credentials") is False, (
                f"{path.name}: {uses} must set persist-credentials: false"
            )


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_uv_invocations_are_locked(path: Path) -> None:
    wf = _load(path)

    for job in wf.get("jobs", {}).values():
        for step in job.get("steps", []):
            run = step.get("run", "")
            if UV_RE.search(run):
                assert "--locked" in run, f"{path.name}: {run!r} must run with --locked"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_service_images_are_pinned(path: Path) -> None:
    wf = _load(path)

    for job in wf.get("jobs", {}).values():
        for name, service in (job.get("services") or {}).items():
            image = service.get("image")
            if image:
                _assert_digest(image, f"{path.name} service {name}")


@pytest.mark.parametrize("path", DOCKERFILES, ids=lambda p: p.name)
def test_dockerfile_from_lines_are_pinned(path: Path) -> None:
    for ref in FROM_RE.findall(path.read_text()):
        _assert_digest(ref, f"{path.name} FROM")


@pytest.mark.parametrize("path", DOCKERFILES, ids=lambda p: p.name)
def test_dockerfile_copy_from_lines_are_pinned(path: Path) -> None:
    for ref in COPY_FROM_RE.findall(path.read_text()):
        _assert_digest(ref, f"{path.name} COPY --from")


def test_compose_image_references_are_pinned() -> None:
    compose = _load(REPO_ROOT / "docker-compose.yml")

    for name, service in compose.get("services", {}).items():
        image = service.get("image")
        if image:
            _assert_digest(image, f"docker-compose.yml service {name}")
