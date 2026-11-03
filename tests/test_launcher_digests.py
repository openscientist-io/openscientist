"""The CLI records image digests after building, and the launcher accepts exactly those."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

import docker
from openscientist.cli import envfiles, stack
from tests.launcher import EXECUTOR_IMAGE, Launcher, image_digest
from tests.test_launcher_api import JOB_ID, LOGIN_ROLE

REPO_ROOT = Path(__file__).resolve().parents[1]

FAKE_AGENT_DIGEST = "sha256:" + "a" * 64
FAKE_EXECUTOR_DIGEST = "sha256:" + "b" * 64

AGENT_TAG = "openscientist-agent:latest"
EXECUTOR_TAG = "openscientist-executor:latest"


@pytest.fixture()
def checkout(tmp_path: Path) -> Path:
    """A fresh checkout skeleton: the env examples the wizard and stack read."""
    for name in (".env.example", ".env.owner.example"):
        (tmp_path / name).write_text(
            (REPO_ROOT / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    return tmp_path


def _fake_run(argv: tuple[str, ...], env: Mapping[str, str] | None = None) -> int:
    return 0


def _fake_inspect(image: str) -> str:
    return FAKE_AGENT_DIGEST if image == AGENT_TAG else FAKE_EXECUTOR_DIGEST


def test_stack_records_digests_after_build(checkout: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The builds are faked; the digest read is faked too, so no Docker needed."""
    monkeypatch.setattr(stack, "_image_id", _fake_inspect)
    code = stack.build_images(_fake_run, checkout)
    assert code == 0
    owner = envfiles.file_values(checkout, "owner")
    assert owner["AGENT_IMAGE_DIGESTS"] == FAKE_AGENT_DIGEST
    assert owner["EXECUTOR_IMAGE_DIGESTS"] == FAKE_EXECUTOR_DIGEST


def test_stack_records_only_the_latest_build(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stack, "_image_id", _fake_inspect)
    code = stack.build_images(_fake_run, checkout)
    assert code == 0
    old = envfiles.file_values(checkout, "owner")
    new_digest = "sha256:" + "c" * 64

    def _rebuilt(image: str) -> str:
        return new_digest if image == AGENT_TAG else old["EXECUTOR_IMAGE_DIGESTS"]

    code = stack.record_image_digests(checkout, image_id=_rebuilt)
    assert code == 0
    owner = envfiles.file_values(checkout, "owner")
    assert owner["AGENT_IMAGE_DIGESTS"] == new_digest
    assert owner["EXECUTOR_IMAGE_DIGESTS"] == old["EXECUTOR_IMAGE_DIGESTS"]


def test_stack_records_fails_when_the_image_is_missing(checkout: Path) -> None:
    code = stack.record_image_digests(checkout, image_id=lambda image: "")
    assert code == 1
    assert "AGENT_IMAGE_DIGESTS" not in envfiles.file_values(checkout, "owner")


@pytest.fixture()
def docker_client() -> Iterator[docker.DockerClient]:
    try:
        client = docker.from_env()
        client.ping()
        client.images.get(EXECUTOR_TAG)
    except docker.errors.DockerException as exc:
        pytest.skip(f"Docker or the executor image not available: {exc}")
    yield client


@pytest.fixture()
def test_agent_image(docker_client: docker.DockerClient) -> str:
    return _build_agent_stand_in(docker_client)


def _build_agent_stand_in(client: docker.DockerClient) -> str:
    from tests.test_launcher_api import _build_test_agent_image

    return _build_test_agent_image(client)


def test_launcher_accepts_exactly_the_cli_recorded_digests(
    checkout: Path, docker_client: docker.DockerClient, test_agent_image: str
) -> None:
    """Record the digests for real (no fake build boundary), then prove the
    launcher accepts exactly what the CLI recorded and refuses a different one."""
    (checkout / ".env").write_text(
        f"OPENSCIENTIST_AGENT_IMAGE={test_agent_image}\nOPENSCIENTIST_EXECUTOR_IMAGE={EXECUTOR_IMAGE}\n",
        encoding="utf-8",
    )
    code = stack.record_image_digests(checkout)
    assert code == 0
    owner = envfiles.file_values(checkout, "owner")
    agent_digest = owner["AGENT_IMAGE_DIGESTS"]
    executor_digest = owner["EXECUTOR_IMAGE_DIGESTS"]
    assert agent_digest == image_digest(docker_client, test_agent_image)
    assert executor_digest == image_digest(docker_client, EXECUTOR_IMAGE)

    jobs = checkout / "jobs"
    (jobs / JOB_ID).mkdir(parents=True)

    def _agent_body() -> dict[str, object]:
        return {
            "job_id": JOB_ID,
            "mode": "discovery",
            "lifetime_seconds": 60,
            "env": {"JOB_ID": JOB_ID},
            "db_login": LOGIN_ROLE,
            "memory_mib": 1024,
            "cpu": 0.5,
        }

    launcher = Launcher.start(
        checkout,
        jobs,
        AGENT_IMAGE=test_agent_image,
        AGENT_IMAGE_DIGESTS=agent_digest,
        EXECUTOR_IMAGE_DIGESTS=executor_digest,
    )
    try:
        with launcher.client() as client:
            response = client.post("/v1/containers/agent", json=_agent_body())
        assert response.status_code == 200, response.text
        with launcher.client() as client:
            client.post(f"/v1/containers/{response.json()['name']}/remove")
    finally:
        launcher.close()

    launcher = Launcher.start(
        checkout,
        jobs,
        AGENT_IMAGE=test_agent_image,
        AGENT_IMAGE_DIGESTS="sha256:" + "0" * 64,
        EXECUTOR_IMAGE_DIGESTS=executor_digest,
    )
    try:
        with launcher.client() as client:
            response = client.post("/v1/containers/agent", json=_agent_body())
        assert response.status_code == 409, response.text
        assert response.json()["error"].startswith("image_missing")
    finally:
        launcher.close()
