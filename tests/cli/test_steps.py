"""The steps' validation of the wizard's answers."""

from openscientist.cli.steps import AddressStep, ConcurrencyStep, ProviderStep
from openscientist.providers import provider_ids


def test_address_refuses_a_base_url_that_is_not_http():
    errors = AddressStep().validate({"OPENSCIENTIST_BASE_URL": "ftp://example.com"})
    assert len(errors) == 1
    assert "OPENSCIENTIST_BASE_URL" in errors[0]


def test_address_accepts_a_valid_url_and_ports():
    values = {
        "OPENSCIENTIST_BASE_URL": "https://example.com",
        "PORT": "8080",
        "POSTGRES_PORT": "5434",
    }
    assert AddressStep().validate(values) == []


def test_address_refuses_a_bind_address_that_is_not_an_ip():
    errors = AddressStep().validate({"OPENSCIENTIST_BIND_ADDRESS": "not-an-ip"})
    assert len(errors) == 1
    assert "OPENSCIENTIST_BIND_ADDRESS" in errors[0]


def test_address_accepts_loopback_and_public_bind_addresses():
    for value in ("127.0.0.1", "0.0.0.0", "::1", "localhost", ""):
        assert AddressStep().validate({"OPENSCIENTIST_BIND_ADDRESS": value}) == []


def test_address_reports_a_port_that_is_not_a_number():
    errors = AddressStep._valid_port("PORT", "abc")
    assert len(errors) == 1
    assert "PORT" in errors[0]


def test_address_reports_a_port_outside_the_tcp_range():
    assert AddressStep._valid_port("PORT", "65535") == []
    errors = AddressStep._valid_port("PORT", "70000")
    assert len(errors) == 1
    assert "PORT" in errors[0]


def test_concurrency_accepts_a_positive_job_count():
    assert ConcurrencyStep().validate({"OPENSCIENTIST_MAX_CONCURRENT_JOBS": "4"}) == []


def test_concurrency_accepts_an_unanswered_question():
    assert ConcurrencyStep().validate({}) == []


def test_concurrency_reports_a_job_count_that_is_not_a_number():
    errors = ConcurrencyStep().validate({"OPENSCIENTIST_MAX_CONCURRENT_JOBS": "four"})
    assert len(errors) == 1
    assert "OPENSCIENTIST_MAX_CONCURRENT_JOBS" in errors[0]


def test_concurrency_reports_a_job_count_below_one():
    assert ConcurrencyStep().validate({"OPENSCIENTIST_MAX_CONCURRENT_JOBS": "1"}) == []
    errors = ConcurrencyStep().validate({"OPENSCIENTIST_MAX_CONCURRENT_JOBS": "0"})
    assert len(errors) == 1
    assert "OPENSCIENTIST_MAX_CONCURRENT_JOBS" in errors[0]


def test_provider_validation_requires_a_provider():
    errors = ProviderStep().validate({})
    assert len(errors) == 1
    assert "OPENSCIENTIST_PROVIDER" in errors[0]


def test_provider_validation_names_the_valid_options_for_an_unknown_id():
    errors = ProviderStep().validate({"OPENSCIENTIST_PROVIDER": "no-such-provider"})
    assert len(errors) == 1
    assert "no-such-provider" in errors[0]
    assert any(option in errors[0] for option in provider_ids())
