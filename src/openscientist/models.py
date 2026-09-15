"""Model abstraction: intrinsic properties of an LLM, independent of who serves it.

A ``Provider`` is *who* hosts a model (Ollama, Anthropic, Vertex). An ``AbstractAgent``
is *how* we drive it. This module is the third axis: *what* model is in use and its
limits. The property that matters today is the usable context window, which is a
property of the model and its deployment, NOT of the provider. The same Ollama
server can serve a 4K-context model and a 131072-context one side by side, and the
effective window for a self-hosted model is whatever ``num_ctx`` the deployment
allocates (e.g. ``OLLAMA_CONTEXT_LENGTH``), which can be well below the model's
trained maximum.

Resolving the window is a provider concern: ``Provider.model_profile()`` owns it,
because the provider knows its model id and (for a self-hosted deployment) how to
probe the live endpoint. This module holds the value object plus both shared
resolutions: ``default_model_profile`` (explicit override, known-model table,
conservative default) for hosted APIs, and ``probed_model_profile`` (explicit
override, live probe, conservative default) for self-hosted servers.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Conservative fallback when nothing else resolves: small enough that prompt
# budgeting trims aggressively rather than risking a silent context overflow.
_DEFAULT_CONTEXT_TOKENS = 8192

# Known API models (trained windows). Self-hosted models are probed instead,
# because their deployment can cap the window below the trained maximum.
_KNOWN_CONTEXT_TOKENS: dict[str, int] = {
    # Claude 4.x
    "claude-opus-4": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-haiku-4": 200_000,
    # Claude 3.x
    "claude-3-5-sonnet": 200_000,
    "claude-3-5-haiku": 200_000,
    "claude-3-opus": 200_000,
    "claude-3-sonnet": 200_000,
    "claude-3-haiku": 200_000,
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4.1": 1_047_576,
    "gpt-4": 8_192,
    "gpt-5": 400_000,
    "o1": 200_000,
    "o3": 200_000,
    "o4-mini": 200_000,
}


@dataclass(frozen=True)
class ModelProfile:
    """Intrinsic, provider-independent properties of an LLM."""

    id: str
    context_window_tokens: int
    # Room to grow: max_output_tokens, supports_tool_use, vision, reasoning, ...


def _strip_provider_qualifiers(model_id: str) -> str:
    """Strip Bedrock/Vertex id decorations down to the bare model name.

    Examples: ``us.anthropic.claude-sonnet-4-5-20250929-v1:0`` ->
    ``claude-sonnet-4-5``; ``claude-sonnet-4-5@20250929`` -> ``claude-sonnet-4-5``.
    """
    model_id = re.sub(r"^(?:us|eu|ap)\.anthropic\.", "", model_id)
    model_id = re.sub(r"-\d{8}.*$", "", model_id)
    model_id = re.sub(r"@\d{8}$", "", model_id)
    return model_id


def _known_context_tokens(model_id: str) -> int | None:
    """Look up a known API model's window by longest-matching prefix.

    Tries the raw id first, then a Bedrock/Vertex-qualifier-stripped id, so
    provider-qualified model ids resolve to the same entry as their bare name.
    """
    for mid in (model_id, _strip_provider_qualifiers(model_id)):
        matches = [(p, ctx) for p, ctx in _KNOWN_CONTEXT_TOKENS.items() if mid.startswith(p)]
        if matches:
            return max(matches, key=lambda pc: len(pc[0]))[1]
    return None


def default_model_profile(model_id: str | None, override: int | None) -> ModelProfile:
    """Resolve a hosted model's profile without a live probe.

    Resolution order: explicit operator override, the known-model table, then a
    conservative default. Providers that serve a self-hosted model override
    ``Provider.model_profile`` to probe the live deployment instead.
    """
    mid = model_id or "unknown"
    if override:
        return ModelProfile(id=mid, context_window_tokens=int(override))
    known = _known_context_tokens(mid)
    if known:
        return ModelProfile(id=mid, context_window_tokens=known)
    logger.warning(
        "No context window size known for model %s; defaulting to %d tokens. "
        "Add this model to _KNOWN_CONTEXT_TOKENS in models.py.",
        mid,
        _DEFAULT_CONTEXT_TOKENS,
    )
    return ModelProfile(id=mid, context_window_tokens=_DEFAULT_CONTEXT_TOKENS)


def probed_model_profile(
    *,
    model_id: str | None,
    override: int | None,
    probe: Callable[[str], int | None],
    server_name: str,
    provider_logger: logging.Logger,
) -> ModelProfile:
    """Resolve a self-hosted model's profile by probing the live deployment.

    Resolution order: explicit operator override, live probe, conservative
    default. The known-model table is skipped on purpose, because a trained
    maximum would over-budget a server capped below it. ``probe`` receives the
    resolved model id, and ``provider_logger`` belongs to the calling provider
    so the warning names whoever failed to answer.
    """
    mid = model_id or "unknown"
    if override:
        return ModelProfile(id=mid, context_window_tokens=int(override))
    probed = probe(mid)
    if probed:
        return ModelProfile(id=mid, context_window_tokens=probed)
    # A failed probe is NOT silent: it collapses the prompt budget to the
    # conservative default, which over-trims the report's literature. Surface
    # it so an operator can pin the window instead of shipping a thin report.
    provider_logger.warning(
        "Could not probe the %s context window for %s. Falling back to a "
        "%d-token budget, so the report prompt will be trimmed more than "
        "necessary. Set OPENSCIENTIST_MODEL_CONTEXT_TOKENS to pin the window.",
        server_name,
        mid,
        _DEFAULT_CONTEXT_TOKENS,
    )
    return ModelProfile(id=mid, context_window_tokens=_DEFAULT_CONTEXT_TOKENS)
