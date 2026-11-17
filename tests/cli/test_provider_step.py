"""The provider step: the live check runs only for a valid provider choice."""

from openscientist.cli.steps.provider import ProviderStep


def test_after_validate_skips_the_check_without_a_provider():
    """A missing provider choice never triggers the live check."""
    assert ProviderStep().after_validate({}) == "pass"


def test_after_validate_skips_the_check_for_an_unknown_provider():
    """An unknown provider choice never triggers the live check."""
    assert ProviderStep().after_validate({"OPENSCIENTIST_PROVIDER": "bogus"}) == "pass"


def test_foundry_asks_the_azure_keys_as_optional_billing_settings():
    """The Azure Cost Management keys come after the model access keys, each
    named as an optional billing setting."""
    questions = ProviderStep().questions({"OPENSCIENTIST_PROVIDER": "foundry"})
    help_by_env = {question.env: question.help or "" for question in questions}
    billing = [env for env in help_by_env if env.startswith("AZURE_")]
    assert billing == [
        "AZURE_SUBSCRIPTION_ID",
        "AZURE_RESOURCE_GROUP",
        "AZURE_TENANT_ID",
        "AZURE_CLIENT_ID",
        "AZURE_CLIENT_SECRET",
    ]
    assert list(help_by_env)[-len(billing) :] == billing
    assert all(help_by_env[env].startswith("Optional billing setting.") for env in billing)
    assert "whole subscription" in help_by_env["AZURE_RESOURCE_GROUP"]
