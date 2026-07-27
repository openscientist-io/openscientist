"""Ollama provider that drives the Claude agent (Anthropic-compatible route).

Ollama serves the Anthropic Messages API at ``/v1/messages`` alongside its
OpenAI-compatible routes, with tool use and the full streaming event set, so
the Claude Code harness can drive a local open-weight model (e.g.
``qwen3.6:35b-a3b``) with no translation layer in between. The sibling
`ollama` provider drives codex against the same server; which agent family
runs is chosen entirely by which provider id is configured.

Base-URL shape differs from the codex path and is easy to get wrong: the
Anthropic SDK appends ``/v1/messages`` to the base URL it is given, while
``OLLAMA_BASE_URL`` names the OpenAI-compatible endpoint that already ends in
``/v1``. This provider therefore hands out the Ollama HTTP *root*; passing the
``/v1`` form through would produce ``/v1/v1/messages``.

Air-gapped runs need no special handling. The provider reports an upstream, so
the default PROXY posture applies: the job container is firewalled down to the
web-side LLM proxy and never learns the Ollama address, and the proxy forwards
``/v1/messages`` verbatim like any other path.
"""

from __future__ import annotations

import logging
import os

from openscientist.models import ModelProfile
from openscientist.providers.base import ClaudeCompatible, CostInfo, LlmUpstream
from openscientist.settings import get_settings

from ._env_cleanup import clear_env_vars, clear_provider_mode_flags
from ._ollama_common import ollama_cost_info, ollama_http_base, ollama_model_profile

logger = logging.getLogger(__name__)

# Ollama ignores the credential, but the Claude CLI refuses to start without
# one. Under the LLM proxy this is replaced by the proxy's placeholder, which
# is what actually authenticates the container.
KEYLESS_PLACEHOLDER = "ollama-local"

# A reasoning model (qwen3.6, gpt-oss, ...) emits Anthropic `thinking` blocks by
# default, but Ollama omits the `signature` field that real extended thinking
# carries and that the Claude SDK's parser requires -- every turn dies with
# "Missing required field in assistant message: 'signature'". The CLI never
# sends a `thinking` field of its own, so the proxy adds this to disable it,
# which makes Ollama return a plain text block instead.
DISABLE_THINKING: dict[str, object] = {"thinking": {"type": "disabled"}}


class OllamaClaudeProvider(ClaudeCompatible):
    """Local Ollama server as a Claude agent backend (open-weight models)."""

    @property
    def id(self) -> str:
        return "ollama-claude"

    @property
    def display_name(self) -> str:
        return "Ollama (local, Claude agent)"

    def validate_required_config(self) -> list[str]:
        # Local and keyless: the base URL and model both have defaults, so
        # there is nothing the operator must supply for the provider to
        # construct. Reachability is a runtime concern, surfaced by the run.
        return []

    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        return ollama_cost_info(self.display_name, lookback_hours)

    def _anthropic_base_url(self) -> str:
        """The Anthropic-API base URL the Claude CLI should talk to.

        ``ANTHROPIC_BASE_URL`` wins when set, which is how a proxied run
        redirects the CLI: `proxy_env_overrides` puts the proxy URL in the job
        container's environment, settings read it back there, and this returns
        it instead of reaching for Ollama directly.
        """
        p = get_settings().provider
        return p.anthropic_base_url or ollama_http_base(p.ollama_base_url)

    def claude_sdk_env(self) -> dict[str, str]:
        """Auth env vars the claude-agent-sdk CLI must see."""
        p = get_settings().provider
        env = {
            "ANTHROPIC_BASE_URL": self._anthropic_base_url(),
            "ANTHROPIC_API_KEY": p.anthropic_api_key or KEYLESS_PLACEHOLDER,
        }
        # The CLI's default output cap is sized for a frontier model's window
        # and exceeds a typical self-hosted one outright, so a talkative local
        # model overruns it and the turn dies with "Claude's response exceeded
        # the N output token maximum". Size it from the actual window instead.
        # Half, not a quarter: an open-weight model narrating its analysis blew
        # through a 32k/4 cap on every iteration of a two-iteration run.
        if p.claude_code_max_output_tokens:
            env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(p.claude_code_max_output_tokens)
        elif "CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in os.environ:
            window = self.model_profile().context_window_tokens
            env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max(1024, min(16384, window // 2)))
        return env

    def llm_upstream(self) -> LlmUpstream | None:
        # Keyless: forward to Ollama with no injected auth. This is resolved
        # host-side to configure the proxy target, so it reads OLLAMA_BASE_URL
        # rather than _anthropic_base_url() (which would be the proxy itself).
        # The root form matters: the proxy appends the request path, which
        # already carries /v1/messages.
        return LlmUpstream(
            ollama_http_base(get_settings().provider.ollama_base_url),
            {},
            request_overrides=DISABLE_THINKING,
        )

    def proxy_env_overrides(self, *, proxy_base_url: str, placeholder: str) -> dict[str, str]:
        # The CLI sends the placeholder so the proxy authenticates the container.
        return {"ANTHROPIC_BASE_URL": proxy_base_url, "ANTHROPIC_API_KEY": placeholder}

    def claude_model_name(self) -> str:
        """Model name for ClaudeAgentOptions.model."""
        s = get_settings().provider
        return s.model or s.ollama_model

    def model_profile(self) -> ModelProfile:
        return ollama_model_profile(self.effective_model_name() or "unknown")

    def setup_environment(self) -> None:
        """Clear routing/auth vars a previously-selected provider may have set."""
        clear_provider_mode_flags(logger)
        # A leftover CBORG token would be preferred over the API key we set.
        clear_env_vars(logger, ("ANTHROPIC_AUTH_TOKEN",))
        logger.info("Ollama (Claude agent) provider initialized")
