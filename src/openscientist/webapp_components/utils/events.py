"""Helpers for working with NiceGUI event payloads."""

from typing import Any


def get_event_value(event: Any) -> Any:
    """Return the payload value for NiceGUI change events.

    Low-level ``update:model-value`` listeners expose the new value via
    ``event.args``. Higher-level callbacks such as ``on_value_change`` still
    expose ``event.value``. Supporting both keeps page handlers stable across
    NiceGUI callback styles.
    """
    args = getattr(event, "args", None)
    if args is not None:
        # NiceGUI ui.select may emit option dicts like
        # {'value': <id>, 'label': 'text'} via update:model-value.
        if isinstance(args, dict) and "label" in args and "value" in args:
            return args["label"]
        return args
    return getattr(event, "value", None)
