"""The web-side omp login vault.

The omp harness stores the operator's logins in an ``agent.db`` under its agent
dir, which the web holds at ``OMP_AUTH_HOST_PATH``. The web signs the proxied
requests for each vault provider with the vault's current access token,
refreshing it when it leaves the fresh window and writing the refreshed row
back so the host's own omp stays current. The job container never sees the
vault: its omp is pointed at the proxy, which does the signing.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests

from openscientist.providers.base import (
    LlmUpstream,
    UpstreamCredentialError,
    UpstreamSigning,
)
from openscientist.providers.chatgpt import (
    CLIENT_ID as _CODEX_CLIENT_ID,
)
from openscientist.providers.chatgpt import (
    TOKEN_ENDPOINT as _CODEX_TOKEN_ENDPOINT,
)
from openscientist.providers.chatgpt import (
    ChatgptRefreshRejectedError,
    ChatgptRefreshTransientError,
    refresh_chatgpt_tokens,
)

logger = logging.getLogger(__name__)

#: The vault providers and their upstream base URLs, keyed by the vault's own
#: provider names (observed in ``agent.db``), not OpenScientist provider ids.
_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
_COPILOT_DEFAULT_BASE_URL = "https://api.githubcopilot.com"

#: The web's refresh-lease owner, distinct from host omp's.
_LEASE_OWNER = "openscientist-web"
#: How long the web's refresh lease holds the credential, ms.
_LEASE_TTL_MS = 30_000
#: Refresh the access token once it is this close to expiry, matching omp's own
#: fresh window and the codex CLI's.
_STALE_WINDOW = timedelta(minutes=5)
#: How long a rejected refresh keeps the credential blocked before a retry.
_BLOCK_TTL_MS = 5 * 60_000
#: The backoff before the web retries a transiently failed refresh.
_RETRY_AFTER = timedelta(seconds=30)
#: The refresh grant is a short single call; the proxy must not hang on it.
_GRANT_TIMEOUT = 10.0

#: The anthropic OAuth refresh grant, as ``omp login`` sends it.
_ANTHROPIC_TOKEN_ENDPOINT = "https://api.anthropic.com/v1/oauth/token"
_ANTHROPIC_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_ANTHROPIC_BETA_HEADER = "anthropic-beta"
_ANTHROPIC_BETA_VALUE = "oauth-2025-04-20"


class VaultCredentialError(UpstreamCredentialError):
    """The web-held vault credential is missing or unusable; the proxy refuses
    the forwarded request with it."""


@dataclass(frozen=True)
class VaultCredential:
    """One oauth login row of the vault's ``auth_credentials``."""

    credential_id: int
    provider: str
    access: str
    refresh: str
    expires_ms: int | None
    data: dict
    updated_at: int


