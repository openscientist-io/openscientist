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
        "PUBMED_BASE_URL",
        # Process runtime — needed for any subprocess to start.
        "PATH",
        "HOME",
        "USER",
        "LANG",
        "LC_ALL",
        "TZ",
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
