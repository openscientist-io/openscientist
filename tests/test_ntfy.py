"""Tests for openscientist.ntfy module."""

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
    async def test_delegates_to_send_notification(self):
        with patch("openscientist.ntfy.send_notification", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True
            result = await notify_job_started("topic1", "job-123", "My Job", "https://app.test")

        assert result is True
        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args
        assert call_kwargs.kwargs["title"] == "Job Started"
        assert "My Job" in call_kwargs.kwargs["message"]


class TestNotifyJobFailed:
    """Tests for notify_job_failed()."""

    @pytest.mark.asyncio
    async def test_truncates_long_error(self):
        with patch("openscientist.ntfy.send_notification", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True
            long_error = "E" * 200
            await notify_job_failed("topic1", "job-1", "Job", long_error, "https://app.test")

        msg = mock_send.call_args.kwargs["message"]
        assert "..." in msg
        # The error in the message should be truncated to 100 chars + "..."
        assert "E" * 101 not in msg


class TestNotifyJobCancelled:
    """Tests for notify_job_cancelled()."""

    @pytest.mark.asyncio
    async def test_with_reason(self):
        with patch("openscientist.ntfy.send_notification", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True
            await notify_job_cancelled(
                "topic1", "job-1", "Job", "user requested", "https://app.test"
            )

        msg = mock_send.call_args.kwargs["message"]
        assert "user requested" in msg

    @pytest.mark.asyncio
    async def test_without_reason(self):
        with patch("openscientist.ntfy.send_notification", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True
            await notify_job_cancelled("topic1", "job-1", "Job", None, "https://app.test")

        msg = mock_send.call_args.kwargs["message"]
        assert "was cancelled" in msg
        assert "Reason" not in msg


class TestNotifyJobStatusChange:
    """Tests for notify_job_status_change()."""

    @pytest.mark.asyncio
    async def test_disabled_returns_false(self):
        with patch(
            "openscientist.ntfy.get_user_ntfy_settings",
            new_callable=AsyncMock,
            return_value=(False, None),
        ):
            result = await notify_job_status_change(
                user_id=uuid4(),
                job_id="j1",
                job_title="Test",
                new_status="running",
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_routes_running_to_started(self):
        mock_settings = MagicMock()
        mock_settings.base_url = "https://app.test"

        with (
            patch("openscientist.ntfy.get_settings", return_value=mock_settings),
            patch(
                "openscientist.ntfy.notify_job_started",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_started,
        ):
            result = await notify_job_status_change(
                user_id=uuid4(),
                job_id="j1",
                job_title="Test",
                new_status="running",
                ntfy_topic="topic1",
            )

        assert result is True
        mock_started.assert_called_once()

    @pytest.mark.asyncio
    async def test_routes_completed(self):
        mock_settings = MagicMock()
        mock_settings.base_url = "https://app.test"

        with (
            patch("openscientist.ntfy.get_settings", return_value=mock_settings),
            patch(
                "openscientist.ntfy.notify_job_completed",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_completed,
        ):
            result = await notify_job_status_change(
                user_id=uuid4(),
                job_id="j1",
                job_title="Test",
                new_status="completed",
                ntfy_topic="topic1",
            )

        assert result is True
        mock_completed.assert_called_once()

    @pytest.mark.asyncio
    async def test_routes_failed(self):
        mock_settings = MagicMock()
        mock_settings.base_url = "https://app.test"

        with (
            patch("openscientist.ntfy.get_settings", return_value=mock_settings),
            patch(
                "openscientist.ntfy.notify_job_failed",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_failed,
        ):
            await notify_job_status_change(
                user_id=uuid4(),
                job_id="j1",
                job_title="Test",
                new_status="failed",
                error_message="boom",
                ntfy_topic="topic1",
            )

        mock_failed.assert_called_once()

    @pytest.mark.asyncio
    async def test_truncates_long_title(self):
        mock_settings = MagicMock()
        mock_settings.base_url = "https://app.test"

        with (
            patch("openscientist.ntfy.get_settings", return_value=mock_settings),
            patch(
                "openscientist.ntfy.notify_job_started",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_started,
        ):
            long_title = "A" * 100
            await notify_job_status_change(
                user_id=uuid4(),
                job_id="j1",
                job_title=long_title,
                new_status="running",
                ntfy_topic="topic1",
            )

        # The title passed to notify_job_started should be truncated
        call_args = mock_started.call_args
        passed_title = call_args[0][2]  # positional arg: title
        assert len(passed_title) <= 53  # 50 + "..."
        assert passed_title.endswith("...")

    @pytest.mark.asyncio
    async def test_unknown_status_returns_false(self):
        mock_settings = MagicMock()
        mock_settings.base_url = "https://app.test"

        with patch("openscientist.ntfy.get_settings", return_value=mock_settings):
            result = await notify_job_status_change(
                user_id=uuid4(),
                job_id="j1",
                job_title="Test",
                new_status="some_unknown_status",
                ntfy_topic="topic1",
            )

        assert result is False
