"""Tests for text formatting/rendering UI components module."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from openscientist.webapp_components.components.text import (
    format_relative_time,
    format_uptime,
    render_justified_text,
)

# ---------------------------------------------------------------------------
# format_uptime
# ---------------------------------------------------------------------------


class TestFormatUptime:
    def test_seconds_only(self):
        assert format_uptime(30) == "30s"

    def test_zero(self):
        assert format_uptime(0) == "0s"

    def test_negative(self):
        assert format_uptime(-5) == "0s"

    def test_minutes_and_seconds(self):
        assert format_uptime(90) == "1m 30s"

    def test_exact_minutes(self):
        assert format_uptime(120) == "2m"

    def test_hours_and_minutes(self):
        assert format_uptime(8100) == "2h 15m"

    def test_exact_hours(self):
        assert format_uptime(3600) == "1h"

    def test_just_under_a_minute(self):
        assert format_uptime(59) == "59s"

    def test_just_over_a_minute(self):
        assert format_uptime(61) == "1m 1s"


# ---------------------------------------------------------------------------
# format_relative_time
# ---------------------------------------------------------------------------


class TestFormatRelativeTime:
    def test_none_returns_dash(self):
        assert format_relative_time(None) == "-"

    def test_just_now_for_current_time(self):
        assert format_relative_time(datetime.now(UTC)) == "just now"

    def test_just_now_for_recent_seconds(self):
        dt = datetime.now(UTC) - timedelta(seconds=30)
        assert format_relative_time(dt) == "just now"

    def test_future_timestamp_returns_just_now(self):
        """The implementation treats negative deltas (future timestamps) as 'just now'."""
        dt = datetime.now(UTC) + timedelta(minutes=5)
        assert format_relative_time(dt) == "just now"

    def test_minutes_ago_singular(self):
        dt = datetime.now(UTC) - timedelta(minutes=1, seconds=5)
        assert format_relative_time(dt) == "1 minute ago"

    def test_minutes_ago_plural(self):
        dt = datetime.now(UTC) - timedelta(minutes=5)
        assert format_relative_time(dt) == "5 minutes ago"

    def test_hours_ago_singular(self):
        dt = datetime.now(UTC) - timedelta(hours=1, minutes=5)
        assert format_relative_time(dt) == "1 hour ago"

    def test_hours_ago_plural(self):
        dt = datetime.now(UTC) - timedelta(hours=3)
        assert format_relative_time(dt) == "3 hours ago"

    def test_days_ago_singular(self):
        dt = datetime.now(UTC) - timedelta(days=1, hours=1)
        assert format_relative_time(dt) == "1 day ago"

    def test_days_ago_plural(self):
        dt = datetime.now(UTC) - timedelta(days=4)
        assert format_relative_time(dt) == "4 days ago"

    def test_weeks_ago_singular(self):
        dt = datetime.now(UTC) - timedelta(weeks=1, days=1)
        assert format_relative_time(dt) == "1 week ago"

    def test_weeks_ago_plural(self):
        dt = datetime.now(UTC) - timedelta(weeks=2)
        assert format_relative_time(dt) == "2 weeks ago"

    def test_months_ago_singular(self):
        dt = datetime.now(UTC) - timedelta(days=30, hours=1)
        assert format_relative_time(dt) == "1 month ago"

    def test_months_ago_plural(self):
        dt = datetime.now(UTC) - timedelta(days=90)
        assert format_relative_time(dt) == "3 months ago"

    def test_naive_datetime_is_treated_as_utc(self):
        """A naive datetime is assumed to already be in UTC (no tzinfo)."""
        naive_dt = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=10)
        assert format_relative_time(naive_dt) == "10 minutes ago"


# ---------------------------------------------------------------------------
# render_justified_text
# ---------------------------------------------------------------------------


class TestRenderJustifiedText:
    @patch("openscientist.webapp_components.components.text.ui")
    def test_empty_text_renders_nothing(self, mock_ui):
        render_justified_text("")
        mock_ui.html.assert_not_called()

    @patch("openscientist.webapp_components.components.text.ui")
    def test_renders_html_paragraph_with_default_classes(self, mock_ui):
        render_justified_text("Hello world")

        mock_ui.html.assert_called_once()
        rendered_html = mock_ui.html.call_args.args[0]
        assert '<p class="text-sm text-gray-700"' in rendered_html
        assert "text-align:justify;hyphens:auto;text-justify:inter-word;margin:0;" in rendered_html
        assert "Hello world</p>" in rendered_html

    @patch("openscientist.webapp_components.components.text.ui")
    def test_custom_text_classes_are_applied(self, mock_ui):
        render_justified_text("Some text", text_classes="text-lg text-blue-900")

        rendered_html = mock_ui.html.call_args.args[0]
        assert '<p class="text-lg text-blue-900"' in rendered_html

    @patch("openscientist.webapp_components.components.text.ui")
    def test_text_is_html_escaped(self, mock_ui):
        render_justified_text("<script>alert('xss')</script> & <b>bold</b>")

        rendered_html = mock_ui.html.call_args.args[0]
        assert "<script>" not in rendered_html
        assert "&lt;script&gt;" in rendered_html
        assert "&amp;" in rendered_html
        assert "&lt;b&gt;bold&lt;/b&gt;" in rendered_html
