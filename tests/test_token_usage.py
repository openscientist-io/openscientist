"""Tests for TokenUsage arithmetic and SDK usage parsing."""

from dataclasses import dataclass

import pytest

from openscientist.agent.base import TokenUsage
from openscientist.agent.claude_code_agent import ClaudeCodeAgent
from openscientist.agent.codex_agent import CodexAgent
from openscientist.agent.omp_agent import OmpAgent


def test_add_accumulates_all_fields() -> None:
    a = TokenUsage(
        input_tokens=1,
        output_tokens=2,
        cache_write_tokens=3,
        cache_write_1h_tokens=6,
        cache_read_tokens=4,
        reasoning_tokens=5,
    )
    b = TokenUsage(
        input_tokens=10,
        output_tokens=20,
        cache_write_tokens=30,
        cache_write_1h_tokens=60,
        cache_read_tokens=40,
        reasoning_tokens=50,
    )
    assert a + b == TokenUsage(
        input_tokens=11,
        output_tokens=22,
        cache_write_tokens=33,
        cache_write_1h_tokens=66,
        cache_read_tokens=44,
        reasoning_tokens=55,
    )


def test_iadd_accumulates_all_fields() -> None:
    acc = TokenUsage()
    acc += TokenUsage(
        input_tokens=1,
        output_tokens=2,
        cache_write_tokens=3,
        cache_write_1h_tokens=6,
        cache_read_tokens=4,
        reasoning_tokens=5,
    )
    acc += TokenUsage(
        input_tokens=10,
        output_tokens=20,
        cache_write_tokens=30,
        cache_write_1h_tokens=60,
        cache_read_tokens=40,
        reasoning_tokens=50,
    )
    assert acc == TokenUsage(
        input_tokens=11,
        output_tokens=22,
        cache_write_tokens=33,
        cache_write_1h_tokens=66,
        cache_read_tokens=44,
        reasoning_tokens=55,
    )


def test_anthropic_payload_maps_to_additive_fields() -> None:
    """Anthropic's usage shape is already additive; only field-name remap is needed."""
    usage = {
        "input_tokens": 100,
        "output_tokens": 200,
        "cache_creation_input_tokens": 10,
        "cache_read_input_tokens": 20,
    }
    assert ClaudeCodeAgent._usage_from_payload(usage) == TokenUsage(
        input_tokens=100,
        output_tokens=200,
        cache_write_tokens=10,
        cache_read_tokens=20,
        reasoning_tokens=0,
    )


def test_anthropic_object_payload_maps_to_additive_fields() -> None:
    class _UsageObj:
        input_tokens = 100
        output_tokens = 200
        cache_creation_input_tokens = 10
        cache_read_input_tokens = 20

    assert ClaudeCodeAgent._usage_from_payload(_UsageObj()) == TokenUsage(
        input_tokens=100,
        output_tokens=200,
        cache_write_tokens=10,
        cache_read_tokens=20,
        reasoning_tokens=0,
    )


def test_anthropic_payload_reasoning_is_zero() -> None:
    """Anthropic's API does not expose extended-thinking tokens separately."""
    usage = {"input_tokens": 100, "output_tokens": 200}
    assert ClaudeCodeAgent._usage_from_payload(usage).reasoning_tokens == 0


def test_anthropic_one_hour_cache_writes_split_out_of_the_flat_total() -> None:
    """`cache_creation_input_tokens` is the sum of the per-lifetime counts, and a
    one-hour write costs twice base input against 1.25x for five minutes, so the two
    tiers cannot share a bucket."""
    usage = {
        "input_tokens": 100,
        "output_tokens": 200,
        "cache_creation_input_tokens": 30,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 12,
            "ephemeral_1h_input_tokens": 18,
        },
        "cache_read_input_tokens": 20,
    }
    assert ClaudeCodeAgent._usage_from_payload(usage) == TokenUsage(
        input_tokens=100,
        output_tokens=200,
        cache_write_tokens=12,
        cache_write_1h_tokens=18,
        cache_read_tokens=20,
    )


def test_anthropic_object_payload_splits_the_one_hour_tier() -> None:
    class _Creation:
        ephemeral_5m_input_tokens = 12
        ephemeral_1h_input_tokens = 18

    class _UsageObj:
        input_tokens = 100
        output_tokens = 200
        cache_creation_input_tokens = 30
        cache_read_input_tokens = 20
        cache_creation = _Creation()

    assert ClaudeCodeAgent._usage_from_payload(_UsageObj()) == TokenUsage(
        input_tokens=100,
        output_tokens=200,
        cache_write_tokens=12,
        cache_write_1h_tokens=18,
        cache_read_tokens=20,
    )


