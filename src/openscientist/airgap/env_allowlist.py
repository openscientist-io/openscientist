"""Active-provider-only credential allowlist for air-gapped mode.

Implements RFC §12.1. The default container env-injection path
(``ProviderSettings.get_container_env_vars()`` at ``settings.py:421`` layered
with ``JobContainerRunner._build_container_environment()`` at
``job_container/runner.py:54``) passes **every** configured provider's
credentials to every job container regardless of which provider is active.

An air-gap job running Anthropic still receives ``OPENAI_API_KEY``, Foundry
token, Bedrock keys, etc. — every one a potential exfiltration channel if any
allowlisted local service ever logs inbound credentials by mistake.

This module's :func:`filtered_agent_env` filters that env down to:

- a small base set every air-gap job needs (model selection, internal endpoint
  addresses, the few non-credential vars Python needs to run);
- the active provider's credentials only.

Everything else — credentials for other providers, ``GITHUB_TOKEN``,
``OPENSCIENTIST_SECRET_KEY``, full ``DATABASE_URL`` — is stripped (RFC §G4).

Env var names verified against ``src/openscientist/settings.py:55-142``.
Bedrock and Vertex provider entries are intentionally absent until RFC §19
OQ#2 resolves their endpoint-redirect mechanism; the egress registry
(``airgap/egress_registry.py``) already refuses those providers in air-gap
mode.
"""

from __future__ import annotations

# Env vars every airgap job needs, regardless of which provider is active.
# Process-runtime vars (PATH, HOME, USER) are included because the agent
# subprocess and the MCP server need them to start at all; air-gap mode is
# defended at the network layer (RFC §6), not by starving the process.
BASE_AIRGAP_ENV: frozenset[str] = frozenset(
    {
        # Model selection (no credential content).
        "OPENSCIENTIST_PROVIDER",
        "OPENSCIENTIST_MODEL",
        "ANTHROPIC_CHAT_MODEL",
        "ANTHROPIC_SMALL_FAST_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        # Air-gap-specific internal endpoint addresses.
        "OPENSCIENTIST_AIR_GAPPED",
        "OPENSCIENTIST_AIRGAP_LLM_ADDR",
        "OPENSCIENTIST_AIRGAP_PUBMED_ADDR",
        # RFC §7.5 opt-in for managed-LLM egress (e.g. Bedrock under AWS
        # BAA). Must flow into the agent container, otherwise the in-
        # container factory.py defaults to False and refuses to start any
        # ClaudeCompatible provider.
        "OPENSCIENTIST_AIRGAP_ALLOW_MANAGED_LLM_EGRESS",
        "PUBMED_BASE_URL",
        # Optional TCP override for the airgap Docker proxy endpoint —
        # read by docker_proxy.docker_base_url_for_airgap(). Required for
        # MCP-tool processes (openscientist_tools spawns a child python
        # subprocess that runs container_manager); without it in the
        # allowlist the agent's MCP server falls back to the conventional
        # unix:///var/run/docker.sock path, which is broken on Docker
        # Desktop hosts where bind-mounted Unix sockets refuse connect().
        # Surfaced by the first end-to-end airgap+ollama job on 2026-06-12.
        "OPENSCIENTIST_AIRGAP_DOCKER_TCP",
        # Process runtime — needed for any subprocess to start.
        "PATH",
        "HOME",
        "USER",
        "LANG",
        "LC_ALL",
        "TZ",
        # Container-side job identification (the runner sets these).
        "JOB_ID",
        "JOB_DIR",
        # Per-turn timeout for the Codex CLI subprocess (PR #195 adds this).
        # Without it in the allowlist, the runner's airgap env filter would
        # silently strip it and slow CPU-bound open-weight model runs
        # (gpt-oss-120b is hours) would be killed by the default timeout.
        "OPENSCIENTIST_CODEX_TURN_TIMEOUT",
        # Host-path signal the Codex provider's container-side config check
        # looks for. Path only, never the file contents.
        "CODEX_AUTH_HOST_PATH",
        # Sibling-container path translation (used by executor spawn).
        "OPENSCIENTIST_HOST_PROJECT_DIR",
        "OPENSCIENTIST_CONTAINER_APP_DIR",
        # Host-mounted scientific-tool paths. Paths, never credentials —
        # the data itself is mounted read-only by the runner.
        "PHENIX_PATH",
        "GOOGLE_APPLICATION_CREDENTIALS",
        # Operational necessity in PR-1 — temporarily allowed pending the
        # job-scoped least-privilege DB credential mechanism that RFC §12.1
        # calls for. The agent's discovery loop loads runtime context from
        # the DB (`_load_runtime_context`) and signs attestation records
        # with the master secret; without these vars airgap jobs cannot
        # start. The mitigation today is the per-job internal Docker
        # network (RFC §6) — the agent process can reach the DB but the
        # network boundary keeps an exfiltration via DB content infeasible.
        # Codex Review-7 BUG #B1 (fixed): previously these were stripped
        # so every airgap job failed during _load_runtime_context.
        # TODO(PR-2 / RFC §12.1): replace with a job-scoped least-privilege
        # DB role and a derived per-job key, so the master credentials
        # never reach the agent container.
        "DATABASE_URL",
        "OPENSCIENTIST_SECRET_KEY",
    }
)


