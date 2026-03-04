"""Tests for SDK message-parser patch hardening."""

import pytest
from claude_agent_sdk._errors import MessageParseError  # type: ignore[import-not-found]
from claude_agent_sdk._internal import (
    message_parser as sdk_message_parser,  # type: ignore[import-not-found]
)

from open_scientist.agent import sdk_executor


def test_unknown_message_type_returns_sentinel() -> None:
    parsed = sdk_message_parser.parse_message({"type": "rate_limit_event"})
    assert isinstance(parsed, sdk_executor._Sentinel)
    assert parsed.type == "rate_limit_event"


def test_known_message_parse_errors_are_not_swallowed() -> None:
    # Known message type with malformed shape should still raise.
    with pytest.raises(MessageParseError):
        sdk_message_parser.parse_message({"type": "assistant", "message": {}})


def test_non_dict_payload_still_raises_message_parse_error() -> None:
    with pytest.raises(MessageParseError):
        sdk_message_parser.parse_message("not-a-dict")  # type: ignore[arg-type]


def test_install_parse_message_patch_is_idempotent() -> None:
    patched = sdk_message_parser.parse_message
    sdk_executor._install_parse_message_patch()
    assert sdk_message_parser.parse_message is patched