class OmpVault:
    """Reads and refreshes the ``agent.db`` of the omp agent dir.

    SQLite WAL lets the web and a running host omp share the database; the
    refresh lease and block tables coordinate them, so neither refreshes a
    credential the other holds.
    """

    def __init__(self, vault_dir: Path) -> None:
        self._db = vault_dir / "agent.db"
        self._lock = threading.Lock()

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        """One vault connection, closed on exit. An OperationalError is a
        vault the web cannot use (typically not writable), reported as a
        credential error the proxy refuses cleanly instead of a 500."""
        con = sqlite3.connect(self._db)
        try:
            yield con
        except sqlite3.OperationalError as exc:
            raise VaultCredentialError(
                f"the omp vault at {self._db.parent} is not usable by the web "
                f"(uid {os.getuid()}, gid {os.getgid()}). sqlite said {exc}. Make the "
                "vault dir and agent.db writable by the web's uid or the jobs group."
            ) from exc
        finally:
            con.close()

    def credential(self, provider: str) -> VaultCredential | None:
        """The newest oauth credential for ``provider``, or None."""
        with self._lock:
            with self._session() as con:
                row = con.execute(
                    "SELECT id, provider, data, updated_at FROM auth_credentials "
                    "WHERE provider = ? AND credential_type = 'oauth' "
                    "ORDER BY updated_at DESC, id DESC LIMIT 1",
                    (provider,),
                ).fetchone()
        if row is None:
            return None
        credential_id, provider, data_text, updated_at = row
        data = json.loads(data_text)
        return VaultCredential(
            credential_id=credential_id,
            provider=provider,
            access=str(data.get("access") or ""),
            refresh=str(data.get("refresh") or ""),
            expires_ms=_as_ms(data.get("expires")),
            data=data,
            updated_at=updated_at,
        )

    def write_data(self, credential_id: int, data: dict) -> None:
        """Write a refreshed credential row back, preserving the other fields.
        The row's UPDATE bumps ``auth_change_revision``, which host omp watches."""
        now_s = int(time.time())
        with self._lock:
            with self._session() as con:
                con.execute(
                    "UPDATE auth_credentials SET data = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(data), now_s, credential_id),
                )
                con.commit()

    def claim_lease(self, credential_id: int) -> bool:
        """Claim the refresh lease. False when another owner holds an unexpired
        lease, so the web and a running host omp never refresh at once."""
        now_ms = _now_ms()
        with self._lock:
            with self._session() as con:
                con.execute(
                    "INSERT INTO auth_credential_refresh_leases "
                    "(credential_id, owner, expires_at_ms, updated_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(credential_id) DO UPDATE SET "
                    "owner = excluded.owner, expires_at_ms = excluded.expires_at_ms, "
                    "updated_at = excluded.updated_at "
                    "WHERE auth_credential_refresh_leases.expires_at_ms < ? "
                    "OR auth_credential_refresh_leases.owner = ?",
                    (
                        credential_id,
                        _LEASE_OWNER,
                        now_ms + _LEASE_TTL_MS,
                        now_ms,
                        now_ms,
                        _LEASE_OWNER,
                    ),
                )
                con.commit()
        return self.lease_holder(credential_id) == _LEASE_OWNER

    def lease_holder(self, credential_id: int) -> str | None:
        """The owner of an unexpired refresh lease, or None."""
        with self._lock:
            with self._session() as con:
                row = con.execute(
                    "SELECT owner, expires_at_ms FROM auth_credential_refresh_leases "
                    "WHERE credential_id = ?",
                    (credential_id,),
                ).fetchone()
        if row is None or row[1] < _now_ms():
            return None
        return str(row[0])

    def release_lease(self, credential_id: int) -> None:
        with self._lock:
            with self._session() as con:
                con.execute(
                    "DELETE FROM auth_credential_refresh_leases "
                    "WHERE credential_id = ? AND owner = ?",
                    (credential_id, _LEASE_OWNER),
                )
                con.commit()

    def block(self, credential_id: int, provider_key: str) -> None:
        """Block a credential whose refresh grant is rejected, so the broker
        stops hammering a dead login until the operator re-authenticates."""
        now_ms = _now_ms()
        with self._lock:
            with self._session() as con:
                con.execute(
                    "INSERT INTO auth_credential_blocks "
                    "(credential_id, provider_key, block_scope, blocked_until_ms, updated_at) "
                    "VALUES (?, ?, 'refresh_failed', ?, ?) "
                    "ON CONFLICT(credential_id, provider_key, block_scope) DO UPDATE SET "
                    "blocked_until_ms = excluded.blocked_until_ms, "
                    "updated_at = excluded.updated_at",
                    (credential_id, provider_key, now_ms + _BLOCK_TTL_MS, now_ms),
                )
                con.commit()

    def is_blocked(self, credential_id: int) -> bool:
        with self._lock:
            with self._session() as con:
                row = con.execute(
                    "SELECT 1 FROM auth_credential_blocks "
                    "WHERE credential_id = ? AND blocked_until_ms > ? LIMIT 1",
                    (credential_id, _now_ms()),
                ).fetchone()
        return row is not None

    def signing_for(
        self,
        provider: str,
        *,
        token_endpoint: str | None = None,
        client_id: str | None = None,
    ) -> "_VaultSigning":
        """A signing for one vault provider of this vault, pointing its refresh
        grant at ``token_endpoint`` when given (the tests stand the provider's
        token authority in locally)."""
        signing_cls = _SIGNING_CLASSES.get(provider)
        if signing_cls is None:
            raise ValueError(f"unknown vault provider {provider!r}")
        return signing_cls(self, token_endpoint=token_endpoint, client_id=client_id)


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _as_ms(value: object) -> int | None:
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    return None


