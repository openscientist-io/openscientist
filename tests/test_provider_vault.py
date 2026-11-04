"""The web-side omp vault: per-provider signing, the single-flight refresh, the
write-back, the lease and the block. A real local token endpoint stands in for
the provider's, and the vault is a real SQLite database in a temp dir."""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Queue
from typing import cast

import pytest

from openscientist.providers.base import UpstreamCredentialError
from openscientist.providers.vault import (
    OmpVault,
    VaultCredentialError,
    vault_llm_upstream,
    vault_signing_for,
)
from tests.omp_vault import make_jwt, make_vault, now_ms

_STALE_MS = now_ms() - 600_000
_FRESH_MS = now_ms() + 3_600_000


def _anthropic_credential(**over: object) -> dict:
    data = {
        "access": "sk-ant-oat01-vault-access",
        "refresh": "vault-anthropic-refresh",
        "expires": _STALE_MS,
        "accountId": "acct-anth",
        "email": "fake@example.com",
        "orgId": "org-1",
        "orgName": "Fake Org",
    }
    data.update(over)
    return data


def _codex_credential(**over: object) -> dict:
    data = {
        "access": make_jwt(datetime.now(UTC) - timedelta(minutes=10)),
        "refresh": "vault-codex-refresh",
        "expires": _STALE_MS,
        "accountId": "acct-1",
        "email": "fake@example.com",
        "orgId": "acct-1",
        "orgName": "fake-plan",
        "authorizedAt": int(datetime.now(UTC).timestamp()),
    }
    data.update(over)
    return data


def _copilot_credential(**over: object) -> dict:
    data = {
        "access": "vault-copilot-access",
        "refresh": "vault-copilot-access",
        "expires": now_ms() + 365 * 86_400_000,
        "apiEndpoint": "https://api.githubcopilot.com",
        "authorizedAt": int(datetime.now(UTC).timestamp()),
    }
    data.update(over)
    return data


