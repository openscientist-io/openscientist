"""Tests for :mod:`openscientist.airgap.mcp_filter`.

Two flavors of test here:

1. **Policy tests** — every MCP tool + Claude built-in is classified
   correctly, ``allowed_mcp_tools`` and ``disallowed_claude_builtins``
   return the right sets across airgap on/off.
2. **Sentinel against live MCP registration** — walks the real
   :mod:`openscientist_tools` MCP server (via FastMCP's tool registry) and
   asserts every registered tool is classified. Catches a new tool being
   added without an airgap classification — the airgap policy default is
   permissive, so the sentinel is the only way to notice the gap.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openscientist.airgap.mcp_filter import (
    ALL_KNOWN_MCP_TOOLS,
    CLAUDE_BUILTINS_NETWORK,
    MCP_TOOLS_LOCAL_ONLY,
    MCP_TOOLS_NETWORK_DEPENDENT,
    allowed_mcp_tools,
    disallowed_claude_builtins,
    enforce_mcp_policy,
    unclassified_mcp_tools,
)


def _settings(*, airgap_enabled: bool, pubmed_addr: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(airgap=SimpleNamespace(enabled=airgap_enabled, pubmed_addr=pubmed_addr))


# --------------------------------------------------------- classification shape


class TestClassificationShape:
    """The constants are the policy. A rewrite mustn't silently widen or
    narrow them — these tests pin the named tools."""

    def test_local_only_includes_expected_tools(self) -> None:
        # The local-only set is the load-bearing one — every tool here is
        # safe in airgap regardless of operator config.
        expected = {
            "ping",
            "execute_code",
            "read_document",
            "set_status",
            "set_job_title",
            "save_iteration_summary",
            "set_consensus_answer",
            "update_knowledge_state",
            "add_hypothesis",
            "update_hypothesis",
            "run_phenix_tool",
            "compare_structures",
            "parse_alphafold_confidence",
        }
        assert MCP_TOOLS_LOCAL_ONLY == expected

    def test_network_dependent_includes_pubmed(self) -> None:
        # PubMed is the only currently-classified network-dependent tool.
        # Future additions go here.
        assert MCP_TOOLS_NETWORK_DEPENDENT == {"search_pubmed"}

    def test_local_and_network_dont_overlap(self) -> None:
        assert MCP_TOOLS_LOCAL_ONLY & MCP_TOOLS_NETWORK_DEPENDENT == set()

    def test_all_known_is_union(self) -> None:
        assert ALL_KNOWN_MCP_TOOLS == (MCP_TOOLS_LOCAL_ONLY | MCP_TOOLS_NETWORK_DEPENDENT)

    def test_claude_builtins_network_pinned(self) -> None:
        # The two Claude SDK built-ins that hit the network. A new one added
        # to the SDK without classifying here would let it through.
        assert CLAUDE_BUILTINS_NETWORK == {"WebFetch", "WebSearch"}


# --------------------------------------------------------- allowed_mcp_tools


class TestAllowedMcpTools:
    def test_non_airgap_allows_everything(self) -> None:
        s = _settings(airgap_enabled=False)
        assert allowed_mcp_tools(s) == ALL_KNOWN_MCP_TOOLS

    def test_airgap_without_pubmed_addr_disallows_search_pubmed(self) -> None:
        # The fail-closed default: no internal mirror configured → no
        # search_pubmed in the allowed set.
        s = _settings(airgap_enabled=True, pubmed_addr=None)
        allowed = allowed_mcp_tools(s)
        assert "search_pubmed" not in allowed
        assert allowed == MCP_TOOLS_LOCAL_ONLY

    def test_airgap_with_pubmed_addr_allows_search_pubmed(self) -> None:
        # Operator configured the mirror → tool is allowed (still through
        # the internal endpoint, not NCBI).
        s = _settings(airgap_enabled=True, pubmed_addr="10.0.0.6:9000")
        allowed = allowed_mcp_tools(s)
        assert "search_pubmed" in allowed
        assert allowed == MCP_TOOLS_LOCAL_ONLY | {"search_pubmed"}

    def test_empty_string_pubmed_addr_treated_as_unset(self) -> None:
        # "" is falsy in Python — should be treated as unset to fail closed.
        s = _settings(airgap_enabled=True, pubmed_addr="")
        assert "search_pubmed" not in allowed_mcp_tools(s)

    def test_legacy_settings_without_airgap_attr_defaults_to_disabled(self) -> None:
        # Mirrors runner.py / container_manager.py defensive pattern. Legacy
        # tests use SimpleNamespace settings stubs with no airgap field; this
        # one must read as not-airgap and allow everything.
        legacy = SimpleNamespace(container=SimpleNamespace())
        assert allowed_mcp_tools(legacy) == ALL_KNOWN_MCP_TOOLS


# --------------------------------------------------------- disallowed_claude_builtins


class TestDisallowedClaudeBuiltins:
    def test_non_airgap_returns_empty(self) -> None:
        # Default SDK behavior — no filter applied.
        s = _settings(airgap_enabled=False)
        assert disallowed_claude_builtins(s) == frozenset()

    def test_airgap_disables_webfetch_and_websearch(self) -> None:
        s = _settings(airgap_enabled=True)
        assert disallowed_claude_builtins(s) == {"WebFetch", "WebSearch"}

    def test_legacy_settings_default_to_not_disabled(self) -> None:
        legacy = SimpleNamespace(container=SimpleNamespace())
        assert disallowed_claude_builtins(legacy) == frozenset()


# --------------------------------------------------------- enforce_mcp_policy


class TestEnforceMcpPolicy:
    """Codex Review-6 wiring: enforce_mcp_policy walks the FastMCP registry
    and removes any tool not in allowed_mcp_tools. Without this call, the
    policy is dead code."""

    def _fresh_mcp(self):
        from mcp.server.fastmcp import FastMCP

        m = FastMCP("test")

        @m.tool()
        def ping(message: str = "hello") -> str:  # noqa: ARG001
            return "pong"

        @m.tool()
        def search_pubmed(query: str) -> str:  # noqa: ARG001
            return ""

        @m.tool()
        def execute_code(code: str) -> str:  # noqa: ARG001
            return ""

        return m

    def test_non_airgap_removes_nothing(self) -> None:
        m = self._fresh_mcp()
        s = _settings(airgap_enabled=False)
        removed = enforce_mcp_policy(m, s)
        assert removed == []

    def test_airgap_without_pubmed_addr_removes_search_pubmed(self) -> None:
        m = self._fresh_mcp()
        s = _settings(airgap_enabled=True, pubmed_addr=None)
        removed = enforce_mcp_policy(m, s)
        assert "search_pubmed" in removed
        assert "ping" not in removed
        assert "execute_code" not in removed

    def test_airgap_with_pubmed_addr_keeps_search_pubmed(self) -> None:
        m = self._fresh_mcp()
        s = _settings(airgap_enabled=True, pubmed_addr="10.0.0.6:9000")
        removed = enforce_mcp_policy(m, s)
        assert "search_pubmed" not in removed

    def test_unknown_tool_in_registry_removed_in_airgap(self) -> None:
        # If a future tool gets registered without a classification, airgap
        # mode removes it. (Non-airgap is default-permissive.)
        from mcp.server.fastmcp import FastMCP

        m = FastMCP("test")

        @m.tool()
        def mystery_query(arg: str) -> str:  # noqa: ARG001
            return ""

        s = _settings(airgap_enabled=True)
        removed = enforce_mcp_policy(m, s)
        assert "mystery_query" in removed

    def test_returns_empty_on_internal_layout_change(self) -> None:
        # Defense-in-depth: if FastMCP's internals shift and we can't reach
        # the tool manager, the function returns [] instead of crashing the
        # server. The agent-side egress boundary still holds.
        class BrokenMcp:
            pass  # no _tool_manager attribute

        s = _settings(airgap_enabled=True)
        removed = enforce_mcp_policy(BrokenMcp(), s)
        assert removed == []


# --------------------------------------------------------- _apply_airgap_policy fail-closed


class TestApplyAirgapPolicyFailClosed:
    """Codex Review-7 BUG #4: the prior _apply_airgap_policy silently
    fail-opened when settings load raised, so the policy enforcement was
    dead code under a misconfigured env. The fix: re-raise when
    OPENSCIENTIST_AIR_GAPPED is set so the MCP server refuses to start."""

    def test_airgap_requested_settings_load_fails_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Set airgap env so the policy is required, then make settings load
        # raise. The function must re-raise rather than fail-open.
        monkeypatch.setenv("OPENSCIENTIST_AIR_GAPPED", "true")
        # Required AirgapSettings validators — present so module-import of
        # `openscientist_tools.server` doesn't blow up before the test runs.
        monkeypatch.setenv("OPENSCIENTIST_AIRGAP_LLM_ADDR", "10.0.0.5:8443")
        monkeypatch.setenv("OPENSCIENTIST_AIRGAP_PUBMED_ADDR", "10.0.0.6:9000")
        monkeypatch.setenv("OPENSCIENTIST_JOB_ID", "mcp-filter-test")
        monkeypatch.setenv("OPENSCIENTIST_JOB_DIR", "/tmp")

        from openscientist_tools import server as srv_module

        with patch(
            "openscientist_tools.server._load_settings_and_filter",
            side_effect=RuntimeError("settings broken for test"),
        ):
            with pytest.raises(RuntimeError, match="settings broken"):
                srv_module._apply_airgap_policy()  # type: ignore[attr-defined]

    def test_non_airgap_settings_load_fails_silently(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Non-airgap deployment with a broken settings load — fail-open is
        # the right behavior here; policy enforcement isn't load-bearing.
        monkeypatch.delenv("OPENSCIENTIST_AIR_GAPPED", raising=False)
        monkeypatch.setenv("OPENSCIENTIST_JOB_ID", "mcp-filter-test")
        monkeypatch.setenv("OPENSCIENTIST_JOB_DIR", "/tmp")

        from openscientist_tools import server as srv_module

        with patch(
            "openscientist_tools.server._load_settings_and_filter",
            side_effect=RuntimeError("settings broken for test"),
        ):
            # Must not raise.
            srv_module._apply_airgap_policy()  # type: ignore[attr-defined]

    def test_airgap_mode_requested_parse_handles_truthy_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from openscientist_tools import server as srv_module

        for truthy in ("1", "true", "TRUE", "yes", "on"):
            monkeypatch.setenv("OPENSCIENTIST_AIR_GAPPED", truthy)
            assert srv_module._airgap_mode_requested(), (  # type: ignore[attr-defined]
                f"{truthy!r} should be truthy"
            )
        for falsy in ("", "0", "false", "no", "off"):
            monkeypatch.setenv("OPENSCIENTIST_AIR_GAPPED", falsy)
            assert not srv_module._airgap_mode_requested(), (  # type: ignore[attr-defined]
                f"{falsy!r} should be falsy"
            )


# --------------------------------------------------------- unclassified sentinel


class TestUnclassifiedDetector:
    """Helper used by the live-registration sentinel below + by any future
    audit script."""

    def test_unclassified_when_new_tool_appears(self) -> None:
        # Pretend a new MCP tool 'magic_query' got added without being
        # classified into either of the constants.
        registered = set(ALL_KNOWN_MCP_TOOLS) | {"magic_query"}
        assert unclassified_mcp_tools(registered) == {"magic_query"}

    def test_no_unclassified_when_registered_subset_of_known(self) -> None:
        registered = set(MCP_TOOLS_LOCAL_ONLY)  # missing a known tool is fine
        assert unclassified_mcp_tools(registered) == set()

    def test_returns_frozenset_for_safe_use_in_assertions(self) -> None:
        result = unclassified_mcp_tools({"foo"})
        assert isinstance(result, frozenset)


# --------------------------------------------------------- live MCP registration


class TestLiveMcpRegistration:
    """Walk the real FastMCP registry. The sentinel here catches a new tool
    added without being classified into one of the constants."""

    @pytest.fixture(autouse=True)
    def _tool_state_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        # openscientist_tools.server imports openscientist_tools.state which
        # instantiates a ToolServerState pydantic-settings model with two
        # required env vars (OPENSCIENTIST_JOB_ID, OPENSCIENTIST_JOB_DIR).
        # In a real agent run the runner sets these; in this test we provide
        # placeholders so the import succeeds and the FastMCP registry is
        # walkable.
        monkeypatch.setenv("OPENSCIENTIST_JOB_ID", "mcp-filter-test")
        monkeypatch.setenv("OPENSCIENTIST_JOB_DIR", str(tmp_path))

    def _registered_tool_names(self) -> set[str]:
        # Import lazily so test collection doesn't fail if a downstream
        # tool module has an import error unrelated to this filter, and so
        # the env-var fixture above is in effect at import time.
        from openscientist_tools.server import mcp

        # FastMCP exposes a list_tools coroutine that returns the registered
        # set; resolve it synchronously here.
        tools = asyncio.run(mcp.list_tools())
        return {t.name for t in tools}

    def test_every_registered_tool_is_classified(self) -> None:
        # The load-bearing security sentinel — a new tool added to the MCP
        # server without an airgap classification slips through `allowed_mcp
        # _tools` (default-permissive) and could exfiltrate silently. This
        # test forces the classification to be added at registration time.
        registered = self._registered_tool_names()
        missing = unclassified_mcp_tools(registered)
        assert missing == frozenset(), (
            f"New MCP tool(s) registered but not classified in mcp_filter.py: "
            f"{sorted(missing)}. Add them to either MCP_TOOLS_LOCAL_ONLY "
            f"or MCP_TOOLS_NETWORK_DEPENDENT, then update the test sentinel."
        )

    # The reverse direction (every classified tool is currently registered)
    # is NOT a sentinel — `add_hypothesis`/`update_hypothesis` only register
    # when `use_hypotheses` is set on the job, and the Phenix tools only
    # register when Phenix is installed. Classifying them up front is
    # forward-looking policy, not a bug.


# --------------------------------------------------------- parametrized end-to-end


@pytest.mark.parametrize(
    "airgap_enabled,pubmed_addr,expected_search_pubmed",
    [
        (False, None, True),  # non-airgap: everything allowed
        (False, "10.0.0.6:9000", True),  # non-airgap with mirror: still allowed
        (True, None, False),  # airgap without mirror: search_pubmed denied
        (True, "10.0.0.6:9000", True),  # airgap with mirror: allowed
        (True, "", False),  # airgap with empty addr: denied (falsy)
    ],
)
def test_search_pubmed_allowance_matrix(
    airgap_enabled: bool,
    pubmed_addr: str | None,
    expected_search_pubmed: bool,
) -> None:
    """The 4x2 truth table — airgap × pubmed-addr → search_pubmed allowed?"""
    s = _settings(airgap_enabled=airgap_enabled, pubmed_addr=pubmed_addr)
    assert ("search_pubmed" in allowed_mcp_tools(s)) is expected_search_pubmed
