"""Air-gap-policy-enforcing Claude Code (SDK) agent.

:class:`AirgapClaudeCodeAgent` is a thin subclass of :class:`ClaudeCodeAgent`
that overrides three protected surfaces to satisfy the air-gap policy
without changing the base class's public contract. The subclass is
selected by ``agent/factory.py`` when ``settings.airgap.enabled=True`` and
the active provider is :class:`ClaudeCompatible`.

Per RFC §10.3 and §12.1:

* :meth:`_build_options` — disable the SDK's network-capable built-in tools
  (``WebFetch``, ``WebSearch``) via
  :func:`airgap.mcp_filter.disallowed_claude_builtins`, and route the
  provider's auth env through the filtered allowlist rather than the
  ambient process environment.
* :meth:`_build_subprocess_env` — seed the MCP tools subprocess env from
  :func:`airgap.env_allowlist.filtered_agent_env` rather than a full
  ``os.environ`` copy, so credentials for inactive providers don't reach
  the MCP server child process (mirrors :class:`AirgapCodexAgent`'s
  ``_mcp_env`` override).
* :meth:`_apply_provider_env` — no-op. The base class calls
  ``os.environ.update(self._provider.claude_sdk_env())``, mutating the
  *parent* process environment. ``ClaudeAgentOptions.env`` is additive to
  ``os.environ`` for the SDK's bundled CLI subprocess (confirmed against
  ``claude_agent_sdk``'s ``subprocess_cli.py``), so anything the base
  class would write there is redundant with — and less safe than —
  passing the same auth env through ``options.env`` in
  :meth:`_build_options` instead, where it goes through the same
  allowlist filter as everything else.

Residual gaps not addressed by these three overrides, tracked in the PR
body / issue #216 rather than silently accepted:

* The SDK's internal ``[claude, "-v"]`` version-check subprocess (run
  once, before any job data or MCP tools are involved) spawns without an
  explicit ``env=``, inheriting the parent's unfiltered environment.
* ``provider.setup_environment()`` (``ClaudeCodeAgent.apply_runtime_environment``)
  is a second, independent ``os.environ`` mutation point per-provider —
  audited as low-risk (writes only the active provider's own routing/auth
  vars) but not filtered here; a future hardening pass could route it
  through the same allowlist if a provider implementation changes.
"""

from __future__ import annotations

import logging
import os

from claude_agent_sdk import ClaudeAgentOptions

from openscientist.agent.claude_code_agent import ClaudeCodeAgent
from openscientist.airgap.env_allowlist import filtered_agent_env
from openscientist.airgap.mcp_filter import disallowed_claude_builtins
from openscientist.settings import get_settings

logger = logging.getLogger(__name__)


class AirgapClaudeCodeAgent(ClaudeCodeAgent):
    """:class:`ClaudeCodeAgent` with air-gap policy applied to 3 overridable surfaces."""

    def _build_options(self) -> ClaudeAgentOptions:
        """Disable network-capable SDK built-ins and route auth env through
        the allowlist instead of the ambient process environment.

        The base class's :meth:`ClaudeCodeAgent._apply_provider_env` writes
        ``provider.claude_sdk_env()`` into ``os.environ`` before this method
        runs; this override intentionally does not rely on that (see
        :meth:`_apply_provider_env`) and instead merges the same auth dict
        directly into ``options.env``, filtered.
        """
        options = super()._build_options()
        settings = get_settings()
        options.disallowed_tools = list(disallowed_claude_builtins(settings))
        merged_env = {**(options.env or {}), **self._provider.claude_sdk_env()}
        options.env = filtered_agent_env(merged_env, self._provider.id)
        return options

    def _build_subprocess_env(self) -> dict[str, str]:
        """Seed the MCP tools subprocess env from the air-gap allowlist
        instead of a full ``os.environ`` copy (RFC §12.1).

        Mirrors :meth:`AirgapCodexAgent._mcp_env`: filter ``os.environ``
        *before* applying the per-job overlay (job id, job dir,
        use_hypotheses, data file paths), not after — those overlay keys
        aren't credentials and aren't in :data:`BASE_AIRGAP_ENV`, so
        filtering post-overlay (e.g. by delegating to the base class's
        already-built dict) would silently drop them.
        """
        config = self._config
        env = filtered_agent_env(dict(os.environ), self._provider.id)
        env["OPENSCIENTIST_JOB_ID"] = config.job_dir.name
        env["OPENSCIENTIST_JOB_DIR"] = str(config.job_dir)
        env["OPENSCIENTIST_USE_HYPOTHESES"] = "1" if config.use_hypotheses else "0"
        if config.data_file is not None:
            env["OPENSCIENTIST_DATA_FILE"] = str(config.data_file)
        else:
            env.pop("OPENSCIENTIST_DATA_FILE", None)
        if config.data_files:
            env["OPENSCIENTIST_DATA_FILES"] = os.pathsep.join(str(p) for p in config.data_files)
        else:
            env.pop("OPENSCIENTIST_DATA_FILES", None)
        return env

    def _apply_provider_env(self) -> None:
        """No-op — see module docstring.

        The base class mutates ``os.environ`` process-wide via
        ``os.environ.update(self._provider.claude_sdk_env())``. Air-gap mode
        must not do that: ``ClaudeAgentOptions.env`` is additive to
        ``os.environ`` for the SDK's CLI subprocess, so a process-wide write
        here would leak straight through the additive merge regardless of
        what :meth:`_build_options` passes. The same auth env instead flows
        through :meth:`_build_options`'s filtered ``options.env``.
        """
