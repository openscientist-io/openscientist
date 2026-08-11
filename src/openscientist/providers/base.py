"""Provider base hierarchy (two-axis Provider x Agent model).

A `Provider` is a model-hosting service (Anthropic, Vertex, Bedrock,
OpenAI, ...). It owns cross-family concerns: configuration validation
and cost/budget tracking. The `ClaudeCompatible` and `CodexCompatible`
subclasses add the wire-format-specific methods that let a provider be
driven by the Claude Code agent or the Codex agent respectively.
"""

from __future__ import annotations

import abc
import enum
import inspect
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar, NotRequired, TypedDict

from openscientist.exceptions import ProviderError
from openscientist.models import ModelProfile, default_model_profile, probed_model_profile
from openscientist.settings import ProviderSettings, get_settings

logger = logging.getLogger(__name__)


def env_from_pairs(pairs: list[tuple[str, str | None]]) -> dict[str, str]:
    """Build an env dict from (name, value) pairs, dropping empty values."""
    return {key: value for key, value in pairs if value}


def self_hosted_codex_provider_table(
    *, provider_id: str, name: str, base_url: str, keyed: bool
) -> list[str]:
    """The ``[model_providers.<id>]`` TOML lines for a self-hosted server.

    Callers pass their own ``codex_model_provider_id()`` and ``display_name``
    so the table can never drift from the identity codex is told to select.
    ``keyed`` is true when a credential is in play, either the provider's own
    API key or the proxy placeholder, and drives ``requires_openai_auth``.
    """
    lines = [
        f"[model_providers.{provider_id}]",
        f'name = "{name}"',
        f'base_url = "{base_url}"',
        # The shipped codex fork accepts no other wire_api: its WireApi enum
        # has a single Responses variant and rejects "chat" outright.
        'wire_api = "responses"',
        f"requires_openai_auth = {'true' if keyed else 'false'}",
    ]
    if keyed:
        lines.append('env_key = "OPENAI_API_KEY"')
    # A self-hosted model can stay silent for minutes during prefill before the
    # first SSE token, tripping codex's default 5-minute idle timeout. Raise it
    # to 1 hour, with a few reconnects as insurance.
    lines += ["stream_idle_timeout_ms = 3600000", "stream_max_retries = 5"]
    return lines


class OmpModelEntry(TypedDict):
    """One model row in an omp ``models.yml`` provider block."""

    id: str
    name: str
    contextWindow: int
    maxTokens: int
    reasoning: bool
    input: list[str]


class OmpProviderEntry(TypedDict):
    """One provider block in an omp ``models.yml``."""

    baseUrl: str
    auth: str
    api: str
    models: list[OmpModelEntry]
    apiKey: NotRequired[str]


class OmpModelCatalog(TypedDict):
    """The whole ``models.yml`` document."""

    providers: dict[str, OmpProviderEntry]


def self_hosted_omp_model_catalog(
    *,
    provider_id: str,
    name: str,
    base_url: str,
    model_id: str,
    context_window: int,
    api_key: str | None,
    max_output_tokens: int = 32768,
) -> OmpModelCatalog:
    """An omp ``models.yml`` declaring a self-hosted model.

    omp resolves ``--model`` against its own catalog, which ships hosted APIs
    only, so a self-hosted server's model cannot be selected until it is
    declared. This is the omp analog of ``self_hosted_codex_provider_table``.
    ``auth`` accepts only apiKey, none or oauth.
    """
    entry: OmpProviderEntry = {
        "baseUrl": base_url,
        "auth": "apiKey" if api_key else "none",
        "api": "openai-completions",
        "models": [
            {
                "id": model_id,
                "name": name,
                "contextWindow": context_window,
                "maxTokens": max_output_tokens,
                "reasoning": True,
                "input": ["text"],
            }
        ],
    }
    if api_key:
        entry["apiKey"] = api_key
    return {"providers": {provider_id: entry}}


@dataclass
class CostInfo:
    """Provider-agnostic cost information."""

    provider_name: str

    # Total project spending (all time)
    # None = unknown/unavailable (e.g., permissions error)
    total_spend_usd: float | None

    # Recent spending (configurable time window)
    # None = unknown/unavailable (e.g., permissions error)
    recent_spend_usd: float | None
    recent_period_hours: int  # e.g., 24 for "last 24h"

    # Budget tracking (optional - provider-specific)
    budget_limit_usd: float | None = None
    budget_remaining_usd: float | None = None

    # Data freshness
    last_updated: datetime = field(default_factory=lambda: datetime.now(UTC))
    data_lag_note: str | None = None  # e.g., "Data current as of 6:35 AM ET"

    # Provider-specific extras
    key_expires: str | None = None  # CBORG only
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LlmUpstream:
    """Real endpoint and auth headers the LLM proxy forwards to."""

    base_url: str
    auth_headers: dict[str, str]


