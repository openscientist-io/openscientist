"""Tests for openscientist.ntfy module."""

import contextlib
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest

from openscientist.ntfy import (
    generate_topic_for_user,
    get_subscription_url,
    notify_job_cancelled,
    notify_job_failed,
    notify_job_started,
    notify_job_status_change,
    send_notification,
)


@contextlib.contextmanager
def recorded_ntfy() -> Iterator[list[httpx.Request]]:
    """Route ntfy HTTP traffic through a recording mock transport.

    The real send_notification code runs, and the httpx client never
    reaches the network. Every request it would have sent is recorded for
    inspection.
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    with patch("httpx.AsyncClient", side_effect=factory):
        yield requests


class TestGenerateTopicForUser:
    """Tests for generate_topic_for_user()."""

    def test_format(self):
        topic = generate_topic_for_user(uuid4())
        assert topic.startswith("openscientist-")
        # 16 hex chars after "openscientist-"
        assert len(topic) == len("openscientist-") + 16

    def test_unique(self):
        user_id = uuid4()
        t1 = generate_topic_for_user(user_id)
        t2 = generate_topic_for_user(user_id)
        assert t1 != t2  # random component makes them different


class TestGetSubscriptionUrl:
    """Tests for get_subscription_url()."""

    def test_returns_ntfy_url(self):
        url = get_subscription_url("openscientist-abc123")
        assert url == "https://ntfy.sh/openscientist-abc123"


class TestSendNotification:
    """Tests for send_notification()."""

    @pytest.mark.asyncio
    async def test_success(self):
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("openscientist.ntfy.httpx.AsyncClient", return_value=mock_client):
            result = await send_notification("topic1", "Title", "Message")

        assert result is True
        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        assert call_kwargs.kwargs["headers"]["Title"] == "Title"

    @pytest.mark.asyncio
    async def test_empty_topic_returns_false(self):
        result = await send_notification("", "Title", "Message")
        assert result is False

    @pytest.mark.asyncio
    async def test_http_error_returns_false(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.HTTPError("fail"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("openscientist.ntfy.httpx.AsyncClient", return_value=mock_client):
            result = await send_notification("topic1", "Title", "Message")

        assert result is False

    @pytest.mark.asyncio
    async def test_tags_joined_in_header(self):
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("openscientist.ntfy.httpx.AsyncClient", return_value=mock_client):
            await send_notification("topic1", "T", "M", tags=["rocket", "fire"])

        headers = mock_client.post.call_args.kwargs["headers"]
        assert headers["Tags"] == "rocket,fire"

    @pytest.mark.asyncio
    async def test_click_url_in_header(self):
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("openscientist.ntfy.httpx.AsyncClient", return_value=mock_client):
            await send_notification("topic1", "T", "M", click_url="https://example.com/job/1")

        headers = mock_client.post.call_args.kwargs["headers"]
        assert headers["Click"] == "https://example.com/job/1"


class TestNotifyJobStarted:
    """Tests for notify_job_started()."""

    @pytest.mark.asyncio
    async def test_sends_started_notification(self):
        """The started notification posts the routed title and message body to the user's topic."""
        with recorded_ntfy() as requests:
            result = await notify_job_started("topic1", "job-123", "My Job", "https://app.test")

        assert result is True
        request = requests[0]
        assert request.url == "https://ntfy.sh/topic1"
        assert request.headers["Title"] == "Job Started"
        assert "My Job" in request.content.decode()
        assert request.headers["Click"] == "https://app.test/job/job-123"
        assert request.headers["Tags"] == "rocket"


class TestNotifyJobFailed:
    """Tests for notify_job_failed()."""

    @pytest.mark.asyncio
    async def test_truncates_long_error(self):
        """Errors longer than 100 chars are truncated before being sent."""
        with recorded_ntfy() as requests:
            await notify_job_failed("topic1", "job-1", "Job", "E" * 200, "https://app.test")

        request = requests[0]
        assert request.headers["Title"] == "Job Failed"
        message = request.content.decode()
        assert "..." in message
        # The error in the message should be truncated to 100 chars + "..."
        assert "E" * 101 not in message


