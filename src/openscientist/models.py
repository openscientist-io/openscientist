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
probe the live endpoint. This module holds the value object plus the shared
"hosted model" resolution (``default_model_profile``: explicit override, known-model
table, conservative default) that providers without a live endpoint reuse.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Conservative fallback when nothing else resolves: small enough that prompt
# budgeting trims aggressively rather than risking a silent context overflow.
_DEFAULT_CONTEXT_TOKENS = 8192

# Known API models (trained windows). Self-hosted models are probed instead,
# because their deployment can cap the window below the trained maximum.
_KNOWN_CONTEXT_TOKENS: dict[str, int] = {
    "claude-opus-4": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-haiku-4": 200_000,
    "claude-3-5-sonnet": 200_000,
    "gpt-4o": 128_000,
    "gpt-4.1": 1_047_576,
    "gpt-5": 400_000,
}


@dataclass(frozen=True)
class ModelProfile:
    """Intrinsic, provider-independent properties of an LLM."""

    id: str
    context_window_tokens: int
    # Room to grow: max_output_tokens, supports_tool_use, vision, reasoning, ...


def _known_context_tokens(model_id: str) -> int | None:
    """Look up a known API model's window by longest-matching prefix."""
    matches = [(p, ctx) for p, ctx in _KNOWN_CONTEXT_TOKENS.items() if model_id.startswith(p)]
    if not matches:
        return None
    return max(matches, key=lambda pc: len(pc[0]))[1]


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
    logger.info(
        "No context window known for model %s; defaulting to %d tokens",
        mid,
        _DEFAULT_CONTEXT_TOKENS,
    )
    return ModelProfile(id=mid, context_window_tokens=_DEFAULT_CONTEXT_TOKENS)
