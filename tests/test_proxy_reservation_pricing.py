"""Request usage bounds for proxy budget reservations."""

from __future__ import annotations

import json
from dataclasses import asdict
from unittest.mock import patch

import pytest

from openscientist.agent.base import TokenUsage
from openscientist.providers import pricing


@pytest.mark.parametrize("as_bytes", [False, True])
def test_input_estimate_counts_utf8_bytes(as_bytes: bool) -> None:
    body = json.dumps({"input": "\u00e9\u6c34", "max_output_tokens": 37}, ensure_ascii=False)
    wire_body = body.encode() if as_bytes else body

    usage = pricing.estimate_request_usage("gpt-5", wire_body)

    assert usage == TokenUsage(input_tokens=len(body.encode()), output_tokens=37)


@pytest.mark.parametrize("key", ["max_tokens", "max_output_tokens", "max_completion_tokens"])
@pytest.mark.parametrize("limit", [0, 1, 65_536, 2**31 - 1])
def test_explicit_limit_needs_no_catalog(key: str, limit: int) -> None:
    body = json.dumps({key: limit})
    with patch.object(pricing, "_get_litellm_pricing", side_effect=AssertionError):
        usage = pricing.estimate_request_usage("local-model", body)

    assert usage.output_tokens == limit


def test_multiple_limit_fields_use_the_largest() -> None:
    body = '{"max_tokens": 5, "max_output_tokens": 30, "max_completion_tokens": 20}'
    assert pricing.estimate_request_usage("gpt-5", body).output_tokens == 30


@pytest.mark.parametrize("key", ["max_tokens", "max_output_tokens", "max_completion_tokens"])
@pytest.mark.parametrize("limit", [-1, True, False, 1.5, "30", [], {}, 2**31, 10**400])
def test_malformed_explicit_limits_are_refused(key: str, limit: object) -> None:
    body = json.dumps({key: limit})
    with pytest.raises(ValueError, match="output token limit"):
        pricing.estimate_request_usage("gpt-5", body)


def test_one_valid_limit_cannot_hide_a_malformed_limit() -> None:
    with pytest.raises(ValueError, match="output token limit"):
        pricing.estimate_request_usage("gpt-5", '{"max_tokens": true, "max_output_tokens": 9}')


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_nonfinite_limit_is_refused(literal: str) -> None:
    with pytest.raises(ValueError, match="output token limit"):
        pricing.estimate_request_usage("gpt-5", '{"max_tokens": ' + literal + "}")


@pytest.mark.parametrize("body", ["{}", '{"max_tokens": null}', '{"max_output_tokens": null}'])
def test_absent_limit_uses_published_output_ceiling(body: str) -> None:
    catalog = {"gpt-5": {"max_output_tokens": 128_000, "max_tokens": 400_000}}
    with patch.object(pricing, "_get_litellm_pricing", return_value=catalog):
        usage = pricing.estimate_request_usage("gpt-5", body)

    assert usage == TokenUsage(input_tokens=len(body.encode()), output_tokens=128_000)


def test_legacy_catalog_max_tokens_is_conservative_fallback() -> None:
    with patch.object(pricing, "_get_litellm_pricing", return_value={"m": {"max_tokens": 8192}}):
        assert pricing.estimate_request_usage("m", "{}").output_tokens == 8192


@pytest.mark.parametrize(
    "model",
    [
        "claude-haiku-4-5-20251001",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "claude-haiku-4-5@20251001",
    ],
)
def test_provider_model_names_use_normalized_catalog_entry(model: str) -> None:
    catalog = {"claude-haiku-4-5": {"max_output_tokens": 64_000}}
    with patch.object(pricing, "_get_litellm_pricing", return_value=catalog):
        assert pricing.estimate_request_usage(model, "{}").output_tokens == 64_000


def test_exact_model_metadata_precedes_normalized_metadata() -> None:
    model = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    catalog = {
        model: {"max_output_tokens": 32_000},
        "claude-haiku-4-5": {"max_output_tokens": 64_000},
    }
    with patch.object(pricing, "_get_litellm_pricing", return_value=catalog):
        assert pricing.estimate_request_usage(model, "{}").output_tokens == 32_000