class AirgapEgress(enum.Enum):
    """How the job container reaches the LLM under air-gap, deciding the firewall allowlist."""

    PROXY = "proxy"
    DIRECT = "direct"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class AirgapPosture:
    """A provider's air-gapped egress posture for the active configuration."""

    mode: AirgapEgress
    direct_endpoints: tuple[tuple[str, int], ...] = ()
    reason: str = ""


# Injected into the job container so codex's in-container config.toml points its
# base_url at the proxy (codex has no base-URL env var, unlike the Claude CLI).
LLM_PROXY_URL_ENV = "OPENSCIENTIST_LLM_PROXY_URL"


class Provider(abc.ABC):
    """A model-hosting service. Family-specific behavior lives on the
    marker subclasses below; configuration validation and cost/budget
    tracking are shared here."""

    def __init__(self) -> None:
        errors = self.validate_required_config()
        if errors:
            raise ValueError(
                f"{self.display_name} provider configuration errors:\n"
                + "\n".join(f"  - {err}" for err in errors)
            )

        warnings = self._validate_optional_config()
        if warnings:
            logger.warning(
                "%s provider configuration warnings:\n%s",
                self.display_name,
                "\n".join(f"  - {warn}" for warn in warnings),
            )

    @property
    @abc.abstractmethod
    def id(self) -> str:
        """Stable identifier used by the factory selector."""

    #: Human-facing provider name for the UI and logs. Concrete providers set
    #: it as a class attribute, so it is readable without instantiating (which
    #: would validate credentials). Enforced in ``__init_subclass__``.
    display_name: ClassVar[str]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if inspect.isabstract(cls):
            return
        if not getattr(cls, "display_name", None) or not isinstance(cls.display_name, str):
            raise TypeError(
                f"{cls.__name__} must set `display_name: ClassVar[str]` "
                "to the provider's human-facing name."
            )

    @abc.abstractmethod
    def validate_required_config(self) -> list[str]:
        """Config errors raised at construction. Forwards to ``required_config_errors``."""

    @classmethod
    def required_config_errors(cls, provider: ProviderSettings) -> list[str]:
        """Config errors for a settings snapshot, without instantiating. Base: none."""
        return []

    def _validate_optional_config(self) -> list[str]:
        """Return warning messages for optional misconfiguration (empty by
        default)."""
        return []

    @abc.abstractmethod
    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        """Project spending information."""

    def check_budget_limits(self, lookback_hours: int = 24) -> dict[str, Any]:
        """Whether the project is within budget, with warnings and errors."""
        try:
            cost_info = self.get_cost_info(lookback_hours=lookback_hours)
        except (ProviderError, ValueError, OSError) as e:
            logger.error("Could not fetch cost info for budget check: %s", e)
            return {
                "can_proceed": True,
                "warnings": [f"Could not check budget limits: {e}"],
                "errors": [],
            }

        return self.evaluate_budget(cost_info)

    def evaluate_budget(self, cost_info: CostInfo) -> dict[str, Any]:
        """Evaluate budget against a pre-fetched CostInfo (avoids a duplicate API call)."""
        warnings = []
        errors = []

        if cost_info.total_spend_usd is None or cost_info.recent_spend_usd is None:
            warnings.append(
                f"Cost data unavailable for budget check. "
                f"Reason: {cost_info.data_lag_note or 'Unknown'}"
            )
        else:
            settings = get_settings()
            max_total = settings.budget.max_project_spend_total_usd
            if cost_info.total_spend_usd >= max_total:
                errors.append(
                    f"Total spend ${cost_info.total_spend_usd:.2f} exceeds limit ${max_total:.2f}"
                )

            max_recent = settings.budget.max_project_spend_24h_usd
            if cost_info.recent_spend_usd >= max_recent:
                errors.append(
                    f"Last {cost_info.recent_period_hours}h spend "
                    f"${cost_info.recent_spend_usd:.2f} "
                    f"exceeds limit ${max_recent:.2f}"
                )

            warn_recent = settings.budget.warn_project_spend_24h_usd
            if (
                cost_info.recent_spend_usd >= warn_recent
                and cost_info.recent_spend_usd < max_recent
            ):
                warnings.append(
                    f"Last {cost_info.recent_period_hours}h spend "
                    f"${cost_info.recent_spend_usd:.2f} "
                    f"approaching limit (warning threshold: ${warn_recent:.2f})"
                )

        # Provider-specific budget (e.g., CBORG max_budget)
        if cost_info.budget_remaining_usd is not None:
            if cost_info.budget_remaining_usd <= 0:
                errors.append(
                    f"{self.display_name} budget exhausted "
                    f"(${cost_info.budget_limit_usd or 0:.2f} limit)"
                )
            elif cost_info.budget_remaining_usd < 10:
                warnings.append(
                    f"{self.display_name} budget low: "
                    f"${cost_info.budget_remaining_usd:.2f} remaining"
                )

        return {"can_proceed": len(errors) == 0, "warnings": warnings, "errors": errors}

    def effective_model_name(self) -> str | None:
        """Model id this provider drives, or None when it defers to an account default."""
        return None

    def model_profile(self) -> ModelProfile:
        """The active model's profile (mainly its context window). Self-hosted
        providers override to probe the live deployment."""
        return default_model_profile(
            self.effective_model_name(), get_settings().provider.model_context_tokens
        )

    def probe_context_window(self) -> int | None:
        """Launched context window from a direct probe, or None (hosted APIs do not probe)."""
        return None

    def prelaunch_model_context_env(self) -> dict[str, str]:
        """Window resolved app-side and injected as ``OPENSCIENTIST_MODEL_CONTEXT_TOKENS``,
        because the proxied container cannot probe a root path like llama.cpp's ``/props``.
        Empty only when nothing is pinned and the provider does not probe.
        """
        pinned = get_settings().provider.model_context_tokens
        if pinned is not None:
            # Pass the pin on rather than skip it. It is set in this process's
            # environment, but the container resolves the window again on its own
            # side, so returning nothing here left it probing -- the very thing
            # pinning exists to avoid -- and silently taking the 8192 fallback.
            return {"OPENSCIENTIST_MODEL_CONTEXT_TOKENS": str(pinned)}
        window = self.probe_context_window()
        if not window:
            return {}
        return {"OPENSCIENTIST_MODEL_CONTEXT_TOKENS": str(window)}

    def llm_upstream(self) -> LlmUpstream | None:
        """Real endpoint and auth headers for the proxy, or None if not proxied."""
        return None

    def proxy_env_overrides(self, *, proxy_base_url: str, placeholder: str) -> dict[str, str]:
        """Env that routes this provider's LLM calls through the proxy, or {}."""
        return {}

    def airgap_egress(self) -> AirgapPosture:
        """Air-gapped egress posture for the active config, read by the firewall
        and proxy-start. Pure, no network. Defaults to PROXY, providers override."""
        if self.proxy_env_overrides(proxy_base_url="", placeholder=""):
            return AirgapPosture(AirgapEgress.PROXY)
        return AirgapPosture(
            AirgapEgress.UNSUPPORTED,
            reason=f"{self.display_name} cannot be air-gapped.",
        )

    def proxied_container_env(self, *, proxy_base_url: str, placeholder: str) -> dict[str, str]:
        """Job-container provider env with LLM traffic routed through the proxy."""
        env = get_settings().provider.get_container_env_vars()
        env.update(self.proxy_env_overrides(proxy_base_url=proxy_base_url, placeholder=placeholder))
        return env

    @classmethod
    def container_env(
        cls,
        provider: ProviderSettings,
        *,
        gcp_credentials_container_path: str | None = None,
    ) -> dict[str, str]:
        """Agent-container env (auth + routing). Takes ``ProviderSettings`` so it
        composes without instantiating the provider. Base: none."""
        return {}

    @abc.abstractmethod
    def harness_env(self, *, proxy: str | None) -> dict[str, str]:
        """Env a provider-agnostic harness (omp) needs to reach this provider.

        ``proxy`` is the in-container LLM proxy URL when active, in which case
        the returned env MUST route the harness at it. Abstract on purpose: this
        used to default to ``{}``, which meant an unwired provider silently sent
        the harness to the vendor with the real credential, bypassing the
        key-replacement proxy. A provider nobody has wired must fail loudly, so
        every concrete provider answers for itself. Returning ``{}`` is still a
        valid answer for a provider that signs its own requests and is reached
        directly, but it now has to be stated rather than inherited.
        """

    def omp_model_catalog(self, *, context_window: int) -> OmpModelCatalog | None:
        """``models.yml`` declaring this provider's model to the omp harness, or
        None when omp's built-in catalog already knows it. Self-hosted providers
        override: omp cannot resolve ``--model`` for a server it has never heard
        of. The codex analog is ``codex_config_overrides``.

        ``context_window`` is passed in rather than resolved here because
        resolving it can probe the live server, and the caller already holds the
        run's cached profile.
        """
        return None

    @classmethod
    def validate_model_format(cls, model: str | None) -> str | None:
        """Error message if ``model`` does not match this provider's naming
        convention, else None. Base enforces no pattern."""
        return None

    @staticmethod
    def model_format_error(model: str | None, pattern: str, description: str) -> str | None:
        """Shared helper: None if ``model`` is unset or matches ``pattern``,
        else a uniform mismatch message naming ``description``."""
        if not model or re.match(pattern, model):
            return None
        return (
            f"OPENSCIENTIST_MODEL={model!r} does not look like {description}. "
            "Either change the model id or change OPENSCIENTIST_PROVIDER."
        )