def test_one_hour_count_is_clamped_to_the_write_total() -> None:
    """The write buckets must stay disjoint and sum to the total even if a payload
    disagrees with itself, because the whole-payload accounting relies on it."""
    usage = {
        "cache_creation_input_tokens": 10,
        "cache_creation": {"ephemeral_1h_input_tokens": 999},
    }
    mapped = ClaudeCodeAgent._usage_from_payload(usage)
    assert (mapped.cache_write_tokens, mapped.cache_write_1h_tokens) == (0, 10)


def test_omp_one_hour_cache_writes_split_out_of_cache_write() -> None:
    """omp reports `cacheWrite` as a total and breaks it down by cache lifetime in
    `cttl`, which is its rendering of Anthropic's `cache_creation`."""
    message = {
        "usage": {
            "input": 8,
            "output": 4,
            "cacheRead": 1,
            "cacheWrite": 505,
            "cttl": {"ephemeral1h": 505},
        }
    }
    assert OmpAgent._usage_from_message(message) == TokenUsage(
        input_tokens=8,
        output_tokens=4,
        cache_read_tokens=1,
        cache_write_tokens=0,
        cache_write_1h_tokens=505,
    )


def test_omp_cache_writes_without_cttl_stay_in_the_short_lived_bucket() -> None:
    mapped = OmpAgent._usage_from_message({"usage": {"input": 8, "output": 4, "cacheWrite": 505}})
    assert (mapped.cache_write_tokens, mapped.cache_write_1h_tokens) == (505, 0)


@dataclass(frozen=True)
class _CodexBreakdown:
    """The SDK's ``TokenUsageBreakdown``, whose counts nest."""

    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int


# Real numbers from tests/fixtures/transcripts/codex/exec_jsonl/agent_message_only.jsonl.gz,
# a one-character reply: 40 of the 42 output tokens are reasoning.
_CODEX_PAYLOAD = _CodexBreakdown(
    input_tokens=21_421,
    cached_input_tokens=3_456,
    output_tokens=42,
    reasoning_output_tokens=40,
    total_tokens=21_463,
)

# Anthropic reports disjoint buckets and carries no total of its own, so the payload's
# total is the sum of the counts it sends. `cache_creation` breaks
# `cache_creation_input_tokens` down by lifetime rather than adding to it, so it does
# not enter the total.
_ANTHROPIC_PAYLOAD = {
    "input_tokens": 100,
    "output_tokens": 200,
    "cache_creation_input_tokens": 30,
    "cache_creation": {"ephemeral_5m_input_tokens": 12, "ephemeral_1h_input_tokens": 18},
    "cache_read_input_tokens": 20,
}
_ANTHROPIC_TOTAL = 100 + 200 + 30 + 20

# Real numbers from tests/fixtures/transcripts/omp/agent_end_messages.json, including
# the `cttl` breakdown showing all 505 cache writes went to a one-hour cache. omp emits
# no "reasoning" key, so that bucket stays 0 on this path.
_OMP_PAYLOAD = {
    "usage": {
        "input": 10,
        "output": 104,
        "cacheRead": 34_488,
        "cacheWrite": 505,
        "cttl": {"ephemeral1h": 505},
        "totalTokens": 35_107,
    }
}
_OMP_TOTAL = 35_107  # omp's own totalTokens for the payload above


def _bucket_sum(usage: TokenUsage) -> int:
    return (
        usage.input_tokens
        + usage.output_tokens
        + usage.cache_write_tokens
        + usage.cache_write_1h_tokens
        + usage.cache_read_tokens
        + usage.reasoning_tokens
    )


@pytest.mark.parametrize(
    ("mapped", "payload_total"),
    [
        pytest.param(
            ClaudeCodeAgent._usage_from_payload(_ANTHROPIC_PAYLOAD),
            _ANTHROPIC_TOTAL,
            id="claude_code",
        ),
        pytest.param(
            CodexAgent._usage_from_breakdown(_CODEX_PAYLOAD),
            _CODEX_PAYLOAD.total_tokens,
            id="codex",
        ),
        pytest.param(
            OmpAgent._usage_from_message(_OMP_PAYLOAD),
            _OMP_TOTAL,
            id="omp",
        ),
    ],
)
def test_mapped_buckets_sum_to_the_payload_total(mapped: TokenUsage, payload_total: int) -> None:
    """Every backend mapper must conserve tokens.

    ``TokenUsage`` promises non-overlapping buckets, so a mapper that leaves a
    nested sub-count inside its parent bills those tokens twice. That is not
    visible in a per-field assertion, which is how codex shipped billing its
    reasoning tokens as output as well.
    """
    assert _bucket_sum(mapped) == payload_total