class _VaultSigning(UpstreamSigning):
    """Signs the forwarded requests of one vault provider with the vault's
    current access token.

    The token is refreshed through the provider's refresh grant once it
    leaves the fresh window: single-flight under a per-signing lock, so a
    concurrent request waits on the in-flight grant and then reads the fresh
    token; the refreshed row is written back for the host's omp. A rejected
    grant blocks the credential; a transient failure serves the stale token
    and retries on the backoff.
    """

    #: The vault's own provider name.
    provider: str
    #: The upstream host the proxy forwards to, unless ``upstream_url`` says
    #: otherwise.
    base_url: str

    def __init__(
        self,
        vault: OmpVault,
        *,
        token_endpoint: str | None = None,
        client_id: str | None = None,
        retry_after: timedelta = _RETRY_AFTER,
    ) -> None:
        self._vault = vault
        self._token_endpoint = token_endpoint
        self._client_id = client_id
        self._retry_after = retry_after
        self._lock = threading.Lock()
        self._next_retry_at: datetime | None = None

    def credential_headers(
        self, *, method: str, path: str, query: str, body: bytes
    ) -> dict[str, str]:
        return {"authorization": f"Bearer {self._access_token()}"}

    def _access_token(self) -> str:
        with self._lock:
            now = datetime.now(UTC)
            credential = self._vault.credential(self.provider)
            if credential is None or not credential.access:
                raise VaultCredentialError(
                    f"the omp vault holds no usable {self.provider} login; "
                    "run `omp login` on the host"
                )
            if self._vault.is_blocked(credential.credential_id):
                raise VaultCredentialError(
                    f"the {self.provider} login is blocked after a rejected refresh; "
                    "re-authenticate on the host"
                )
            if self._is_stale(credential) and (
                self._next_retry_at is None or now >= self._next_retry_at
            ):
                self._refresh(credential)
                if self._vault.is_blocked(credential.credential_id):
                    # A rejected grant is dead, not transient: serving the stale
                    # token would only 401 upstream and loop the harness.
                    raise VaultCredentialError(
                        f"the {self.provider} login is blocked after a rejected refresh; "
                        "re-authenticate on the host"
                    )
                credential = self._vault.credential(self.provider)
                if credential is None or not credential.access:
                    raise VaultCredentialError(
                        f"the {self.provider} login is missing after a refresh; "
                        "re-authenticate on the host"
                    )
            return credential.access

    def _is_stale(self, credential: VaultCredential) -> bool:
        if credential.expires_ms is None:
            return False
        return credential.expires_ms <= _now_ms() + int(_STALE_WINDOW.total_seconds() * 1000)

    def _backoff(self, reason: str) -> None:
        logger.warning(
            "omp vault %s refresh failed (%s); serving the stale token and retrying on the backoff",
            self.provider,
            reason,
        )
        self._next_retry_at = datetime.now(UTC) + self._retry_after

    def _refresh(self, credential: VaultCredential) -> None:
        if not self._vault.claim_lease(credential.credential_id):
            # A host omp is refreshing this credential; serve the stale token
            # and let the next request read whatever it wrote back.
            return
        try:
            self._grant(credential)
        finally:
            self._vault.release_lease(credential.credential_id)

    def _grant(self, credential: VaultCredential) -> None:
        """One refresh grant: write the fresh row back, block, or back off."""

    def _write_back(
        self,
        credential: VaultCredential,
        *,
        access: str,
        refresh: str | None,
        expires_ms: int | None,
    ) -> None:
        data = dict(credential.data)
        data["access"] = access
        if refresh:
            data["refresh"] = refresh
        if expires_ms is not None:
            data["expires"] = expires_ms
        self._vault.write_data(credential.credential_id, data)
        self._next_retry_at = None


