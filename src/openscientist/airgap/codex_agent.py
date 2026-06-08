"""Air-gap-policy-enforcing Codex agent.

:class:`AirgapCodexAgent` is a thin subclass of :class:`CodexAgent` that
overrides three protected helpers to satisfy the air-gap policy without
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
* :meth:`_ensure_auth` — no-op. In air-gap mode the operator (or
  :class:`JobContainerRunner`'s air-gap branch) provisions ``auth.json`` via
  a host-mounted read-only secret at ``CODEX_HOME/auth.json``; the agent
  process never copies anything from the host filesystem.

PR #195 integration note
------------------------

The previous draft of this file had a fourth override —
:meth:`_thread_options` — that set
``ThreadOptions(network_access_enabled=False, web_search_enabled=False)``
against the ``openai-codex-sdk`` package. PR #195 swaps the SDK for the
``openai-codex`` package, which:

* drops the ``ThreadOptions`` dataclass entirely (``thread_start`` takes
  keyword arguments directly);
* gates ``web_search`` at the **fork level** so it's not advertised to
  non-OpenAI providers (Ollama, BedrockOpenAI, AzureOpenAI), which is
  every CodexCompatible provider the air-gap registry actually supports;
* has no ``network_access_enabled`` parameter — Codex's network policy is
  the host firewall + Docker network configuration (RFC §6), not a flag
  on the SDK.

So our explicit overrides for both fields are **redundant after PR #195**.
The kernel/firewall boundary (§6) and the fork's capability gate carry the
defense; the subclass keeps only the three overrides that are still load-
bearing (CODEX_HOME relocation, env allowlist, no host-auth copy).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

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
    """:class:`CodexAgent` with air-gap policy applied to its 3 overridable surfaces."""

    def _codex_home(self) -> Path:
        """Per-job Codex home outside the export tree.

        The base class puts this at ``job_dir/.codex/``, which means the
        Codex CLI's generated ``config.toml`` (which may contain the MCP env
        the agent assembled) ends up inside the artifact ZIP scope. Air-gap
        mode relocates it to a tmpfs path that the runner mounts per job.
        """
        root_env = os.environ.get(_AIRGAP_CODEX_HOME_ROOT_ENV)
        root = Path(root_env) if root_env else _AIRGAP_CODEX_HOME_ROOT_DEFAULT
        return root / self._job_dir().name

    def _mcp_env(self) -> dict[str, str]:
        """Seed the MCP env from the air-gap allowlist instead of ``os.environ``.

        Codex doesn't pass its process env to MCP server children — only the
        ``[mcp_servers.<name>.env]`` table it writes into ``config.toml``.
        That's the choke point: filtering here cuts off the cross-provider
        credential leak (§12.1).

        PR #195's :meth:`CodexAgent._mcp_env` reads ``os.environ`` directly
        and inlines the per-job overlay (no helper-extraction); we mirror
        the inlined shape against an allowlisted seed.
        """
        config = self._config
        job_dir = self._job_dir()
        env = filtered_agent_env(dict(os.environ), self._provider.id)
        env.update(
            {
                "OPENSCIENTIST_JOB_ID": job_dir.name,
                "OPENSCIENTIST_JOB_DIR": str(job_dir),
                "OPENSCIENTIST_USE_HYPOTHESES": "1" if config.use_hypotheses else "0",
            }
        )
        if config.data_file is not None:
            env["OPENSCIENTIST_DATA_FILE"] = str(config.data_file)
        if config.data_files:
            env["OPENSCIENTIST_DATA_FILES"] = os.pathsep.join(str(p) for p in config.data_files)
        return env

    def _ensure_auth(self) -> None:
        """No-op. Air-gap mode provisions auth via host-mounted secret.

        The base class copies ``~/.codex/auth.json`` into the per-job
        ``CODEX_HOME`` so headless ``codex`` can use the operator's ChatGPT
        login. In air-gap mode the agent has no business reading the host's
        ``~/.codex/``; the runner mounts the auth file as a read-only tmpfs
        secret at ``CODEX_HOME/auth.json`` before the agent starts. See
        RFC §12.2. Ollama in particular is keyless, so there's nothing
        to provision anyway — but the override stays unconditional so a
        future API-keyed CodexCompatible provider doesn't accidentally
        re-enable the host copy.
        """
        logger.debug("Air-gap: skipping ~/.codex/auth.json copy (host-mounted)")