class ClaudeCompatible(Provider, abc.ABC):
    """Provider that speaks the Anthropic Messages API and can be driven
    by the Claude Code agent."""

    @abc.abstractmethod
    def setup_environment(self) -> None:
        """Configure process environment variables for the Claude CLI
        (auth + routing flags), clearing any conflicting flags from a
        previously-selected provider."""

    @abc.abstractmethod
    def claude_sdk_env(self) -> dict[str, str]:
        """Environment variables the claude-agent-sdk CLI must see
        (e.g., ANTHROPIC_API_KEY, ANTHROPIC_BASE_URL, AWS_REGION)."""

    @abc.abstractmethod
    def claude_model_name(self) -> str:
        """Model name to pass to ClaudeAgentOptions.model."""

    def effective_model_name(self) -> str | None:
        return self.claude_model_name()


class OpenAiWireCompatible(Provider, abc.ABC):
    """Provider reachable over the OpenAI wire, drivable by a generic harness.

    Speaking this wire does not make a provider a Codex backend. Codex needs the
    extra contract in ``CodexCompatible`` and, in practice, tolerant handling of
    non-gptoss models. A self-hosted server that omp drives happily belongs here
    rather than there.
    """

    @abc.abstractmethod
    def effective_model_name(self) -> str | None:
        """The model id sent to the server, or None to let the harness decide."""

    def harness_env(self, *, proxy: str | None) -> dict[str, str]:
        # OpenAI-family harnesses read OPENAI_BASE_URL; point it at the proxy
        # when active (codex uses config.toml instead, so this is omp's path).
        return {"OPENAI_BASE_URL": proxy} if proxy else {}