class AnthropicVaultSigning(_VaultSigning):
    """The vault's ``anthropic`` login: its OAuth bearer at ``api.anthropic.com``."""

    provider = "anthropic"
    base_url = _ANTHROPIC_BASE_URL

    def _grant(self, credential: VaultCredential) -> None:
        if not credential.refresh:
            self._vault.block(credential.credential_id, f"{self.provider}:oauth")
            return
        grant = {
            "grant_type": "refresh_token",
            "client_id": self._client_id or _ANTHROPIC_CLIENT_ID,
            "refresh_token": credential.refresh,
        }
        try:
            response = requests.post(
                self._token_endpoint or _ANTHROPIC_TOKEN_ENDPOINT,
                json=grant,
                headers={_ANTHROPIC_BETA_HEADER: _ANTHROPIC_BETA_VALUE},
                timeout=_GRANT_TIMEOUT,
            )
        except requests.RequestException as exc:
            self._backoff(str(exc))
            return
        if 400 <= response.status_code < 500:
            logger.warning(
                "omp vault %s refresh grant rejected (HTTP %d); blocking the credential",
                self.provider,
                response.status_code,
            )
            self._vault.block(credential.credential_id, f"{self.provider}:oauth")
            return
        if not 200 <= response.status_code < 300:
            self._backoff(f"HTTP {response.status_code}")
            return
        payload = response.json()
        access = payload.get("access_token")
        if not isinstance(access, str) or not access:
            self._backoff("the response carried no access token")
            return
        rotated = payload.get("refresh_token")
        expires_at = payload.get("expires_at")  # unix seconds
        self._write_back(
            credential,
            access=access,
            refresh=rotated if isinstance(rotated, str) and rotated else None,
            expires_ms=int(expires_at * 1000) if isinstance(expires_at, (int, float)) else None,
        )


class CopilotVaultSigning(_VaultSigning):
    """The vault's ``github-copilot`` login: its device token at the login's
    ``apiEndpoint``. The device token carries no refresh grant, so an expired
    one is dead and blocked."""

    provider = "github-copilot"
    base_url = _COPILOT_DEFAULT_BASE_URL

    def upstream_url(self, *, base_url: str, path: str, query: str) -> str:
        # The login's apiEndpoint beats the default (an enterprise host).
        credential = self._vault.credential(self.provider)
        endpoint = (credential.data.get("apiEndpoint") if credential else None) or base_url
        url = f"{endpoint}{path}"
        return f"{url}?{query}" if query else url

    def _grant(self, credential: VaultCredential) -> None:
        self._vault.block(credential.credential_id, f"{self.provider}:oauth")