# Per-provider credential allowlist. Each set lists the env vars that
# provider needs and only that provider needs; all other vars are stripped.
# Verified against ``settings.py`` field aliases.
PROVIDER_ENV_VARS: dict[str, frozenset[str]] = {
    "anthropic": frozenset(
        {
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",  # OAuth/CBORG-style auth
            "CLAUDE_CODE_OAUTH_TOKEN",  # `claude login` provided
            "ANTHROPIC_BASE_URL",
        }
    ),
    "cborg": frozenset(
        {
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
        }
    ),
    "openai": frozenset(
        {
            "OPENAI_API_KEY",
        }
    ),
    "azure-openai": frozenset(
        {
            "AZURE_OPENAI_API_KEY",
            "AZURE_OPENAI_RESOURCE",
            "AZURE_OPENAI_DEPLOYMENT",
            "AZURE_OPENAI_API_VERSION",
            "AZURE_OPENAI_STREAM_MAX_RETRIES",
        }
    ),
    "foundry": frozenset(
        {
            "ANTHROPIC_FOUNDRY_RESOURCE",
            "ANTHROPIC_FOUNDRY_BASE_URL",
            "ANTHROPIC_FOUNDRY_API_KEY",
        }
    ),
    "ollama": frozenset(
        {
            # PR #195: Ollama is keyless. OLLAMA_BASE_URL points at the
            # local server; OLLAMA_MODEL selects gpt-oss-120b (the validated
            # reference per RFC §7.4) or another open-weight model.
            "OLLAMA_BASE_URL",
            "OLLAMA_MODEL",
        }
    ),
    # Bedrock and Vertex deferred — see RFC §19 OQ#2. The egress registry
    # already refuses these providers in air-gap mode.
}


def filtered_agent_env(
    full_env: dict[str, str],
    active_provider_id: str,
) -> dict[str, str]:
    """Return only the env vars an air-gap job needs.

    Strips every credential except those for the active provider, plus the
    minimal base env (model selection, internal endpoint addresses, process
    runtime).

    Args:
        full_env: The complete env that would otherwise be passed to the job
            container (``os.environ``-derived or the result of
            ``ProviderSettings.get_container_env_vars()``).
        active_provider_id: The id of the provider this job is running
            (matches ``providers/__init__.py`` keys: ``"anthropic"``,
            ``"openai"``, ``"azure-openai"``, etc.).

    Returns:
        A new dict containing only the allowlisted entries.
    """
    allowed = BASE_AIRGAP_ENV | PROVIDER_ENV_VARS.get(active_provider_id, frozenset())
    return {k: v for k, v in full_env.items() if k in allowed}
