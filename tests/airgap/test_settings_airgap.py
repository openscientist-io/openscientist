"""Tests for :class:`openscientist.settings.AirgapSettings`."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from openscientist.settings import AirgapSettings


class TestDefaults:
    def test_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Clear any inherited airgap env so the test sees defaults.
        for var in (
            "OPENSCIENTIST_AIR_GAPPED",
            "OPENSCIENTIST_AIRGAP_LLM_ADDR",
            "OPENSCIENTIST_AIRGAP_PUBMED_ADDR",
            "OPENSCIENTIST_AIRGAP_CODEX_HOME_ROOT",
        ):
            monkeypatch.delenv(var, raising=False)
        s = AirgapSettings(_env_file=None)
        assert s.enabled is False
        assert s.llm_addr is None
        assert s.pubmed_addr is None
        assert s.codex_home_root is None


class TestFailClosed:
    """RFC §G4: enabling air-gap mode without the required addresses must
    fail at startup, not silently produce a broken deployment."""

    def test_enabled_without_addrs_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENSCIENTIST_AIR_GAPPED", "true")
        monkeypatch.delenv("OPENSCIENTIST_AIRGAP_LLM_ADDR", raising=False)
        monkeypatch.delenv("OPENSCIENTIST_AIRGAP_PUBMED_ADDR", raising=False)
        with pytest.raises(ValidationError, match="OPENSCIENTIST_AIRGAP_LLM_ADDR"):
            AirgapSettings(_env_file=None)

    def test_enabled_without_pubmed_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENSCIENTIST_AIR_GAPPED", "true")
        monkeypatch.setenv("OPENSCIENTIST_AIRGAP_LLM_ADDR", "10.0.0.5:8443")
        monkeypatch.delenv("OPENSCIENTIST_AIRGAP_PUBMED_ADDR", raising=False)
        with pytest.raises(ValidationError, match="OPENSCIENTIST_AIRGAP_PUBMED_ADDR"):
            AirgapSettings(_env_file=None)

    def test_enabled_with_all_addrs_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENSCIENTIST_AIR_GAPPED", "true")
        monkeypatch.setenv("OPENSCIENTIST_AIRGAP_LLM_ADDR", "10.0.0.5:8443")
        monkeypatch.setenv("OPENSCIENTIST_AIRGAP_PUBMED_ADDR", "10.0.0.6:9000")
        s = AirgapSettings(_env_file=None)
        assert s.enabled is True
        assert s.llm_addr == "10.0.0.5:8443"
        assert s.pubmed_addr == "10.0.0.6:9000"

    def test_disabled_without_addrs_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Default state — disabled with no addresses set — must not raise.
        # Otherwise every non-airgap deployment fails at startup.
        for var in (
            "OPENSCIENTIST_AIR_GAPPED",
            "OPENSCIENTIST_AIRGAP_LLM_ADDR",
            "OPENSCIENTIST_AIRGAP_PUBMED_ADDR",
        ):
            monkeypatch.delenv(var, raising=False)
        s = AirgapSettings(_env_file=None)
        assert s.enabled is False


class TestCodexHomeRootField:
    def test_optional_when_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # codex_home_root is optional even in airgap mode — the agent has a
        # default. Operators set it on non-Linux dev or to override the tmpfs
        # path.
        monkeypatch.setenv("OPENSCIENTIST_AIR_GAPPED", "true")
        monkeypatch.setenv("OPENSCIENTIST_AIRGAP_LLM_ADDR", "10.0.0.5:8443")
        monkeypatch.setenv("OPENSCIENTIST_AIRGAP_PUBMED_ADDR", "10.0.0.6:9000")
        monkeypatch.delenv("OPENSCIENTIST_AIRGAP_CODEX_HOME_ROOT", raising=False)
        s = AirgapSettings(_env_file=None)
        assert s.codex_home_root is None

    def test_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENSCIENTIST_AIR_GAPPED", "true")
        monkeypatch.setenv("OPENSCIENTIST_AIRGAP_LLM_ADDR", "10.0.0.5:8443")
        monkeypatch.setenv("OPENSCIENTIST_AIRGAP_PUBMED_ADDR", "10.0.0.6:9000")
        monkeypatch.setenv("OPENSCIENTIST_AIRGAP_CODEX_HOME_ROOT", "/tmp/codex-home")
        s = AirgapSettings(_env_file=None)
        assert s.codex_home_root == "/tmp/codex-home"