class TestNotifyJobCancelled:
    """Tests for notify_job_cancelled()."""

    @pytest.mark.asyncio
    async def test_with_reason(self):
        """The cancellation reason is included in the sent message."""
        with recorded_ntfy() as requests:
            await notify_job_cancelled(
                "topic1", "job-1", "Job", "user requested", "https://app.test"
            )

        assert requests[0].headers["Title"] == "Job Cancelled"
        message = requests[0].content.decode()
        assert "user requested" in message

    @pytest.mark.asyncio
    async def test_without_reason(self):
        """Without a reason the message says the job was cancelled and omits a reason line."""
        with recorded_ntfy() as requests:
            await notify_job_cancelled("topic1", "job-1", "Job", None, "https://app.test")

        message = requests[0].content.decode()
        assert "was cancelled" in message
        assert "Reason" not in message


class TestNotifyJobStatusChange:
    """Tests for notify_job_status_change()."""

    @pytest.mark.asyncio
    async def test_disabled_returns_false(self):
        with patch(
            "openscientist.ntfy.get_user_ntfy_settings",
            return_value=(False, None),
        ):
            with recorded_ntfy() as requests:
                result = await notify_job_status_change(
                    user_id=uuid4(),
                    job_id="j1",
                    job_title="Test",
                    new_status="running",
                )
        assert result is False
        assert not requests

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("new_status", "expected_title", "message_fragment", "extra_kwargs"),
        [
            ("running", "Job Started", "has started running", {}),
            ("completed", "Job Completed", "has completed successfully", {}),
            ("failed", "Job Failed", "has failed: boom", {"error_message": "boom"}),
            (
                "cancelled",
                "Job Cancelled",
                "was cancelled",
                {"cancellation_reason": "user requested"},
            ),
            (
                "awaiting_feedback",
                "Feedback Requested",
                "awaiting your feedback",
                {"iteration": 3},
            ),
        ],
    )
    async def test_routes_status_to_expected_notification(
        self, new_status, expected_title, message_fragment, extra_kwargs
    ):
        """Each job status is routed to the matching notification with the right title and message."""
        mock_settings = SimpleNamespace(base_url="https://app.test")
        with (
            patch("openscientist.ntfy.get_settings", return_value=mock_settings),
            recorded_ntfy() as requests,
        ):
            result = await notify_job_status_change(
                user_id=uuid4(),
                job_id="j1",
                job_title="Test",
                new_status=new_status,
                ntfy_topic="topic1",
                **extra_kwargs,
            )

        assert result is True
        assert len(requests) == 1
        request = requests[0]
        assert request.url == "https://ntfy.sh/topic1"
        assert request.headers["Title"] == expected_title
        assert message_fragment in request.content.decode()

    @pytest.mark.asyncio
    async def test_truncates_long_title(self):
        """Titles longer than 50 chars are truncated to 50 chars plus an ellipsis before routing."""
        mock_settings = SimpleNamespace(base_url="https://app.test")
        with (
            patch("openscientist.ntfy.get_settings", return_value=mock_settings),
            recorded_ntfy() as requests,
        ):
            long_title = "A" * 100
            await notify_job_status_change(
                user_id=uuid4(),
                job_id="j1",
                job_title=long_title,
                new_status="running",
                ntfy_topic="topic1",
            )

        # The title in the message body should be truncated to 50 chars + "..."
        message = requests[0].content.decode()
        assert "A" * 50 + "..." in message
        # The full 100-char title must not be sent
        assert "A" * 51 not in message

    @pytest.mark.asyncio
    async def test_unknown_status_returns_false(self):
        mock_settings = SimpleNamespace(base_url="https://app.test")
        with (
            patch("openscientist.ntfy.get_settings", return_value=mock_settings),
            recorded_ntfy() as requests,
        ):
            result = await notify_job_status_change(
                user_id=uuid4(),
                job_id="j1",
                job_title="Test",
                new_status="some_unknown_status",
                ntfy_topic="topic1",
            )

        assert result is False
        assert not requests