def _token_endpoint_handler(endpoint: "_TokenEndpoint") -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("content-length", 0)))
            endpoint.requests.append(
                {"path": self.path, "headers": dict(self.headers.items()), "body": raw}
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
    """A real local HTTP token authority that records the refresh grants and
    replays the queued responses in order."""

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


class TestVaultRead:
    def test_reads_the_newest_oauth_credential_per_provider(self, tmp_path: Path) -> None:
        now_s = int(datetime.now(UTC).timestamp())
        make_vault(
            tmp_path,
            {
                "anthropic": _anthropic_credential(expires=_FRESH_MS),
                "openai-codex": _codex_credential(),
                "github-copilot": _copilot_credential(),
            },
            now_s=now_s,
        )
        # A second, newer anthropic row: the broker takes the newest.
        import sqlite3

        con = sqlite3.connect(tmp_path / "agent.db")
        con.execute(
            "INSERT INTO auth_credentials (provider, credential_type, data, created_at, updated_at) "
            "VALUES (?, 'oauth', ?, ?, ?)",
            (
                "anthropic",
                json.dumps(_anthropic_credential(access="newest-access", expires=_FRESH_MS)),
                now_s,
                now_s + 10,
            ),
        )
        con.commit()
        con.close()

        vault = OmpVault(tmp_path)
        cred = vault.credential("anthropic")
        assert cred is not None
        assert cred.access == "newest-access"
        assert cred.provider == "anthropic"
        assert cred.expires_ms == _FRESH_MS
        # The other fields ride along for the write-back.
        assert cred.data["orgName"] == "Fake Org"

    def test_a_provider_with_no_oauth_row_is_none(self, tmp_path: Path) -> None:
        make_vault(tmp_path, {"anthropic": _anthropic_credential()})
        vault = OmpVault(tmp_path)
        assert vault.credential("openai-codex") is None

    def test_an_unknown_provider_refuses(self, tmp_path: Path) -> None:
        make_vault(tmp_path, {"anthropic": _anthropic_credential()})
        with pytest.raises(ValueError, match="unknown vault provider"):
            vault_signing_for(tmp_path, "xai")
        with pytest.raises(ValueError, match="unknown vault provider"):
            vault_llm_upstream(tmp_path, "xai")


class TestAnthropicSigning:
    def test_a_fresh_token_signs_without_a_grant(self, tmp_path: Path) -> None:
        endpoint = _TokenEndpoint()
        try:
            make_vault(tmp_path, {"anthropic": _anthropic_credential(expires=_FRESH_MS)})
            vault = OmpVault(tmp_path)
            signing = vault.signing_for("anthropic", token_endpoint=endpoint.url)
            headers = signing.credential_headers(
                method="POST", path="/v1/messages", query="", body=b"{}"
            )
            assert headers == {"authorization": "Bearer sk-ant-oat01-vault-access"}
            assert endpoint.requests == []
        finally:
            endpoint.stop()

    def test_a_stale_token_is_refreshed_and_written_back(self, tmp_path: Path) -> None:
        endpoint = _TokenEndpoint()
        try:
            make_vault(tmp_path, {"anthropic": _anthropic_credential()})
            fresh_expires_s = int((datetime.now(UTC) + timedelta(hours=1)).timestamp())
            endpoint.queue(
                200,
                {
                    "access_token": "fresh-anthropic-access",
                    "refresh_token": "rotated-refresh",
                    "expires_at": fresh_expires_s,
                },
            )
            vault = OmpVault(tmp_path)
            signing = vault.signing_for("anthropic", token_endpoint=endpoint.url)
            headers = signing.credential_headers(
                method="POST", path="/v1/messages", query="", body=b"{}"
            )
            assert headers == {"authorization": "Bearer fresh-anthropic-access"}
            # The grant the binary sends: JSON to the token endpoint with the
            # oauth beta header and the client id.
            assert len(endpoint.requests) == 1
            grant = endpoint.requests[0]
            assert grant["path"] == "/oauth/token"
            assert json.loads(grant["body"]) == {
                "grant_type": "refresh_token",
                "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
                "refresh_token": "vault-anthropic-refresh",
            }
            assert grant["headers"].get("anthropic-beta") == "oauth-2025-04-20"
            # The write-back: the rotated tokens in, the other fields intact.
            con = _con(tmp_path)
            row = con.execute(
                "SELECT data FROM auth_credentials WHERE provider = 'anthropic' "
                "ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
            con.close()
            data = json.loads(row[0])
            assert data["access"] == "fresh-anthropic-access"
            assert data["refresh"] == "rotated-refresh"
            assert data["expires"] == fresh_expires_s * 1000
            assert data["accountId"] == "acct-anth"
            assert data["email"] == "fake@example.com"
            assert data["orgId"] == "org-1"
            assert data["orgName"] == "Fake Org"
            # The next read uses the fresh token without another grant.
            assert signing.credential_headers(
                method="POST", path="/v1/messages", query="", body=b"{}"
            ) == {"authorization": "Bearer fresh-anthropic-access"}
            assert len(endpoint.requests) == 1
        finally:
            endpoint.stop()

    def test_a_rejected_refresh_blocks_the_credential(self, tmp_path: Path) -> None:
        endpoint = _TokenEndpoint()
        try:
            make_vault(tmp_path, {"anthropic": _anthropic_credential()})
            endpoint.queue(400, {"error": "invalid_grant"})
            vault = OmpVault(tmp_path)
            signing = vault.signing_for("anthropic", token_endpoint=endpoint.url)
            with pytest.raises(VaultCredentialError, match="blocked"):
                signing.credential_headers(method="POST", path="/v1/messages", query="", body=b"{}")
            # Blocked: no further grants until the operator re-authenticates.
            with pytest.raises(VaultCredentialError, match="blocked"):
                signing.credential_headers(method="POST", path="/v1/messages", query="", body=b"{}")
            assert len(endpoint.requests) == 1
        finally:
            endpoint.stop()

    def test_a_transient_refresh_failure_serves_the_stale_token_and_backs_off(
        self, tmp_path: Path
    ) -> None:
        endpoint = _TokenEndpoint()
        try:
            make_vault(tmp_path, {"anthropic": _anthropic_credential()})
            endpoint.queue(500, {"error": "boom"})
            vault = OmpVault(tmp_path)
            signing = vault.signing_for("anthropic", token_endpoint=endpoint.url)
            headers = signing.credential_headers(
                method="POST", path="/v1/messages", query="", body=b"{}"
            )
            # The stale token is served; the run keeps going.
            assert headers == {"authorization": "Bearer sk-ant-oat01-vault-access"}
            assert len(endpoint.requests) == 1
            # Inside the backoff window no further grant is attempted.
            headers = signing.credential_headers(
                method="POST", path="/v1/messages", query="", body=b"{}"
            )
            assert headers == {"authorization": "Bearer sk-ant-oat01-vault-access"}
            assert len(endpoint.requests) == 1
        finally:
            endpoint.stop()

    def test_concurrent_requests_refresh_once(self, tmp_path: Path) -> None:
        endpoint = _TokenEndpoint()
        try:
            make_vault(tmp_path, {"anthropic": _anthropic_credential()})
            endpoint.queue(
                200, {"access_token": "fresh-anthropic-access", "expires_at": _FRESH_MS // 1000}
            )
            vault = OmpVault(tmp_path)
            signing = vault.signing_for("anthropic", token_endpoint=endpoint.url)
            seen: list[str] = []
            barrier = threading.Barrier(8)

            def worker() -> None:
                barrier.wait()
                seen.append(
                    signing.credential_headers(
                        method="POST", path="/v1/messages", query="", body=b"{}"
                    )["authorization"]
                )

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            assert seen == ["Bearer fresh-anthropic-access"] * 8
            assert len(endpoint.requests) == 1
        finally:
            endpoint.stop()


class TestCodexSigning:
    def test_a_stale_token_is_refreshed_through_the_shared_chatgpt_grant(
        self, tmp_path: Path
    ) -> None:
        endpoint = _TokenEndpoint()
        try:
            make_vault(tmp_path, {"openai-codex": _codex_credential()})
            fresh_jwt = make_jwt(datetime.now(UTC) + timedelta(hours=1))
            endpoint.queue(200, {"access_token": fresh_jwt, "refresh_token": "rt-2"})
            vault = OmpVault(tmp_path)
            signing = vault.signing_for("openai-codex", token_endpoint=endpoint.url)
            headers = signing.credential_headers(
                method="POST", path="/responses", query="", body=b"{}"
            )
            assert headers == {"authorization": f"Bearer {fresh_jwt}"}
            # The ChatGPT grant: form-urlencoded, as the binary sends it.
            assert len(endpoint.requests) == 1
            grant = endpoint.requests[0]
            assert grant["path"] == "/oauth/token"
            assert grant["headers"].get("Content-Type") == "application/x-www-form-urlencoded"
            assert grant["body"].decode() == (
                "grant_type=refresh_token"
                "&client_id=app_EMoamEEZ73f0CkXaXp7hrann"
                "&refresh_token=vault-codex-refresh"
            )
            data = _latest_data(tmp_path, "openai-codex")
            assert data["access"] == fresh_jwt
            assert data["refresh"] == "rt-2"
            # The other fields ride through.
            assert data["accountId"] == "acct-1"
            assert data["orgName"] == "fake-plan"
            # The upstream is the codex backend, not the proxy.
            upstream = vault_llm_upstream(tmp_path, "openai-codex")
            assert upstream is not None
            assert upstream.base_url == "https://chatgpt.com/backend-api/codex"
        finally:
            endpoint.stop()

    def test_concurrent_requests_refresh_once(self, tmp_path: Path) -> None:
        endpoint = _TokenEndpoint()
        try:
            make_vault(tmp_path, {"openai-codex": _codex_credential()})
            fresh_jwt = make_jwt(datetime.now(UTC) + timedelta(hours=1))
            endpoint.queue(200, {"access_token": fresh_jwt})
            vault = OmpVault(tmp_path)
            signing = vault.signing_for("openai-codex", token_endpoint=endpoint.url)
            seen: list[str] = []
            barrier = threading.Barrier(8)

            def worker() -> None:
                barrier.wait()
                seen.append(
                    signing.credential_headers(
                        method="POST", path="/responses", query="", body=b"{}"
                    )["authorization"]
                )

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            assert seen == [f"Bearer {fresh_jwt}"] * 8
            assert len(endpoint.requests) == 1
        finally:
            endpoint.stop()


class TestCopilotSigning:
    def test_the_device_token_signs_and_the_endpoint_comes_from_the_login(
        self, tmp_path: Path
    ) -> None:
        endpoint = _TokenEndpoint()
        try:
            make_vault(tmp_path, {"github-copilot": _copilot_credential()})
            vault = OmpVault(tmp_path)
            signing = vault.signing_for("github-copilot", token_endpoint=endpoint.url)
            headers = signing.credential_headers(
                method="POST", path="/chat/completions", query="", body=b"{}"
            )
            assert headers == {"authorization": "Bearer vault-copilot-access"}
            assert endpoint.requests == []
            upstream = vault_llm_upstream(tmp_path, "github-copilot")
            assert upstream is not None
            assert upstream.base_url == "https://api.githubcopilot.com"
            assert (
                upstream.signing.upstream_url(
                    base_url=upstream.base_url, path="/chat/completions", query=""
                )
                == "https://api.githubcopilot.com/chat/completions"
            )
        finally:
            endpoint.stop()

    def test_an_expired_device_token_is_blocked_without_a_grant(self, tmp_path: Path) -> None:
        endpoint = _TokenEndpoint()
        try:
            make_vault(tmp_path, {"github-copilot": _copilot_credential(expires=_STALE_MS)})
            vault = OmpVault(tmp_path)
            signing = vault.signing_for("github-copilot", token_endpoint=endpoint.url)
            with pytest.raises(VaultCredentialError, match="blocked"):
                signing.credential_headers(
                    method="POST", path="/chat/completions", query="", body=b"{}"
                )
            assert endpoint.requests == []
        finally:
            endpoint.stop()


class TestLease:
    def test_the_web_does_not_refresh_a_credential_a_host_omp_lease_holds(
        self, tmp_path: Path
    ) -> None:
        endpoint = _TokenEndpoint()
        try:
            make_vault(tmp_path, {"anthropic": _anthropic_credential()})
            import sqlite3

            con = sqlite3.connect(tmp_path / "agent.db")
            credential_id = con.execute(
                "SELECT id FROM auth_credentials WHERE provider = 'anthropic'"
            ).fetchone()[0]
            con.execute(
                "INSERT INTO auth_credential_refresh_leases "
                "(credential_id, owner, expires_at_ms, updated_at) VALUES (?, ?, ?, ?)",
                (credential_id, "omp-host", now_ms() + 60_000, now_ms()),
            )
            con.commit()
            con.close()
            endpoint.queue(200, {"access_token": "fresh", "expires_at": _FRESH_MS // 1000})
            vault = OmpVault(tmp_path)
            signing = vault.signing_for("anthropic", token_endpoint=endpoint.url)
            headers = signing.credential_headers(
                method="POST", path="/v1/messages", query="", body=b"{}"
            )
            # The stale token is served; the host omp is the one refreshing.
            assert headers == {"authorization": "Bearer sk-ant-oat01-vault-access"}
            assert endpoint.requests == []
        finally:
            endpoint.stop()

    def test_an_expired_lease_is_claimed_by_the_web(self, tmp_path: Path) -> None:
        make_vault(tmp_path, {"anthropic": _anthropic_credential()})
        import sqlite3

        con = sqlite3.connect(tmp_path / "agent.db")
        credential_id = con.execute(
            "SELECT id FROM auth_credentials WHERE provider = 'anthropic'"
        ).fetchone()[0]
        con.execute(
            "INSERT INTO auth_credential_refresh_leases "
            "(credential_id, owner, expires_at_ms, updated_at) VALUES (?, ?, ?, ?)",
            (credential_id, "omp-host", now_ms() - 1_000, now_ms()),
        )
        con.commit()
        con.close()
        vault = OmpVault(tmp_path)
        assert vault.claim_lease(credential_id) is True
        assert vault.lease_holder(credential_id) == "openscientist-web"
        vault.release_lease(credential_id)
        assert vault.lease_holder(credential_id) is None


class TestUnwritableVault:
    """The vault's ``agent.db`` is not writable by the web (the host dir is not
    group-owned by the web's gid). The web signs with the stale token until a
    refresh is due, and the storage failure must surface as the credential
    error the proxy refuses cleanly, never as an unhandled 500."""

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses read-only file permissions")
    def test_a_refresh_on_an_unwritable_vault_raises_the_credential_error(
        self, tmp_path: Path
    ) -> None:
        make_vault(tmp_path, {"anthropic": _anthropic_credential()})
        (tmp_path / "agent.db").chmod(0o444)
        signing = vault_signing_for(tmp_path, "anthropic")
        with pytest.raises(VaultCredentialError, match="not usable by the web"):
            signing.credential_headers(method="POST", path="/v1/messages", query="", body=b"{}")
        # The error type the LLM proxy already turns into a clean refusal.
        with pytest.raises(UpstreamCredentialError):
            signing.credential_headers(method="POST", path="/v1/messages", query="", body=b"{}")

    @pytest.mark.parametrize("provider_id", ["openai", "anthropic"])
    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses read-only file permissions")
    def test_an_unwritable_vault_is_reported_when_the_provider_is_built(
        self,
        provider_id: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        make_vault(vault_dir, {})
        for key in (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CODEX_AUTH_HOST_PATH",
            "AGENT",
        ):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("OPENSCIENTIST_PROVIDER", provider_id)
        monkeypatch.setenv("OMP_AUTH_HOST_PATH", str(vault_dir))
        from openscientist.providers import provider_class
        from openscientist.settings import clear_settings_cache

        clear_settings_cache()
        try:
            vault_dir.chmod(0o500)
            with caplog.at_level(logging.WARNING):
                provider_class(provider_id)()
            assert "the omp vault dir" in caplog.text
            assert "not writable by the web" in caplog.text
            caplog.clear()
            vault_dir.chmod(0o755)
            (vault_dir / "agent.db").chmod(0o444)
            with caplog.at_level(logging.WARNING):
                provider_class(provider_id)()
            assert "agent.db" in caplog.text
            assert "not writable by the web" in caplog.text
        finally:
            vault_dir.chmod(0o755)
            (vault_dir / "agent.db").chmod(0o644)
            clear_settings_cache()


class TestUpstream:
    def test_no_credential_means_no_upstream(self, tmp_path: Path) -> None:
        make_vault(tmp_path, {})
        assert vault_llm_upstream(tmp_path, "anthropic") is None

    def test_the_signing_is_cached_per_vault_and_provider(self, tmp_path: Path) -> None:
        make_vault(tmp_path, {"anthropic": _anthropic_credential(expires=_FRESH_MS)})
        first = vault_signing_for(tmp_path, "anthropic")
        second = vault_signing_for(tmp_path, "anthropic")
        assert first is second


def _con(vault_dir: Path):
    import sqlite3

    return sqlite3.connect(vault_dir / "agent.db")


def _latest_data(vault_dir: Path, provider: str) -> dict:
    con = _con(vault_dir)
    row = con.execute(
        "SELECT data FROM auth_credentials WHERE provider = ? ORDER BY updated_at DESC LIMIT 1",
        (provider,),
    ).fetchone()
    con.close()
    return cast(dict, json.loads(row[0]))
