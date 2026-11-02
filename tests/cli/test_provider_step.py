"""The provider step: the live check runs only for a valid provider choice."""

from openscientist.cli.steps.provider import ProviderStep


def test_after_validate_skips_the_check_without_a_provider():
    """A missing provider choice never triggers the live check."""
    assert ProviderStep().after_validate({}) == "pass"


def test_after_validate_skips_the_check_for_an_unknown_provider():
    """An unknown provider choice never triggers the live check."""
    assert ProviderStep().after_validate({"OPENSCIENTIST_PROVIDER": "bogus"}) == "pass"
