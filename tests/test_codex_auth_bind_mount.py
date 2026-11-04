"""The web-held ChatGPT login, persisted on a token refresh, across a
single-file bind mount the non-root web cannot rename over.

The web holds the codex CLI's auth.json mounted as one file at
/app/codex-auth.json. A refresh must persist the rotated tokens into that
same file, and the refresh runs as uid 1002 under a root-owned /app, so a
temp file plus rename in /app cannot work. The test runs the real signing in
a container as that uid with the real single-file mount and checks the host
file afterwards. Skipped when Docker is absent.
"""

from __future__ import annotations

import base64
import io
import json
import os
import tarfile
import tempfile
from collections.abc import Iterator
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import docker

REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_IMAGE = (
    "python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30"
)
TEST_IMAGE = f"openscientist-codex-auth-mount:{os.getpid()}"
WEB_UID = 1002
WEB_GID = 1001
AUTH_MOUNT = "/app/codex-auth.json"

DOCKERFILE = f"""\
FROM {BASE_IMAGE}
RUN pip install --no-cache-dir "pydantic>=2,<3" "pydantic-settings>=2,<3" "sqlalchemy>=2,<3" "requests>=2,<3"
RUN groupadd --gid 1001 jobs && useradd --uid 1002 --gid 1001 --system web
COPY src /opt/openscientist-src
COPY driver.py /driver.py
RUN mkdir /app
WORKDIR /app
USER web
"""

#: The driver: the real ChatgptSigning against the mounted file, with a local
#: token endpoint standing in for the ChatGPT grant, run as uid 1002 under
#: umask 0007.
DRIVER = """\
import base64
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "/opt/openscientist-src")

AUTH = "/app/codex-auth.json"


def b64url(obj):
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


fresh = f"{b64url({'alg': 'RS256', 'typ': 'JWT'})}.{b64url({'exp': int(time.time()) + 3600})}.fakesig"
grants = []


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        grants.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)))))
        body = json.dumps(
            {"access_token": fresh, "refresh_token": "rt-rotated", "id_token": "id-jwt"}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()

from openscientist.providers.openai import ChatgptSigning

try:
    with open("/app/probe", "w") as probe:
        probe.write("x")
    os.unlink("/app/probe")
    print("APP-STATE:WRITABLE")
except PermissionError:
    print("APP-STATE:READONLY")

signing = ChatgptSigning(AUTH, token_endpoint=f"http://127.0.0.1:{server.server_address[1]}")
headers = signing.credential_headers(method="POST", path="/responses", query="", body=b"{}")
print("BEARER:" + headers["authorization"])
print("FRESH:" + fresh)
data = json.load(open(AUTH))
print("FILE-ACCESS:" + data["tokens"]["access_token"])
print("FILE-REFRESH:" + data["tokens"]["refresh_token"])
print("GRANTS:" + str(len(grants)))
server.shutdown()
"""


def _jwt(expires: datetime) -> str:
    """A structurally valid JWT whose ``exp`` claim is ``expires``."""

    def b64url(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    header = b64url({"alg": "RS256", "typ": "JWT"})
    body = b64url({"exp": int(expires.timestamp())})
    return f"{header}.{body}.fakesig"


def _stat(path: Path) -> tuple[int, int, int]:
    st = path.stat()
    return st.st_uid, st.st_gid, st.st_mode & 0o7777


@pytest.fixture(scope="module")
def docker_client() -> Iterator[docker.DockerClient]:
    try:
        client = docker.from_env()
        client.ping()
    except docker.errors.DockerException as exc:
        pytest.skip(f"Docker not available: {exc}")
    yield client


@pytest.fixture(scope="module")
def auth_mount_image(docker_client: docker.DockerClient) -> Iterator[str]:
    """Build the stand-in web image. The tag carries the pid so the shared
    daemon never serves a sibling test's image under a shared tag."""
    files: list[tuple[str, bytes]] = [
        ("Dockerfile", DOCKERFILE.encode()),
        ("driver.py", DRIVER.encode()),
    ]
    for path in sorted((REPO_ROOT / "src").rglob("*")):
        if path.is_file():
            files.append((str(path.relative_to(REPO_ROOT)), path.read_bytes()))
    tar_path = Path(tempfile.mkstemp(suffix=".tar", dir=REPO_ROOT / "docker")[1])
    try:
        with tarfile.open(tar_path, "w") as tar:
            for name, data in files:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        with tar_path.open("rb") as tar_file:
            docker_client.images.build(
                fileobj=tar_file, tag=TEST_IMAGE, rm=True, custom_context=True
            )
    finally:
        tar_path.unlink(missing_ok=True)
    yield TEST_IMAGE
    with suppress(docker.errors.ImageNotFound):
        docker_client.images.remove(TEST_IMAGE, force=True)


def test_a_refresh_persists_across_a_single_file_bind_mount(
    docker_client: docker.DockerClient, auth_mount_image: str, tmp_path: Path
) -> None:
    # The host auth file, stale enough to force a refresh on the first request.
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": _jwt(datetime.now(UTC) + timedelta(seconds=60)),
                    "refresh_token": "rt-1",
                    "account_id": "acct-1",
                },
            }
        )
    )
    # The documented CODEX_AUTH_FILE. Host-user owned, group-owned by the web's
    # gid with the group-writable bit, when the test user may chown.
    host_owned = False
    try:
        os.chown(auth, 1000, WEB_GID)
        host_owned = True
    except OSError:
        pass
    auth.chmod(0o660 if host_owned else 0o666)
    before = _stat(auth)

    container = docker_client.containers.run(
        auth_mount_image,
        command=["sh", "-c", "umask 0007 && exec python3 /driver.py"],
        user=f"{WEB_UID}:{WEB_GID}",
        volumes={str(auth): {"bind": AUTH_MOUNT, "mode": "rw"}},
        detach=True,
    )
    try:
        result = container.wait(timeout=120)
        exit_code = result["StatusCode"] if isinstance(result, dict) else result[0]
        logs = container.logs().decode()
    finally:
        container.remove(force=True)

    lines = dict(line.split(":", 1) for line in logs.splitlines() if ":" in line)
    assert exit_code == 0, logs
    # The test setup must reproduce the deploy shape. A writable /app would let
    # a temp file plus rename work and prove nothing.
    assert lines["APP-STATE"] == "READONLY", logs
    assert lines["GRANTS"] == "1", logs
    fresh = lines["FRESH"]
    assert lines["BEARER"] == f"Bearer {fresh}"
    assert lines["FILE-ACCESS"] == fresh, "the refreshed token was not persisted"
    assert lines["FILE-REFRESH"] == "rt-rotated"
    # The rotation reached the host file through the bind mount.
    data = json.loads(auth.read_text())
    assert data["tokens"]["access_token"] == fresh
    assert data["tokens"]["refresh_token"] == "rt-rotated"
    assert data["last_refresh"]
    # The write went in place. The host file keeps its owner, group and mode.
    assert _stat(auth) == before
