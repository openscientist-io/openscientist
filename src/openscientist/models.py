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
resolutions: ``default_model_profile`` (explicit override, the litellm model
database, known-model table, conservative default) for hosted APIs, and
``probed_model_profile`` (explicit override, live probe, conservative default) for
self-hosted servers.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Conservative fallback when nothing else resolves: small enough that prompt
# budgeting trims aggressively rather than risking a silent context overflow.
_DEFAULT_CONTEXT_TOKENS = 8192

# Known API models' input windows, the offline fallback for the litellm database.
# Every value is litellm's ``max_input_tokens`` for that family, which the
# `network`-marked test checks. Self-hosted models are probed instead, because
# their deployment can cap the window below the trained maximum.
_KNOWN_CONTEXT_TOKENS: dict[str, int] = {
    "claude-3-5-sonnet": 200_000,
    "claude-opus-4": 200_000,
    "claude-opus-4-6": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-sonnet-4": 200_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-haiku-4": 200_000,
    "claude-opus-5": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-haiku-5": 1_000_000,
    "claude-fable-5": 1_000_000,
    "claude-mythos-5": 1_000_000,
    "gpt-4o": 128_000,
    "gpt-4.1": 1_047_576,
    "gpt-5": 272_000,
}


@dataclass(frozen=True)
class ModelProfile:
    """Intrinsic, provider-independent properties of an LLM."""

    id: str
    context_window_tokens: int
    # Room to grow: max_output_tokens, supports_tool_use, vision, reasoning, ...


def normalize_model_name(model: str) -> str:
    """Strip provider-specific prefixes and suffixes to get a litellm-compatible key.

    Handles Bedrock region prefixes (``us.``, ``apac.``, ``global.``, ``us-gov.``),
    dated snapshots, Vertex ``@`` versions, and Bedrock's ``-v1`` / ``-v1:0`` suffix
    on Claude ids. Other families keep a ``-vN`` ending, which is part of their name.
    """
    model = re.sub(r"^(?:[a-z]+(?:-[a-z]+)?\.)?anthropic\.", "", model)
    model = re.sub(r"-\d{8}.*$", "", model)
    model = re.sub(r"@[^@]+$", "", model)
    if model.startswith("claude-"):
        model = re.sub(r"-v\d+(?::\d+)?$", "", model)
    return model


def _known_context_tokens(model_id: str) -> int | None:
    """Look up a known API model's window by longest-matching prefix."""
    matches = [(p, ctx) for p, ctx in _KNOWN_CONTEXT_TOKENS.items() if model_id.startswith(p)]
    if not matches:
        return None
    return max(matches, key=lambda pc: len(pc[0]))[1]


def _catalog_context_tokens(model_id: str, catalog: Mapping[str, Any]) -> int | None:
    """The model's ``max_input_tokens`` in the litellm database, exact id first."""
    for key in (model_id, normalize_model_name(model_id)):
        entry = catalog.get(key)
        tokens = entry.get("max_input_tokens") if isinstance(entry, dict) else None
        if isinstance(tokens, int) and tokens > 0:
            return tokens
    return None


def default_model_profile(
    model_id: str | None,
    override: int | None,
    catalog: Mapping[str, Any] | None = None,
) -> ModelProfile:
    """Resolve a hosted model's profile without a live probe or any I/O.

    Resolution order: explicit operator override, the litellm model database
    ``catalog`` the caller passes, the known-model table, then a conservative
    default. Providers that serve a self-hosted model override
    ``Provider.model_profile`` to probe the live deployment instead.
    """
    mid = model_id or "unknown"
    if override:
        return ModelProfile(id=mid, context_window_tokens=int(override))
    known = (_catalog_context_tokens(mid, catalog) if catalog else None) or _known_context_tokens(
        normalize_model_name(mid)
    )
    if known:
        return ModelProfile(id=mid, context_window_tokens=known)
    logger.warning(
        "No context window known for model %s, so prompts are budgeted for %d tokens. "
        "Set OPENSCIENTIST_MODEL_CONTEXT_TOKENS to the model's window.",
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
