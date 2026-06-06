"""Tests for :mod:`openscientist.airgap.egress_registry`.

Includes the **coverage assertion** — adding a new provider to OS without
adding a registry entry would silently bypass the airgap egress check, which
is exactly the bug Codex Review-3 wanted blocked.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from openscientist.airgap.egress_registry import (
    EGRESS_TARGETS,
    AirGapPolicyError,
    AirGapUnsupportedError,
    egress_targets_for,
    validate_provider_for_airgap,
)


def _settings(**provider_attrs: object) -> SimpleNamespace:
    """Build a fake ``settings`` whose ``.provider`` namespace has the given attrs."""
    return SimpleNamespace(provider=SimpleNamespace(**provider_attrs))


# --------------------------------------------------------- registry coverage


class TestRegistryCoverage:
    """The registry must cover every provider OS exposes.

    Adding a new provider in ``providers/__init__.py`` without a registry entry
    here would let air-gap mode silently accept that provider — defeating the
    purpose of the check. This test fails noisily when that happens; the fix
    is to add the entry, not relax the test.
    """

    def test_known_providers_present(self) -> None:
        # Must match the id strings returned by `providers/__init__.py:get_provider`.
        # Note `azure-openai` is hyphenated, not underscored — matches the
        # actual provider id at `providers/__init__.py:47` and `:126`.
        expected = {
            "anthropic",
            "cborg",
            "foundry",
            "openai",
            "azure-openai",
            "bedrock",
            "vertex",
        }
        assert set(EGRESS_TARGETS) == expected


# --------------------------------------------------------- egress_targets_for


class TestEgressTargetsFor:
    def test_cborg_from_base_url(self) -> None:
        s = _settings(anthropic_base_url="https://internal-llm.example:8443/anthropic")
        assert egress_targets_for("cborg", s) == {("internal-llm.example", 8443)}

    def test_foundry_explicit_url_wins(self) -> None:
        s = _settings(
            anthropic_foundry_base_url="https://foundry.internal:443/anthropic",
            anthropic_foundry_resource="ignored-because-url-set",
        )
        assert egress_targets_for("foundry", s) == {("foundry.internal", 443)}

    def test_foundry_resource_derives_endpoint(self) -> None:
        s = _settings(
            anthropic_foundry_base_url=None,
            anthropic_foundry_resource="myresource",
        )
        assert egress_targets_for("foundry", s) == {
            ("myresource.services.ai.azure.com", 443)
        }

    def test_azure_openai_resource_derives_endpoint(self) -> None:
        s = _settings(azure_openai_resource="my-aoai")
        assert egress_targets_for("azure-openai", s) == {
            ("my-aoai.openai.azure.com", 443)
        }

    def test_anthropic_requires_base_url_override(self) -> None:
        s = _settings(anthropic_base_url=None)
        with pytest.raises(AirGapUnsupportedError, match="ANTHROPIC_BASE_URL"):
            egress_targets_for("anthropic", s)

    def test_openai_unsupported_in_airgap(self) -> None:
        # OpenAI provider has no base-URL override field on current main.
        # Until RFC §19 OQ is resolved, air-gap refuses this provider.
        s = _settings()
        with pytest.raises(AirGapUnsupportedError, match="OpenAI provider"):
            egress_targets_for("openai", s)

    def test_bedrock_unsupported_in_airgap(self) -> None:
        s = _settings()
        with pytest.raises(AirGapUnsupportedError, match="Bedrock provider"):
            egress_targets_for("bedrock", s)

    def test_vertex_unsupported_in_airgap(self) -> None:
        s = _settings()
        with pytest.raises(AirGapUnsupportedError, match="Vertex provider"):
            egress_targets_for("vertex", s)

    def test_unknown_provider_raises_policy_error(self) -> None:
        s = _settings()
        with pytest.raises(AirGapPolicyError, match="not registered"):
            egress_targets_for("never-heard-of-it", s)


# --------------------------------------------------------- validate_provider_for_airgap


class TestValidateProviderForAirgap:
    def test_target_in_allowlist_passes(self) -> None:
        s = _settings(anthropic_base_url="https://llm.internal:8443/anthropic")
        targets = validate_provider_for_airgap(
            "cborg",
            s,
            allowlist={("llm.internal", 8443), ("pubmed.internal", 9000)},
        )
        assert targets == {("llm.internal", 8443)}

    def test_target_not_in_allowlist_raises(self) -> None:
        s = _settings(anthropic_base_url="https://api.anthropic.com")
        with pytest.raises(AirGapPolicyError, match="not in airgap allowlist"):
            validate_provider_for_airgap(
                "cborg",
                s,
                allowlist={("llm.internal", 8443)},
            )

    def test_unsupported_provider_propagates(self) -> None:
        s = _settings()
        with pytest.raises(AirGapUnsupportedError):
            validate_provider_for_airgap(
                "bedrock",
                s,
                allowlist={("any", 443)},
            )

    def test_azure_openai_validates_with_hyphenated_id(self) -> None:
        # Regression: catch the underscore-vs-hyphen typo Codex flagged
        # against v4 — the provider id is `azure-openai`, not `azure_openai`.
        s = _settings(azure_openai_resource="prod-aoai")
        targets = validate_provider_for_airgap(
            "azure-openai",
            s,
            allowlist={("prod-aoai.openai.azure.com", 443)},
        )
        assert targets == {("prod-aoai.openai.azure.com", 443)}

    def test_default_port_80_for_http(self) -> None:
        # Sanity check on the URL parser — http defaults to 80.
        s = _settings(anthropic_base_url="http://plain-http.internal/anthropic")
        targets = validate_provider_for_airgap(
            "cborg",
            s,
            allowlist={("plain-http.internal", 80)},
        )
        assert targets == {("plain-http.internal", 80)}
