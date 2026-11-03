"""The drift test: every settings variable is asked, defaulted or explicitly not asked."""

from pydantic_core import PydanticUndefined

from openscientist.cli import envfiles
from openscientist.cli.step import SETTINGS_CLASSES
from openscientist.cli.steps import STEPS
from tests.cli.conftest import REPO_ROOT

#: Every variable the settings read that no step asks and no default covers,
#: with the reason.
NOT_ASKED = {
    "GITHUB_TOKEN": "optional skill-sync token, edited in .env",
    "OPENSCIENTIST_MODEL_CONTEXT_TOKENS": "advanced context-window override, probed at job launch",
    "ANTHROPIC_CHAT_MODEL": "model override, edited in .env",
    "ANTHROPIC_SMALL_FAST_MODEL": "model override, edited in .env",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "model override, edited in .env",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "model override, edited in .env",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "model override, edited in .env",
    "AZURE_OPENAI_API_VERSION": "optional API version, edited in .env",
    "GCP_CREDENTIALS_HOST_PATH": "host mount path for agent containers, edited in .env",
    "OMP_AUTH_HOST_PATH": "omp vault path, set when the harness is omp",
    "VERTEX_REGION_CLAUDE_4_5_SONNET": "optional per-model region, edited in .env",
    "VERTEX_REGION_CLAUDE_4_5_HAIKU": "optional per-model region, edited in .env",
    "AZURE_SUBSCRIPTION_ID": "optional Azure Cost Management credentials, edited in .env",
    "AZURE_RESOURCE_GROUP": "optional Azure Cost Management credentials, edited in .env",
    "AZURE_TENANT_ID": "optional Azure Cost Management credentials, edited in .env",
    "AZURE_CLIENT_ID": "optional Azure Cost Management credentials, edited in .env",
    "AZURE_CLIENT_SECRET": "optional Azure Cost Management credentials, edited in .env",
    "PHENIX_PATH": "container path, derived from PHENIX_HOST_PATH by compose, never asked",
    "OPENSCIENTIST_AGENT_NETWORK": "container plumbing, edited in .env",
    "OPENSCIENTIST_AGENT_PLATFORM": "container plumbing, edited in .env",
    "OPENSCIENTIST_HOST_PROJECT_DIR": "host project path, edited in .env",
    "DREMIO_USER": "Berkeley Lab lakehouse credentials, optional",
    "DREMIO_PASSWORD": "Berkeley Lab lakehouse credentials, optional",
    "APP_URL": "derives from OPENSCIENTIST_BASE_URL, the wizard writes it",
}


def _settings_aliases() -> dict[str, object]:
    """Every env variable the settings classes read, alias to its field default."""
    aliases: dict[str, object] = {}
    for settings_cls in SETTINGS_CLASSES:
        for info in settings_cls.model_fields.values():
            if info.alias:
                aliases[info.alias] = info.default
    return aliases


def _example_aliases() -> set[str]:
    aliases: set[str] = set()
    for file in ("env", "db", "owner"):
        aliases.update(
            envfiles.parse_values(
                (REPO_ROOT / envfiles.EXAMPLE_FILES[file]).read_text(encoding="utf-8").splitlines()
            )
        )
    return aliases


def test_every_settings_variable_is_asked_defaulted_or_not_asked():
    asked: set[str] = set()
    for step in STEPS:
        asked |= step.declared_envs()
    aliases = _settings_aliases()
    # An empty-string default (OPENSCIENTIST_PROVIDER) is not a default the
    # wizard can offer, so it does not count as defaulted.
    defaulted = {
        alias
        for alias, default in aliases.items()
        if default is not None and default is not PydanticUndefined and default != ""
    }
    covered = asked | defaulted | _example_aliases() | set(envfiles.GENERATED_SECRETS)
    unknown = {alias: reason for alias, reason in NOT_ASKED.items() if alias not in aliases}
    unaccounted = {
        alias: "no step asks it, no default covers it, no reason given"
        for alias in aliases
        if alias not in covered and alias not in NOT_ASKED
    }
    assert not unknown, f"NOT_ASKED names variables the settings do not read: {unknown}"
    assert not unaccounted, f"settings variables nobody accounts for: {unaccounted}"
