"""Tests for the `Provider` ABC family in `providers/base.py`."""

from __future__ import annotations

import pytest

from openscientist.providers.base import (
    ClaudeCompatible,
    CodexCompatible,
    CostInfo,
    Provider,
)


def _stub_cost_info(provider_name: str, lookback_hours: int = 24) -> CostInfo:
    return CostInfo(
        provider_name=provider_name,
        month_spend_usd=None,
        recent_spend_usd=None,
        recent_period_hours=lookback_hours,
    )


class _StubClaude(ClaudeCompatible):
    @property
    def id(self) -> str:
        return "stub-claude"

    display_name = "Stub Claude"

    def validate_required_config(self) -> list[str]:
        return []

    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        return _stub_cost_info(self.display_name, lookback_hours)

    def setup_environment(self) -> None:
        return None

    def claude_sdk_env(self) -> dict[str, str]:
        return {"ANTHROPIC_API_KEY": "sk-test"}

    def claude_model_name(self) -> str:
        return "claude-test-model"

    def harness_env(self, *, proxy: str | None) -> dict[str, str]:
        return {}


class _StubCodex(CodexCompatible):
    @property
    def id(self) -> str:
        return "stub-codex"

    display_name = "Stub Codex"

    def validate_required_config(self) -> list[str]:
        return []

    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        return _stub_cost_info(self.display_name, lookback_hours)

    def codex_config_overrides(self) -> list[str]:
        return ["model_reasoning_effort=high"]

    def codex_model_name(self) -> str:
        return "gpt-codex-test"

    def codex_model_provider_id(self) -> str:
        return "openai"

    def codex_sdk_env(self) -> dict[str, str]:
        return {"OPENAI_API_KEY": "sk-test"}


class _StubHybrid(ClaudeCompatible, CodexCompatible):
    @property
    def id(self) -> str:
        return "stub-hybrid"

    display_name = "Stub Hybrid"

    def validate_required_config(self) -> list[str]:
        return []

    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        return _stub_cost_info(self.display_name, lookback_hours)

    def setup_environment(self) -> None:
        return None

    def claude_sdk_env(self) -> dict[str, str]:
        return {}

    def claude_model_name(self) -> str:
        return "claude-hybrid"

    def codex_config_overrides(self) -> list[str]:
        return []

    def codex_model_name(self) -> str:
        return "codex-hybrid"

    def codex_model_provider_id(self) -> str:
        return "hybrid"

    def codex_sdk_env(self) -> dict[str, str]:
        return {}


def test_provider_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        Provider()  # type: ignore[abstract]


def test_claude_compatible_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        ClaudeCompatible()  # type: ignore[abstract]


def test_codex_compatible_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        CodexCompatible()  # type: ignore[abstract]


def test_incomplete_claude_subclass_cannot_instantiate() -> None:
    class _Incomplete(ClaudeCompatible):
        @property
        def id(self) -> str:
            return "incomplete"

        display_name = "Incomplete"

        def validate_required_config(self) -> list[str]:
            return []

        def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
            return _stub_cost_info(self.display_name, lookback_hours)

        def setup_environment(self) -> None:
            return None

        def claude_model_name(self) -> str:
            return "m"

        # claude_sdk_env intentionally omitted

    with pytest.raises(TypeError):
        _Incomplete()  # type: ignore[abstract]


def test_construction_raises_on_required_config_errors() -> None:
    """The Provider base validates configuration on construction."""

    class _Misconfigured(_StubClaude):
        def validate_required_config(self) -> list[str]:
            return ["missing OPENAI_API_KEY"]

    with pytest.raises(ValueError, match="missing OPENAI_API_KEY"):
        _Misconfigured()


def test_complete_claude_provider_instantiates_and_returns_values() -> None:
    provider = _StubClaude()
    assert provider.id == "stub-claude"
    assert provider.display_name == "Stub Claude"
    assert provider.validate_required_config() == []
    assert provider.claude_sdk_env() == {"ANTHROPIC_API_KEY": "sk-test"}
    assert provider.claude_model_name() == "claude-test-model"


def test_complete_codex_provider_instantiates_and_returns_values() -> None:
    provider = _StubCodex()
    assert provider.id == "stub-codex"
    assert provider.display_name == "Stub Codex"
    assert provider.validate_required_config() == []
    assert provider.codex_config_overrides() == ["model_reasoning_effort=high"]
    assert provider.codex_model_name() == "gpt-codex-test"
    assert provider.codex_model_provider_id() == "openai"


def test_family_isinstance_relationships() -> None:
    claude = _StubClaude()
    codex = _StubCodex()

    assert isinstance(claude, Provider)
    assert isinstance(claude, ClaudeCompatible)
    assert not isinstance(claude, CodexCompatible)

    assert isinstance(codex, Provider)
    assert isinstance(codex, CodexCompatible)
    assert not isinstance(codex, ClaudeCompatible)


def test_multi_family_provider_satisfies_both() -> None:
    hybrid = _StubHybrid()
    assert isinstance(hybrid, Provider)
    assert isinstance(hybrid, ClaudeCompatible)
    assert isinstance(hybrid, CodexCompatible)
    assert hybrid.claude_model_name() == "claude-hybrid"
    assert hybrid.codex_model_name() == "codex-hybrid"
