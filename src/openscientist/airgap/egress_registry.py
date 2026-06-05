"""Provider → egress-target dispatch table for air-gapped mode.

Per the air-gap RFC §7.2: rather than adding an ``airgap_egress_targets()``
method to ``providers/base.py`` and every provider subclass — which would mean
modifying the provider hierarchy Luca just rewrote — keep all air-gap-specific
logic external to the provider classes. This module is the dispatch table.

For each provider id registered in ``providers/__init__.py``, this module
declares the deterministic ``(host, port)`` set the provider would reach when
configured in air-gapped mode. At job start
:func:`validate_provider_for_airgap` checks every target is in the operator's
allowlist; otherwise the job refuses to run (fail-closed, RFC §G4).

Providers whose endpoint can't be made deterministic at startup (Bedrock /
Vertex SDK regional clients with no override surface) raise
:class:`AirGapUnsupportedError`. The operator can opt-in by providing an
explicit internal mapping (e.g. ``OPENSCIENTIST_AIRGAP_BEDROCK_ENDPOINT``);
the registry entry then reads that override.

The pytest fixture in ``tests/airgap/test_egress_registry.py`` asserts the
registry's key set matches the set of registered provider ids — adding a new
provider without a registry entry fails CI rather than silently bypassing
the airgap egress check.
"""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import urlparse


class AirGapPolicyError(RuntimeError):
    """The configured provider's egress targets aren't in the airgap allowlist."""


class AirGapUnsupportedError(RuntimeError):
    """The configured provider has no deterministic egress and isn't explicitly mapped."""


HostPort = tuple[str, int]


def _from_url(url: str | None) -> set[HostPort]:
    """Parse a single ``https://host[:port]/...`` URL into a ``{(host, port)}`` set.

    Returns the empty set for ``None`` or unparseable input so the caller can
    fall back to a different source (e.g. a resource-derived URL) via ``or``.
    """
    if not url:
        return set()
    parsed = urlparse(url)
    if not parsed.hostname:
        return set()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return {(parsed.hostname, port)}


def _from_foundry_resource(resource: str | None) -> set[HostPort]:
    """Foundry derives ``https://{resource}.services.ai.azure.com/anthropic``."""
    if not resource:
        return set()
    return {(f"{resource}.services.ai.azure.com", 443)}


def _from_azure_openai_resource(resource: str | None) -> set[HostPort]:
    """Azure OpenAI derives ``https://{resource}.openai.azure.com/openai/v1``."""
    if not resource:
        return set()
    return {(f"{resource}.openai.azure.com", 443)}


def _unsupported(reason: str) -> set[HostPort]:
    """Helper for the registry entries below: raises rather than returning a set."""
    raise AirGapUnsupportedError(reason)


# Dispatch: provider_id → callable(settings) → set of (host, port) it would reach.
#
# When you add a provider to ``providers/__init__.py``, add it here too — the
# coverage test in ``tests/airgap/test_egress_registry.py`` asserts this
# mapping covers every registered provider id.
EGRESS_TARGETS: dict[str, Callable[[Any], set[HostPort]]] = {
    "anthropic": lambda s: (
        _from_url(getattr(s.provider, "anthropic_base_url", None))
        or _unsupported(
            "Anthropic provider with no ANTHROPIC_BASE_URL override — "
            "set it to your internal endpoint for air-gapped mode"
        )
    ),
    "cborg": lambda s: _from_url(getattr(s.provider, "anthropic_base_url", None)),
    "foundry": lambda s: (
        _from_url(getattr(s.provider, "anthropic_foundry_base_url", None))
        or _from_foundry_resource(
            getattr(s.provider, "anthropic_foundry_resource", None)
        )
        or _unsupported(
            "Foundry provider with no ANTHROPIC_FOUNDRY_BASE_URL or _RESOURCE — "
            "set one for air-gapped mode"
        )
    ),
    "openai": lambda s: (
        _from_url(getattr(s.provider, "openai_base_url", None))
        or _unsupported(
            "OpenAI provider using Codex's default endpoint — "
            "set OPENAI_BASE_URL to your internal endpoint for air-gapped mode"
        )
    ),
    "azure_openai": lambda s: (
        _from_azure_openai_resource(getattr(s.provider, "azure_openai_resource", None))
        or _unsupported(
            "Azure OpenAI provider with no AZURE_OPENAI_RESOURCE — "
            "set it for air-gapped mode"
        )
    ),
    "bedrock": lambda s: (
        _from_url(getattr(s.provider, "airgap_bedrock_endpoint", None))
        or _unsupported(
            "Bedrock provider's regional SDK client has no introspectable endpoint — "
            "set OPENSCIENTIST_AIRGAP_BEDROCK_ENDPOINT to your internal Bedrock-compatible URL"
        )
    ),
    "vertex": lambda s: (
        _from_url(getattr(s.provider, "airgap_vertex_endpoint", None))
        or _unsupported(
            "Vertex provider's regional SDK client has no introspectable endpoint — "
            "set OPENSCIENTIST_AIRGAP_VERTEX_ENDPOINT to your internal Vertex-compatible URL"
        )
    ),
}


def egress_targets_for(provider_id: str, settings: Any) -> set[HostPort]:
    """Return the IP:port set the named provider would reach in air-gap config.

    Args:
        provider_id: Matches the keys in ``providers/__init__.py``'s registry
            (``"anthropic"``, ``"openai"``, ``"foundry"``, etc.).
        settings: The OS settings object (typed ``Any`` here to avoid a hard
            dependency on the settings module from this module).

    Returns:
        Set of ``(hostname, port)`` tuples.

    Raises:
        AirGapPolicyError: ``provider_id`` is not registered.
        AirGapUnsupportedError: provider's endpoint isn't deterministic and
            no operator override has been set.
    """
    if provider_id not in EGRESS_TARGETS:
        raise AirGapPolicyError(
            f"Provider {provider_id!r} is not registered in the airgap egress registry"
        )
    return EGRESS_TARGETS[provider_id](settings)


def validate_provider_for_airgap(
    provider_id: str,
    settings: Any,
    allowlist: set[HostPort],
) -> set[HostPort]:
    """Walk the configured provider's egress targets; refuse if any aren't allowlisted.

    Returns the resolved target set on success (so the caller can record it in
    the per-job attestation).

    Raises:
        AirGapPolicyError: any target is outside the allowlist.
        AirGapUnsupportedError: provider isn't deterministic and not mapped.
    """
    targets = egress_targets_for(provider_id, settings)
    extra = targets - allowlist
    if extra:
        raise AirGapPolicyError(
            f"Provider {provider_id!r} would reach {sorted(extra)}, "
            f"not in airgap allowlist {sorted(allowlist)}"
        )
    return targets
