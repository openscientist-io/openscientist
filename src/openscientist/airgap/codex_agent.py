"""Air-gap-policy-enforcing Codex agent.

:class:`AirgapCodexAgent` is a thin subclass of :class:`CodexAgent` that
overrides four protected helpers to satisfy the air-gap policy without
changing the base class's public contract. The subclass is selected by
``agent/factory.py`` when ``settings.airgap_mode=True`` and the active
provider is :class:`CodexCompatible`.

Per RFC §8.2 and §12.2:

* :meth:`_codex_home` — relocate the per-job Codex home **outside** the job
  directory so the generated ``config.toml`` and any auth file don't land in
  the export tree (`docs/AIR_GAPPED_MODE_RFC.md` §11/§12.2).
* :meth:`_mcp_env` — seed the MCP env from
  :func:`airgap.env_allowlist.filtered_agent_env` rather than
  ``os.environ``, so credentials for inactive providers don't reach the MCP
  server child process.
* :meth:`_thread_options` — turn off Codex's built-in network access
  (``network_access_enabled=False``) and web search
  (``web_search_enabled=False``) as defense-in-depth on top of the kernel
  network namespace boundary.
* :meth:`_ensure_auth` — no-op. In air-gap mode the operator (or
  :class:`JobContainerRunner`'s air-gap branch) provisions ``auth.json`` via
  a host-mounted read-only secret at ``CODEX_HOME/auth.json``; the agent
  process never copies anything from the host filesystem.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from openai_codex_sdk import ThreadOptions

from openscientist.agent.codex_agent import CodexAgent
from openscientist.airgap.env_allowlist import filtered_agent_env

logger = logging.getLogger(__name__)


# Per-job Codex home root. ``/run`` is a tmpfs on Linux, which is what
# air-gap deployments target; the per-job suffix gets appended in
# :meth:`AirgapCodexAgent._codex_home`. Operators on other platforms (the
# local-dev Mac) can override via the env var below.
_AIRGAP_CODEX_HOME_ROOT_ENV = "OPENSCIENTIST_AIRGAP_CODEX_HOME_ROOT"
_AIRGAP_CODEX_HOME_ROOT_DEFAULT = Path("/run/openscientist-codex-home")


class AirgapCodexAgent(CodexAgent):
    """:class:`CodexAgent` with air-gap policy applied to its 4 surfaces."""

    def _codex_home(self) -> Path:
        """Per-job Codex home outside the export tree.

        The base class puts this at ``job_dir/.codex/``, which means the
        Codex CLI's generated ``config.toml`` (which may contain the MCP env
        the agent assembled) ends up inside the artifact ZIP scope. Air-gap
        mode relocates it to a tmpfs path that the runner mounts per job.
        """
        root_env = os.environ.get(_AIRGAP_CODEX_HOME_ROOT_ENV)
        root = Path(root_env) if root_env else _AIRGAP_CODEX_HOME_ROOT_DEFAULT
        return root / self._config.job_dir.name

    def _mcp_env(self) -> dict[str, str]:
        """Seed the MCP env from the air-gap allowlist instead of ``os.environ``.

        Codex doesn't pass its process env to MCP server children — only the
        ``[mcp_servers.<name>.env]`` table it writes into ``config.toml``.
        That's the choke point: filtering here cuts off the cross-provider
        credential leak (§12.1).
        """
        base = filtered_agent_env(dict(os.environ), self._provider.id)
        return self._overlay_job_env(base)

    def _thread_options(self) -> ThreadOptions:
        """Disable Codex's built-in network access and web search.

        Defense-in-depth on top of the kernel network namespace (§6, §10.2):
        even if the namespace boundary were misconfigured, the Codex CLI
        itself refuses to make these calls.
        """
        return (
            super()
            ._thread_options()
            .model_copy(
                update={
                    "network_access_enabled": False,
                    "web_search_enabled": False,
                }
            )
        )

    def _ensure_auth(self) -> None:
        """No-op. Air-gap mode provisions auth via host-mounted secret.

        The base class copies ``~/.codex/auth.json`` into the per-job
        ``CODEX_HOME`` so headless ``codex exec`` can use the operator's
        ChatGPT login. In air-gap mode the agent has no business reading the
        host's ``~/.codex/``; the runner mounts the auth file as a
        read-only tmpfs secret at ``CODEX_HOME/auth.json`` before the agent
        starts. See RFC §12.2.
        """
        logger.debug("Air-gap: skipping ~/.codex/auth.json copy (host-mounted)")
