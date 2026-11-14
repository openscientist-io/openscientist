"""The providers' setup questions and their validation against answers."""

import pytest

from openscientist.cli.step import settings_field_info
from openscientist.providers import provider_class, provider_ids


@pytest.mark.parametrize("provider_id", provider_ids())
def test_setup_questions_are_settings_aliases(provider_id: str):
    for question in provider_class(provider_id).setup_questions():
        assert settings_field_info(question.env) is not None, (provider_id, question.env)


def test_vllm_requires_the_served_model():
    cls = provider_class("vllm")
    assert cls.setup_answers_errors("vllm", {"VLLM_BASE_URL": "http://localhost:8000/v1"})
    assert (
        cls.setup_answers_errors(
            "vllm", {"VLLM_BASE_URL": "http://localhost:8000/v1", "OPENSCIENTIST_MODEL": "qwen3-8b"}
        )
        == []
    )


def test_anthropic_requires_a_credential():
    cls = provider_class("anthropic")
    assert cls.setup_answers_errors("anthropic", {})
    assert cls.setup_answers_errors("anthropic", {"ANTHROPIC_API_KEY": "sk-test"}) == []
    assert cls.setup_answers_errors("anthropic", {"CLAUDE_CODE_OAUTH_TOKEN": "oauth-test"}) == []


def test_anthropic_refuses_a_model_outside_its_convention():
    cls = provider_class("anthropic")
    errors = cls.setup_answers_errors(
        "anthropic", {"ANTHROPIC_API_KEY": "sk-test", "OPENSCIENTIST_MODEL": "gpt-4"}
    )
    assert errors
    assert all("OPENSCIENTIST_MODEL" in error for error in errors)


def test_azure_openai_requires_key_resource_and_model():
    cls = provider_class("azure-openai")
    assert cls.setup_answers_errors("azure-openai", {"AZURE_OPENAI_RESOURCE": "res"})
    assert (
        cls.setup_answers_errors(
            "azure-openai",
            {
                "AZURE_OPENAI_API_KEY": "key",
                "AZURE_OPENAI_RESOURCE": "res",
                "OPENSCIENTIST_MODEL": "deploy",
            },
        )
        == []
    )


def test_azure_openai_refuses_a_dated_api_version_from_an_existing_env_file():
    """The wizard's answers carry every value of an existing env file."""
    errors = provider_class("azure-openai").setup_answers_errors(
        "azure-openai",
        {
            "AZURE_OPENAI_API_KEY": "key",
            "AZURE_OPENAI_RESOURCE": "res",
            "OPENSCIENTIST_MODEL": "deploy",
            "AZURE_OPENAI_API_VERSION": "2025-04-01-preview",
        },
    )
    assert len(errors) == 1
    assert "AZURE_OPENAI_API_VERSION" in errors[0]
