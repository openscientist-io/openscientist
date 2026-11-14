"""Tests for `OpenAIDirectProvider` (the first CodexCompatible provider)."""

from __future__ import annotations

import base64
import json
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Queue
from unittest.mock import patch

import pytest

from openscientist.providers.base import CodexCompatible
from openscientist.providers.openai import (
    ChatgptCredentialError,
    ChatgptSigning,
    OpenAIDirectProvider,
)


@pytest.fixture(autouse=True)
def _auth_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic auth: no API key, but a fake codex home with auth.json so
    construction validates the same way in CI as locally."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    home = tmp_path_factory.mktemp("codexhome")
    (home / ".codex").mkdir()
    (home / ".codex" / "auth.json").write_text("{}")
    monkeypatch.setattr("openscientist.providers.openai.Path.home", lambda: home)


def test_is_codex_compatible() -> None:
    assert isinstance(OpenAIDirectProvider(), CodexCompatible)


def test_identity_and_codex_hooks() -> None:
    p = OpenAIDirectProvider()
    assert p.id == "openai"
    assert p.display_name == "OpenAI API"
    assert p.codex_model_provider_id() == "openai"
    assert p.codex_config_overrides() == []


def test_model_name_is_none_without_explicit_model() -> None:
    # No forced default: codex picks the account/config default.
    assert OpenAIDirectProvider().codex_model_name() is None


def test_codex_sdk_env_reflects_the_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    assert OpenAIDirectProvider().codex_sdk_env() == {}
    monkeypatch.setenv("CODEX_API_KEY", "placeholder-123")
    assert OpenAIDirectProvider().codex_sdk_env() == {"CODEX_API_KEY": "placeholder-123"}


def test_valid_with_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert OpenAIDirectProvider().validate_required_config() == []


def test_valid_with_codex_auth_json(tmp_path: Path) -> None:
    auth = tmp_path / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text("{}")
    with patch("openscientist.providers.openai.Path.home", return_value=tmp_path):
        assert OpenAIDirectProvider().validate_required_config() == []


def test_valid_with_codex_auth_host_path(tmp_path: Path) -> None:
    # In the agent container there is no ~/.codex and no key, but the runner
    # provisions auth into the per-job CODEX_HOME and signals it via this path.
    with patch("openscientist.providers.openai.Path.home", return_value=tmp_path):
        with patch("openscientist.providers.openai.get_settings") as mock_settings:
            mock_settings.return_value.provider.codex_auth_host_path = "/host/auth.json"
            mock_settings.return_value.provider.openai_api_key = None
            assert OpenAIDirectProvider().validate_required_config() == []


def test_invalid_without_any_auth(tmp_path: Path) -> None:
    # No API key, no ~/.codex/auth.json -> Provider.__init__ validates and raises.
    with patch("openscientist.providers.openai.Path.home", return_value=tmp_path):
        with pytest.raises(ValueError, match="auth"):
            OpenAIDirectProvider()


def test_get_cost_info_unavailable() -> None:
    info = OpenAIDirectProvider().get_cost_info()
    assert info.total_spend_usd is None
    assert info.recent_spend_usd is None


def test_get_provider_selects_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    """`provider_id="openai"` resolves to OpenAIDirectProvider via the factory."""
    from openscientist.providers import get_provider
    from openscientist.settings import clear_settings_cache

    monkeypatch.setenv("OPENSCIENTIST_PROVIDER", "openai")
    clear_settings_cache()
    try:
        assert isinstance(get_provider(), OpenAIDirectProvider)
    finally:
        clear_settings_cache()


# ── the web-held ChatGPT login (ChatgptSigning) ──────────────────────────


def _jwt(expiry: datetime | None) -> str:
    """A structurally valid JWT whose payload carries ``expiry`` as ``exp``."""
    header = (
        base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        .rstrip(b"=")
        .decode()
    )
    payload: dict[str, object] = (
        {"exp": int(expiry.timestamp())} if expiry is not None else {"sub": "acct-1"}
    )
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.signature"


def _write_auth(path: Path, access: str, refresh: str, *, last_refresh: str | None = None) -> None:
    data: dict = {
        "auth_mode": "chatgpt",
        "tokens": {
            "id_token": _jwt(None),
            "access_token": access,
            "refresh_token": refresh,
            "account_id": "acct-1",
        },
    }
    if last_refresh:
        data["last_refresh"] = last_refresh
    path.write_text(json.dumps(data))