class SelfHostedOpenAiWire(OpenAiWireCompatible, abc.ABC):
    """Shared plumbing for a self-hosted OpenAI-wire server driven by omp (vLLM, llama.cpp).

    A subclass supplies its identity, its two settings fields, and the context probe.
    """

    server_name: ClassVar[str]
    base_url_env: ClassVar[str]
    api_key_env: ClassVar[str]

    @classmethod
    @abc.abstractmethod
    def _base_url_of(cls, provider: ProviderSettings) -> str:
        """The configured server base URL."""

    @classmethod
    @abc.abstractmethod
    def _api_key_of(cls, provider: ProviderSettings) -> str | None:
        """The configured API key, or None for a keyless server."""

    @staticmethod
    @abc.abstractmethod
    def _probe_context_tokens(base_url: str, model_id: str, api_key: str | None) -> int | None:
        """Read the launched context window from the live server, or None."""

    def _base_url(self) -> str:
        return self._base_url_of(get_settings().provider)

    def _api_key(self) -> str | None:
        return self._api_key_of(get_settings().provider)

    @classmethod
    def container_env(
        cls, provider: ProviderSettings, *, gcp_credentials_container_path: str | None = None
    ) -> dict[str, str]:
        return env_from_pairs(
            [
                (cls.base_url_env, cls._base_url_of(provider)),
                (cls.api_key_env, cls._api_key_of(provider)),
            ]
        )

    def harness_env(self, *, proxy: str | None) -> dict[str, str]:
        if proxy:
            return super().harness_env(proxy=proxy)
        # OpenAI clients require a non-empty key, so a keyless server gets a dummy.
        return {"OPENAI_BASE_URL": self._base_url(), "OPENAI_API_KEY": self._api_key() or self.id}

    def validate_required_config(self) -> list[str]:
        return self.required_config_errors(get_settings().provider)

    @classmethod
    def required_config_errors(cls, provider: ProviderSettings) -> list[str]:
        # One server serves one model, so it must be named.
        if provider.model:
            return []
        return [f"OPENSCIENTIST_MODEL must name the model the {cls.server_name} server serves."]

    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        # No per-call cost, so report zero spend and keep budget checks quiet.
        return CostInfo(
            provider_name=self.display_name,
            total_spend_usd=0.0,
            recent_spend_usd=0.0,
            recent_period_hours=lookback_hours,
            data_lag_note=f"Self-hosted {self.server_name} inference incurs no API cost.",
        )

    def llm_upstream(self) -> LlmUpstream | None:
        key = self._api_key()
        headers = {"authorization": f"Bearer {key}"} if key else {}
        return LlmUpstream(self._base_url(), headers)

    def proxy_env_overrides(self, *, proxy_base_url: str, placeholder: str) -> dict[str, str]:
        # omp reads OPENAI_API_KEY, which the proxy swaps for the real key.
        env = {"OPENAI_API_KEY": placeholder, LLM_PROXY_URL_ENV: proxy_base_url}
        if self._api_key():
            # Never ship the real key into the container.
            env[self.api_key_env] = placeholder
        return env

    def _endpoint(self) -> tuple[str, str | None]:
        """Base URL and key to reach the server: the proxy and placeholder when proxied, else the real values."""
        proxy = os.environ.get(LLM_PROXY_URL_ENV)
        if proxy:
            return proxy, os.environ.get("OPENAI_API_KEY")
        return self._base_url(), self._api_key()

    def effective_model_name(self) -> str | None:
        return get_settings().provider.model or None

    def probe_context_window(self) -> int | None:
        # Probe the real server directly, since the proxy cannot forward a root /props.
        model = self.effective_model_name()
        if not model:
            return None
        return self._probe_context_tokens(self._base_url(), model, self._api_key())

    def model_profile(self) -> ModelProfile:
        # Probe the live window, not the trained maximum. Normally the launcher
        # resolves it app-side (override below), so this proxied probe is a fallback.
        base_url, key = self._endpoint()
        return probed_model_profile(
            model_id=self.effective_model_name(),
            override=get_settings().provider.model_context_tokens,
            probe=lambda mid: self._probe_context_tokens(base_url, mid, key),
            server_name=self.server_name,
            provider_logger=logging.getLogger(type(self).__module__),
        )

    def omp_model_catalog(self, *, context_window: int) -> OmpModelCatalog | None:
        model_id = self.effective_model_name()
        if not model_id:
            return None
        base_url, key = self._endpoint()
        return self_hosted_omp_model_catalog(
            provider_id=self.id,
            name=self.display_name,
            base_url=base_url,
            model_id=model_id,
            context_window=context_window,
            api_key=key,
        )


class CodexCompatible(OpenAiWireCompatible, abc.ABC):
    """Provider that speaks the OpenAI Responses API and can be driven by
    the Codex agent."""

    @abc.abstractmethod
    def codex_config_overrides(self) -> list[str]:
        """TOML lines for the per-job ``$CODEX_HOME/config.toml``, typically a
        ``[model_providers.<id>]`` table. The SDK has no programmatic override."""

    @abc.abstractmethod
    def codex_model_name(self) -> str | None:
        """Model for ``thread_start(model=...)``, or None to use codex's account
        default (some accounts reject an explicit id, e.g. ChatGPT-auth)."""

    @abc.abstractmethod
    def codex_model_provider_id(self) -> str:
        """The ``model_providers.<id>`` key, written as the top-level
        ``model_provider = "<id>"`` in config.toml to select this
        provider."""

    @abc.abstractmethod
    def codex_sdk_env(self) -> dict[str, str]:
        """Auth env for the codex child, at minimum the secret named by this
        provider's ``model_providers.<id>.env_key``. Codex analog of ``claude_sdk_env``."""

    def effective_model_name(self) -> str | None:
        return self.codex_model_name()
