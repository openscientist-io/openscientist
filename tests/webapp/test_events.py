"""Tests for NiceGUI event helpers."""

from types import SimpleNamespace

from openscientist.webapp_components.utils.events import get_event_value


class TestGetEventValue:
    """Tests for get_event_value helper."""

    def test_returns_args_for_model_update_events(self):
        event = SimpleNamespace(args="search term")

        assert get_event_value(event) == "search term"

    def test_falls_back_to_value_for_on_value_change_events(self):
        event = SimpleNamespace(value="search term")

        assert get_event_value(event) == "search term"

    def test_preserves_none_when_input_is_cleared(self):
        event = SimpleNamespace(args=None)

        assert get_event_value(event) is None