def _token_endpoint_handler(endpoint: "_TokenEndpoint") -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("content-length", 0)))
            endpoint.requests.append(
                {"path": self.path, "headers": dict(self.headers.items()), "body": json.loads(raw)}
            )
            status, payload = endpoint.next_response()
            out = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *args: object) -> None:
            pass

    return _Handler


class _TokenEndpoint:
    """The token authority at its network edge: a real local HTTP server that
    records the refresh grants and replays the queued responses in order."""

    def __init__(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.requests: list[dict] = []
        self._queue: "Queue[tuple[int, dict]]" = Queue()
        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), _token_endpoint_handler(self))
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/oauth/token"

    def queue(self, status: int, payload: dict) -> None:
        self._queue.put((status, payload))

    def next_response(self) -> tuple[int, dict]:
        return self._queue.get()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class TestChatgptSigning:
    """The web-held ChatGPT login: the bearer, the single-flight refresh, the
    atomic write-back, and the refresh-failure policy."""

    def test_the_bearer_is_the_current_access_token(self, tmp_path: Path) -> None:
        endpoint = _TokenEndpoint()
        try:
            access = _jwt(datetime.now(UTC) + timedelta(hours=1))
            auth = tmp_path / "auth.json"
            _write_auth(auth, access, "rt-1")
            before = auth.read_text()
            signing = ChatgptSigning(str(auth), token_endpoint=endpoint.url)
            headers = signing.credential_headers(
                method="GET", path="/responses", query="", body=b""
            )
            assert headers == {"authorization": f"Bearer {access}"}
            # A fresh token needs no grant.
            assert endpoint.requests == []
            assert auth.read_text() == before
        finally:
            endpoint.stop()

    def test_a_near_expiry_token_is_refreshed_and_the_file_written_back(
        self, tmp_path: Path
    ) -> None:
        endpoint = _TokenEndpoint()
        try:
            stale = _jwt(datetime.now(UTC) + timedelta(seconds=60))
            fresh = _jwt(datetime.now(UTC) + timedelta(hours=1))
            auth = tmp_path / "auth.json"
            _write_auth(auth, stale, "rt-1")
            endpoint.queue(
                200, {"access_token": fresh, "id_token": "new-id-jwt", "refresh_token": "rt-2"}
            )
            signing = ChatgptSigning(str(auth), token_endpoint=endpoint.url)
            headers = signing.credential_headers(
                method="GET", path="/responses", query="", body=b""
            )
            assert headers == {"authorization": f"Bearer {fresh}"}
            # The refresh grant: JSON body to the token endpoint, the codex client id.
            assert len(endpoint.requests) == 1
            grant = endpoint.requests[0]
            assert grant["path"] == "/oauth/token"
            assert grant["headers"]["Content-Type"] == "application/json"
            assert grant["body"] == {
                "grant_type": "refresh_token",
                "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
                "refresh_token": "rt-1",
            }
            # The refreshed file keeps the login intact and is written back.
            data = json.loads(auth.read_text())
            assert data["auth_mode"] == "chatgpt"
            assert data["tokens"]["access_token"] == fresh
            assert data["tokens"]["refresh_token"] == "rt-2"
            assert data["tokens"]["id_token"] == "new-id-jwt"
            assert data["tokens"]["account_id"] == "acct-1"
            assert data["last_refresh"]
            # The next read uses the fresh token without another grant.
            assert signing.credential_headers(
                method="GET", path="/responses", query="", body=b""
            ) == {"authorization": f"Bearer {fresh}"}
            assert len(endpoint.requests) == 1
        finally:
            endpoint.stop()

    def test_a_refresh_writes_the_auth_file_in_place(self, tmp_path: Path) -> None:
        # The auth file is one bind-mounted file, so the write-back must go
        # in place. A temp file plus rename would swap the inode and re-chmod
        # the host file to 0600.
        endpoint = _TokenEndpoint()
        try:
            stale = _jwt(datetime.now(UTC) + timedelta(seconds=60))
            fresh = _jwt(datetime.now(UTC) + timedelta(hours=1))
            auth = tmp_path / "auth.json"
            _write_auth(auth, stale, "rt-1")
            auth.chmod(0o660)
            before = auth.stat().st_ino
            endpoint.queue(200, {"access_token": fresh, "refresh_token": "rt-2"})
            signing = ChatgptSigning(str(auth), token_endpoint=endpoint.url)
            signing.credential_headers(method="GET", path="/responses", query="", body=b"")
            after = auth.stat()
            assert after.st_ino == before
            assert after.st_mode & 0o777 == 0o660
            data = json.loads(auth.read_text())
            assert data["tokens"]["access_token"] == fresh
            assert data["tokens"]["refresh_token"] == "rt-2"
        finally:
            endpoint.stop()

    def test_concurrent_reads_refresh_the_token_once(self, tmp_path: Path) -> None:
        endpoint = _TokenEndpoint()
        try:
            stale = _jwt(datetime.now(UTC) + timedelta(seconds=60))
            fresh = _jwt(datetime.now(UTC) + timedelta(hours=1))
            auth = tmp_path / "auth.json"
            _write_auth(auth, stale, "rt-1")
            endpoint.queue(200, {"access_token": fresh, "refresh_token": "rt-2"})
            signing = ChatgptSigning(str(auth), token_endpoint=endpoint.url)
            seen: list[str] = []
            barrier = threading.Barrier(8)

            def worker() -> None:
                barrier.wait()
                seen.append(
                    signing.credential_headers(method="GET", path="/responses", query="", body=b"")[
                        "authorization"
                    ]
                )

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            assert seen == [f"Bearer {fresh}"] * 8
            assert len(endpoint.requests) == 1
        finally:
            endpoint.stop()

    def test_a_rejected_refresh_blocks_until_the_login_file_changes(self, tmp_path: Path) -> None:
        endpoint = _TokenEndpoint()
        try:
            stale = _jwt(datetime.now(UTC) + timedelta(seconds=60))
            auth = tmp_path / "auth.json"
            _write_auth(auth, stale, "rt-dead")
            endpoint.queue(
                400, {"error": "invalid_grant", "error_description": "refresh token expired"}
            )
            signing = ChatgptSigning(str(auth), token_endpoint=endpoint.url)
            with pytest.raises(ChatgptCredentialError):
                signing.credential_headers(method="GET", path="/responses", query="", body=b"")
            # Blocked: no further grants until the operator re-authenticates.
            with pytest.raises(ChatgptCredentialError):
                signing.credential_headers(method="GET", path="/responses", query="", body=b"")
            assert len(endpoint.requests) == 1
            # The host's codex CLI rewrites auth.json after a new login.
            reauthed = _jwt(datetime.now(UTC) + timedelta(hours=1))
            _write_auth(auth, reauthed, "rt-new")
            assert signing.credential_headers(
                method="GET", path="/responses", query="", body=b""
            ) == {"authorization": f"Bearer {reauthed}"}
            assert len(endpoint.requests) == 1
        finally:
            endpoint.stop()

    def test_a_transient_refresh_failure_serves_the_stale_token_and_retries(
        self, tmp_path: Path
    ) -> None:
        endpoint = _TokenEndpoint()
        try:
            stale = _jwt(datetime.now(UTC) + timedelta(seconds=60))
            fresh = _jwt(datetime.now(UTC) + timedelta(hours=1))
            auth = tmp_path / "auth.json"
            _write_auth(auth, stale, "rt-1")
            endpoint.queue(500, {"error": "unavailable"})
            endpoint.queue(200, {"access_token": fresh, "refresh_token": "rt-2"})
            signing = ChatgptSigning(
                str(auth), token_endpoint=endpoint.url, retry_after=timedelta(seconds=0.3)
            )
            # The stale token goes to the upstream, which 401s for the harness to retry.
            assert signing.credential_headers(
                method="GET", path="/responses", query="", body=b""
            ) == {"authorization": f"Bearer {stale}"}
            assert signing.credential_headers(
                method="GET", path="/responses", query="", body=b""
            ) == {"authorization": f"Bearer {stale}"}
            assert len(endpoint.requests) == 1
            time.sleep(0.35)
            assert signing.credential_headers(
                method="GET", path="/responses", query="", body=b""
            ) == {"authorization": f"Bearer {fresh}"}
            assert len(endpoint.requests) == 2
        finally:
            endpoint.stop()

    def test_a_missing_auth_file_raises(self, tmp_path: Path) -> None:
        signing = ChatgptSigning(str(tmp_path / "missing.json"))
        with pytest.raises(ChatgptCredentialError):
            signing.credential_headers(method="GET", path="/responses", query="", body=b"")
