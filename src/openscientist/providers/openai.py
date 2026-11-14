"""Direct OpenAI API provider (drives the Codex agent).

Auth is ``OPENAI_API_KEY`` or the ChatGPT login the codex CLI stores in
``auth.json``. For the ChatGPT login the web process holds the file
(``CODEX_AUTH_HOST_PATH``): the LLM proxy signs the Responses calls upstream
with the refreshed ChatGPT access token, and the container's codex runs its
API-key mode against the proxy with a per-run placeholder.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from openscientist.providers.base import (
    LLM_PROXY_URL_ENV,
    AirgapEgress,
    AirgapPosture,
    Alternatives,
    CodexCompatible,
    CostInfo,
    HostMount,
    LiveCheckResult,
    LlmUpstream,
    SetupQuestion,
    StaticHeadersSigning,
    UpstreamCredentialError,
    UpstreamSigning,
    _listing_live_check,
    env_from_pairs,
)
from openscientist.providers.chatgpt import (
    CLIENT_ID as _CHATGPT_CLIENT_ID,
)
from openscientist.providers.chatgpt import (
    TOKEN_ENDPOINT as _CHATGPT_TOKEN_ENDPOINT,
)
from openscientist.providers.chatgpt import (
    ChatgptRefreshRejectedError,
    ChatgptRefreshTransientError,
    ChatgptTokens,
    refresh_chatgpt_tokens,
)
from openscientist.providers.vault import vault_llm_upstream, vault_writability_warnings
from openscientist.settings import ProviderSettings, get_settings

logger = logging.getLogger(__name__)

_OPENAI_BASE_URL = "https://api.openai.com/v1"
_CHATGPT_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"

#: The access token is a JWT; refresh it once it is this close to its ``exp``
#: claim (the window the codex CLI itself uses).
_STALE_WINDOW = timedelta(minutes=5)
#: When the token carries no readable ``exp``, fall back to the cadence the
#: codex CLI schedules its refreshes on.
_SCHEDULED_INTERVAL = timedelta(days=8)
_REFRESH_RETRY_AFTER = timedelta(seconds=30)


class ChatgptCredentialError(UpstreamCredentialError):
    """The web-held ChatGPT login (``auth.json``) is missing or unusable."""


@dataclass
class _ChatgptLogin:
    """The ChatGPT tokens of one generation of the web-held ``auth.json``."""

    access_token: str
    refresh_token: str
    expires_at: datetime | None
    last_refresh: datetime | None


def _jwt_expires_at(token: str) -> datetime | None:
    """The ``exp`` claim of a JWT access token, or None when it is not a
    readable JWT."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        exp = payload.get("exp")
        return datetime.fromtimestamp(exp, UTC) if isinstance(exp, (int, float)) else None
    except (ValueError, TypeError):
        return None


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _codex_auth_json() -> Path:
    """Path to the codex CLI's stored OAuth login (default codex home)."""
    return Path.home() / ".codex" / "auth.json"