@pytest.mark.parametrize(
    ("model", "ceiling"),
    [
        ("claude-opus-4-6", 128_000),
        ("claude-sonnet-5-5", 128_000),
        ("claude-sonnet-4-6", 128_000),
        ("claude-haiku-4-5", 64_000),
    ],
)
def test_offline_models_have_published_output_ceilings(model: str, ceiling: int) -> None:
    with patch.object(pricing, "_get_litellm_pricing", return_value=pricing._FALLBACK_PRICING):
        assert pricing.estimate_request_usage(model, "{}").output_tokens == ceiling


@pytest.mark.parametrize("model", ["unknown", "ollama/local-model", "vllm/local-model"])
def test_unknown_output_ceiling_is_explicit_error(model: str) -> None:
    with (
        patch.object(pricing, "_get_litellm_pricing", return_value={}),
        pytest.raises(ValueError, match="output token ceiling"),
    ):
        pricing.estimate_request_usage(model, "{}")


@pytest.mark.parametrize("ceiling", [None, -1, 0, True, 1.5, "128000", 2**31])
def test_invalid_catalog_ceiling_is_not_a_reservation(ceiling: object) -> None:
    catalog = {"m": {"max_output_tokens": ceiling}}
    with (
        patch.object(pricing, "_get_litellm_pricing", return_value=catalog),
        pytest.raises(ValueError, match="output token ceiling"),
    ):
        pricing.estimate_request_usage("m", "{}")


def test_invalid_output_metadata_can_use_valid_legacy_ceiling() -> None:
    catalog = {"m": {"max_output_tokens": None, "max_tokens": 16_384}}
    with patch.object(pricing, "_get_litellm_pricing", return_value=catalog):
        assert pricing.estimate_request_usage("m", "{}").output_tokens == 16_384


@pytest.mark.parametrize("model", ["", " "])
def test_implicit_model_must_be_resolved_by_caller(model: str) -> None:
    with pytest.raises(ValueError, match="model"):
        pricing.estimate_request_usage(model, '{"max_tokens": 20}')


@pytest.mark.parametrize("body", ["[]", "null", "true", '"text"', "{", b"\xff"])
def test_invalid_request_body_is_refused(body: bytes | str) -> None:
    with pytest.raises(ValueError):
        pricing.estimate_request_usage("m", body)


def test_estimate_is_directly_priceable() -> None:
    catalog = {"m": {"input_cost_per_token": 1e-6, "output_cost_per_token": 5e-6}}
    body = '{"max_output_tokens": 10}'
    with patch.object(pricing, "_get_litellm_pricing", return_value=catalog):
        usage = pricing.estimate_request_usage("m", body)
        cost = pricing.estimate_cost_usd("m", **asdict(usage))

    assert cost == pytest.approx(len(body.encode()) * 1e-6 + 10 * 5e-6)


def test_unknown_model_with_explicit_limit_keeps_zero_price_policy() -> None:
    with patch.object(pricing, "_get_litellm_pricing", return_value={}):
        usage = pricing.estimate_request_usage("unknown", '{"max_tokens": 10}')
        assert pricing.estimate_cost_usd("unknown", **asdict(usage)) == 0.0


@pytest.mark.parametrize(("n", "best_of"), [(3, 1), (1, 4), (2, 5)])
def test_generated_candidates_are_reserved(n: int, best_of: int) -> None:
    body = json.dumps({"max_tokens": 10, "n": n, "best_of": best_of})
    usage = pricing.estimate_request_usage("gpt-5", body)
    assert usage.output_tokens == 10 * max(n, best_of)


@pytest.mark.parametrize("count", [0, -1, True, 1.5, "2", 2**31])
def test_invalid_candidate_count_is_refused(count: object) -> None:
    body = json.dumps({"max_tokens": 10, "n": count})
    with pytest.raises(ValueError):
        pricing.estimate_request_usage("gpt-5", body)


def test_candidate_product_cannot_overflow_ledger() -> None:
    with pytest.raises(ValueError):
        pricing.estimate_request_usage("gpt-5", '{"max_tokens": 2147483647, "n": 2}')