class CodexVaultSigning(_VaultSigning):
    """The vault's ``openai-codex`` login: its ChatGPT bearer at the codex
    backend, refreshed through the shared ChatGPT grant."""

    provider = "openai-codex"
    base_url = _CODEX_BASE_URL

    def _grant(self, credential: VaultCredential) -> None:
        if not credential.refresh:
            self._vault.block(credential.credential_id, f"{self.provider}:oauth")
            return
        try:
            tokens = refresh_chatgpt_tokens(
                credential.refresh,
                token_endpoint=self._token_endpoint or _CODEX_TOKEN_ENDPOINT,
                client_id=self._client_id or _CODEX_CLIENT_ID,
                # The omp binary sends the vault's grant form-urlencoded.
                encoding="form",
            )
        except ChatgptRefreshRejectedError:
            logger.warning(
                "omp vault %s refresh grant rejected; blocking the credential",
                self.provider,
            )
            self._vault.block(credential.credential_id, f"{self.provider}:oauth")
            return
        except ChatgptRefreshTransientError as exc:
            self._backoff(str(exc))
            return
        self._write_back(
            credential,
            access=tokens.access_token,
            refresh=tokens.refresh_token,
            expires_ms=int(tokens.expires_at.timestamp() * 1000)
            if tokens.expires_at is not None
            else None,
        )


_SIGNING_CLASSES: dict[str, type[_VaultSigning]] = {
    "anthropic": AnthropicVaultSigning,
    "github-copilot": CopilotVaultSigning,
    "openai-codex": CodexVaultSigning,
}

#: The web-side signing per (vault dir, provider), cached for the process's
#: life like the Vertex ADC signing, so the refreshed tokens are shared by
#: every request.
_signings: dict[tuple[str, str], _VaultSigning] = {}
_signings_lock = threading.Lock()


def vault_signing_for(vault_dir: str | Path, provider: str) -> _VaultSigning:
    """The web-side signing for the vault's ``provider``, built once per
    process, vault dir and provider."""
    signing_cls = _SIGNING_CLASSES.get(provider)
    if signing_cls is None:
        raise ValueError(f"unknown vault provider {provider!r}")
    key = (str(Path(vault_dir).expanduser().resolve()), provider)
    with _signings_lock:
        signing = _signings.get(key)
        if signing is None:
            signing = signing_cls(OmpVault(Path(vault_dir)))
            _signings[key] = signing
        return signing


def vault_llm_upstream(vault_dir: str | Path, provider: str) -> LlmUpstream | None:
    """The proxy upstream for the vault's ``provider``, or None when the vault
    holds no usable credential for it, which the proxy then refuses."""
    if provider not in _SIGNING_CLASSES:
        raise ValueError(f"unknown vault provider {provider!r}")
    vault = OmpVault(Path(vault_dir).expanduser())
    credential = vault.credential(provider)
    if credential is None or not credential.access:
        return None
    if provider == "anthropic":
        base_url = _ANTHROPIC_BASE_URL
    elif provider == "openai-codex":
        base_url = _CODEX_BASE_URL
    else:
        base_url = str(credential.data.get("apiEndpoint") or _COPILOT_DEFAULT_BASE_URL)
    return LlmUpstream(base_url, vault_signing_for(vault_dir, provider))


def vault_writability_warnings(vault_dir: Path) -> list[str]:
    """Warnings for a vault the web cannot refresh.

    The web rewrites ``agent.db`` and its ``-wal``/``-shm`` sidecars on a
    refresh, which needs the vault dir and the database writable by the web's
    uid. Each warning names the host command that fixes its case.
    """
    if not vault_dir.is_dir():
        return [
            f"the omp vault at {vault_dir} does not exist. Point OMP_AUTH_DIR at the "
            "omp agent dir of a host `omp login`"
        ]
    uid_gid = f"uid {os.getuid()}, gid {os.getgid()}"
    warnings: list[str] = []
    if not os.access(vault_dir, os.W_OK):
        warnings.append(
            f"the omp vault dir {vault_dir} is not writable by the web ({uid_gid}), so the "
            f"token refresh cannot persist. chgrp 1001 {vault_dir} && chmod 2770 {vault_dir}"
        )
    db = vault_dir / "agent.db"
    if db.exists() and not os.access(db, os.W_OK):
        warnings.append(
            f"the omp vault database {db} is not writable by the web ({uid_gid}), so the "
            f"token refresh cannot persist. chgrp 1001 {db} && chmod 0660 {db}"
        )
    return warnings