class ChatgptSigning(UpstreamSigning):
    """Signs each forwarded request with the web-held ChatGPT login.

    The file is the host's codex login, mounted read-write into the web: the
    access token is refreshed through the refresh token once it leaves the
    fresh window, single-flight under a lock so concurrent requests share one
    grant. A refreshed token is written back in place so the host's own codex
    CLI stays current, and in memory for the proxy. A rejected grant blocks the credential until the login file
    changes; a transient failure serves the stale token (the upstream 401s and
    the harness retries) and retries the refresh on a backoff.
    """

    def __init__(
        self,
        auth_path: str,
        *,
        token_endpoint: str = _CHATGPT_TOKEN_ENDPOINT,
        client_id: str = _CHATGPT_CLIENT_ID,
        retry_after: timedelta = _REFRESH_RETRY_AFTER,
    ) -> None:
        self._auth_path = Path(auth_path).expanduser()
        self._token_endpoint = token_endpoint
        self._client_id = client_id
        self._retry_after = retry_after
        self._lock = threading.Lock()
        self._login: _ChatgptLogin | None = None
        self._generation: tuple[int, int] | None = None
        self._blocked = False
        self._next_retry_at: datetime | None = None

    def credential_headers(
        self, *, method: str, path: str, query: str, body: bytes
    ) -> dict[str, str]:
        return {"authorization": f"Bearer {self._access_token()}"}

    def _access_token(self) -> str:
        with self._lock:
            self._reload_if_changed()
            now = datetime.now(UTC)
            if self._login is not None and not self._blocked and self._is_stale(now):
                if self._next_retry_at is None or now >= self._next_retry_at:
                    self._refresh(now)
            if self._blocked or self._login is None:
                raise ChatgptCredentialError(
                    f"ChatGPT login at {self._auth_path} is unusable; re-authenticate on the host"
                )
            return self._login.access_token

    def _reload_if_changed(self) -> None:
        try:
            stat = self._auth_path.stat()
        except OSError:
            if self._generation is not None:
                self._generation = None
                self._login = None
                self._blocked = False
                self._next_retry_at = None
            return
        generation = (stat.st_mtime_ns, stat.st_size)
        if generation == self._generation:
            return
        self._generation = generation
        self._login = self._read_login()
        self._blocked = False
        self._next_retry_at = None

    def _read_login(self) -> _ChatgptLogin | None:
        try:
            data = json.loads(self._auth_path.read_text())
            tokens = data.get("tokens")
            if not isinstance(tokens, dict):
                return None
            access = tokens.get("access_token")
            if not isinstance(access, str) or not access:
                return None
            refresh = tokens.get("refresh_token")
            refresh = refresh if isinstance(refresh, str) else ""
        except (OSError, ValueError, AttributeError):
            return None
        return _ChatgptLogin(
            access_token=access,
            refresh_token=refresh,
            expires_at=_jwt_expires_at(access),
            last_refresh=_parse_timestamp(data.get("last_refresh")),
        )

    def _is_stale(self, now: datetime) -> bool:
        login = self._login
        if login is None:
            return False
        if login.expires_at is not None:
            return login.expires_at <= now + _STALE_WINDOW
        if login.last_refresh is not None:
            return now - login.last_refresh >= _SCHEDULED_INTERVAL
        return False

    def _refresh(self, now: datetime) -> None:
        login = self._login
        if login is None:
            return
        if not login.refresh_token:
            logger.warning("ChatGPT login at %s carries no refresh token", self._auth_path)
            self._blocked = True
            return
        try:
            tokens = refresh_chatgpt_tokens(
                login.refresh_token,
                token_endpoint=self._token_endpoint,
                client_id=self._client_id,
                # The pinned codex CLI sends the ChatGPT grant as JSON.
                encoding="json",
            )
        except ChatgptRefreshRejectedError as exc:
            logger.warning(
                "ChatGPT refresh grant rejected (%s); blocking the credential until "
                "the login file changes",
                exc,
            )
            self._blocked = True
            return
        except ChatgptRefreshTransientError as exc:
            logger.warning("ChatGPT token refresh failed (%s); serving the stale token", exc)
            self._next_retry_at = now + self._retry_after
            return
        self._login = _ChatgptLogin(
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token or login.refresh_token,
            expires_at=tokens.expires_at,
            last_refresh=now,
        )
        self._next_retry_at = None
        self._persist(tokens)

    def _persist(self, tokens: ChatgptTokens) -> None:
        """Write the refreshed login back so the host's own codex CLI stays
        current; the file is the source of truth."""
        try:
            data = json.loads(self._auth_path.read_text())
        except (OSError, ValueError):
            logger.warning(
                "Could not read back %s after the ChatGPT refresh; keeping the token in memory",
                self._auth_path,
            )
            return
        login = self._login
        if login is None:
            return
        stored = data.get("tokens")
        if not isinstance(stored, dict):
            stored = {}
            data["tokens"] = stored
        stored["access_token"] = login.access_token
        stored["refresh_token"] = login.refresh_token
        if tokens.id_token:
            stored["id_token"] = tokens.id_token
        last = login.last_refresh
        data["last_refresh"] = last.isoformat() if last is not None else None
        # The auth file is a single bind mount, so a temp file plus rename
        # cannot replace it. The write goes in place and keeps the file's
        # ownership and mode, which the operator sets for the web.
        try:
            with self._auth_path.open("r+", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
                handle.truncate()
        except OSError as exc:
            logger.warning(
                "Could not write the refreshed ChatGPT login back to %s (%s); keeping it in memory",
                self._auth_path,
                exc,
            )


#: The web-side signing per auth file, cached for the process's life like the
#: Vertex ADC signing, so the refreshed tokens are shared by every request.
_chatgpt_signings: dict[str, ChatgptSigning] = {}
_chatgpt_signings_lock = threading.Lock()


def chatgpt_signing_for(auth_path: str) -> ChatgptSigning:
    """The web-side ChatGPT signing for the login file at ``auth_path``, built
    once per process and path."""
    path = os.path.abspath(os.path.expanduser(auth_path))
    with _chatgpt_signings_lock:
        signing = _chatgpt_signings.get(path)
        if signing is None:
            signing = ChatgptSigning(path)
            _chatgpt_signings[path] = signing
        return signing


class OpenAIDirectProvider(CodexCompatible):
    """OpenAI's API as a Codex backend (Codex's default endpoint)."""

    @property
    def id(self) -> str:
        return "openai"

    display_name = "OpenAI API"
    credential_env_keys = ("OPENAI_API_KEY", "CODEX_API_KEY")

    @classmethod
    def container_env(cls, provider: ProviderSettings) -> dict[str, str]:
        return env_from_pairs([("OPENAI_API_KEY", provider.openai_api_key)])

    def validate_required_config(self) -> list[str]:
        return self.config_errors(get_settings().provider)

    def _validate_optional_config(self) -> list[str]:
        warnings = super()._validate_optional_config()
        p = get_settings().provider
        # The vault signs the LLM only when it is the active auth source.
        if p.omp_auth_host_path and not p.openai_api_key and not p.codex_auth_host_path:
            warnings.extend(vault_writability_warnings(Path(p.omp_auth_host_path).expanduser()))
        return warnings

    @classmethod
    def required_config_errors(cls, provider: ProviderSettings) -> list[str]:
        # Auth is satisfied by an API key or the web-held ChatGPT login file;
        # a host-side codex CLI login counts when the web runs on the host.
        if (
            os.environ.get("OPENAI_API_KEY")
            or provider.codex_auth_host_path
            or provider.omp_auth_host_path
            or _codex_auth_json().exists()
        ):
            return []
        return [
            "OpenAI provider needs auth: set OPENAI_API_KEY, set "
            "CODEX_AUTH_FILE to the host path of the codex auth.json (`codex login`), "
            "which compose mounts as CODEX_AUTH_HOST_PATH for the web to sign the "
            "ChatGPT calls with, or set OMP_AUTH_HOST_PATH to the omp vault of an "
            "`omp login`."
        ]

    @classmethod
    def setup_questions(cls) -> tuple[SetupQuestion, ...]:
        return (
            SetupQuestion("OPENAI_API_KEY", kind="secret"),
            SetupQuestion(
                "CODEX_AUTH_FILE",
                kind="path",
                help="or the host path of the codex auth.json from `codex login`",
                mount=HostMount("CODEX_AUTH_HOST_PATH", "/app/codex-auth.json"),
            ),
            SetupQuestion(
                "OPENSCIENTIST_MODEL", help="model id, or leave empty for the codex account default"
            ),
        )

    @classmethod
    def setup_alternatives(cls) -> Alternatives:
        return ((("OPENAI_API_KEY",), ("CODEX_AUTH_HOST_PATH",)),)

    @classmethod
    def _live_check(cls, p: ProviderSettings) -> LiveCheckResult:
        if not p.openai_api_key:
            return LiveCheckResult(
                ok=True, detail="No OpenAI API key (ChatGPT login); skipped the live check."
            )
        headers = {"authorization": f"Bearer {p.openai_api_key}"}
        return _listing_live_check(f"{_OPENAI_BASE_URL}/models", headers, cls.display_name)

    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        # OpenAI exposes no simple per-key spend endpoint, so report unavailable.
        return CostInfo.unavailable(
            self.display_name,
            "OpenAI per-key cost tracking is not available.",
            lookback_hours,
        )

    def llm_upstream(self) -> LlmUpstream | None:
        p = get_settings().provider
        key = p.openai_api_key
        if key:
            return LlmUpstream(
                _OPENAI_BASE_URL, StaticHeadersSigning({"authorization": f"Bearer {key}"})
            )
        auth_path = get_settings().provider.codex_auth_host_path
        if auth_path:
            return LlmUpstream(_CHATGPT_CODEX_BASE_URL, chatgpt_signing_for(auth_path))
        if p.omp_auth_host_path:
            return vault_llm_upstream(p.omp_auth_host_path, "openai-codex")
        return None

    def harness_routing_error(self) -> str | None:
        # The pinned omp binary pins the codex backend to chatgpt.com and ignores base-URL settings.
        p = get_settings().provider
        if not (p.codex_auth_host_path or p.omp_auth_host_path):
            return None
        if p.harness != "omp":
            return None
        return (
            "the omp harness cannot route the ChatGPT login: the pinned omp "
            "binary pins the codex backend to chatgpt.com and ignores every "
            "base-URL setting, so the container's requests would leave with a "
            "placeholder the vendor rejects. Use OPENSCIENTIST_HARNESS=codex, "
            "which routes the codex CLI through the proxy, or OPENAI_API_KEY."
        )

    def proxy_env_overrides(self, *, proxy_base_url: str, placeholder: str) -> dict[str, str]:
        # The container's codex runs its API-key mode against the proxy in both
        # auth setups; the pinned CLI reads CODEX_API_KEY, and omp's OpenAI
        # client reads OPENAI_API_KEY.
        return {
            "OPENAI_API_KEY": placeholder,
            "CODEX_API_KEY": placeholder,
            LLM_PROXY_URL_ENV: proxy_base_url,
        }

    @classmethod
    def airgap_posture(cls, provider: ProviderSettings) -> AirgapPosture:
        return AirgapPosture(AirgapEgress.PROXY)

    def codex_config_overrides(self) -> list[str]:
        # The pinned codex CLI refuses to override the built-in "openai"
        # provider entry, so the base URL rides the top-level keys it reads
        # for the built-in one (inference) and the ChatGPT auxiliary calls.
        proxy = os.environ.get(LLM_PROXY_URL_ENV)
        if not proxy:
            return []
        return [
            f'openai_base_url = "{proxy}"',
            f'chatgpt_base_url = "{proxy}"',
        ]

    def codex_model_name(self) -> str | None:
        # No forced default. codex uses its account or config default unless the
        # operator sets OPENSCIENTIST_MODEL (some accounts reject explicit ids).
        return get_settings().provider.model

    def codex_model_provider_id(self) -> str:
        return "openai"

    def codex_sdk_env(self) -> dict[str, str]:
        key = os.environ.get("CODEX_API_KEY")
        return {"CODEX_API_KEY": key} if key else {}
