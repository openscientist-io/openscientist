"""ntfy.sh push notification service.

Sends push notifications to users about job status changes via ntfy.sh.
Users can subscribe to their personal topic to receive notifications on
mobile devices or desktop.
"""

import hashlib
import logging
import secrets
from uuid import UUID

import httpx
from sqlalchemy import select

from shandy.database.models import User
from shandy.database.session import AsyncSessionLocal
from shandy.settings import get_settings

logger = logging.getLogger(__name__)

NTFY_BASE_URL = "https://ntfy.sh"


async def get_user_ntfy_settings(user_id: UUID) -> tuple[bool, str | None]:
    """Get a user's ntfy settings from the database.

    Uses thread-safe session to avoid event loop conflicts when called from
    worker threads.

    Returns:
        Tuple of (ntfy_enabled, ntfy_topic). If user not found, returns (False, None).
    """
    try:
        async with AsyncSessionLocal(thread_safe=True) as session:
            stmt = select(User.ntfy_enabled, User.ntfy_topic).where(User.id == user_id)
            result = await session.execute(stmt)
            row = result.first()
            if row:
                return row.ntfy_enabled, row.ntfy_topic
    except Exception as e:
        logger.error("Failed to get ntfy settings for user %s: %s", user_id, e)

    return False, None


async def ensure_user_has_topic(user_id: UUID) -> str | None:
    """Ensure a user has an ntfy topic, creating one if needed.

    Uses thread-safe session to avoid event loop conflicts when called from
    worker threads.

    Returns:
        The user's ntfy topic, or None if unable to create/retrieve.
    """
    try:
        async with AsyncSessionLocal(thread_safe=True) as session:
            stmt = select(User).where(User.id == user_id)
            result = await session.execute(stmt)
            user = result.scalar_one_or_none()

            if not user:
                return None

            if not user.ntfy_topic:
                # Generate a new topic
                user.ntfy_topic = generate_topic_for_user(user_id)
                await session.commit()
                logger.info("Generated ntfy topic for user %s: %s", user_id, user.ntfy_topic)

            return user.ntfy_topic
    except Exception as e:
        logger.error("Failed to ensure ntfy topic for user %s: %s", user_id, e)
        return None


def generate_topic_for_user(user_id: UUID) -> str:
    """Generate a unique, hard-to-guess topic for a user.

    The topic is derived from the user ID plus a random component,
    making it unique and not easily guessable.
    """
    # Create a hash combining user ID and random bytes for uniqueness
    random_part = secrets.token_hex(8)
    hash_input = f"shandy-{user_id}-{random_part}"
    topic_hash = hashlib.sha256(hash_input.encode()).hexdigest()[:16]
    return f"shandy-{topic_hash}"


def get_subscription_url(topic: str) -> str:
    """Get the URL users can use to subscribe to notifications."""
    return f"{NTFY_BASE_URL}/{topic}"


async def send_notification(
    topic: str,
    title: str,
    message: str,
    priority: str = "default",
    tags: list[str] | None = None,
    click_url: str | None = None,
) -> bool:
    """Send a notification to a ntfy.sh topic.

    Args:
        topic: The ntfy topic to send to
        title: Notification title
        message: Notification body
        priority: Priority level (min, low, default, high, urgent)
        tags: List of emoji tags (e.g., ["white_check_mark", "rocket"])
        click_url: URL to open when notification is clicked

    Returns:
        True if notification was sent successfully, False otherwise
    """
    if not topic:
        logger.warning("No ntfy topic provided, skipping notification")
        return False

    url = f"{NTFY_BASE_URL}/{topic}"

    headers = {
        "Title": title,
        "Priority": priority,
    }

    if tags:
        headers["Tags"] = ",".join(tags)

    if click_url:
        headers["Click"] = click_url

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, content=message, headers=headers)
            response.raise_for_status()
            logger.info("Sent ntfy notification to topic %s: %s", topic, title)
            return True
    except httpx.HTTPError as e:
        logger.error("Failed to send ntfy notification: %s", e)
        return False


async def notify_job_started(topic: str, job_id: str, title: str, base_url: str) -> bool:
    """Send notification when a job starts."""
    return await send_notification(
        topic=topic,
        title="Job Started",
        message=f"Your job '{title}' has started running.",
        priority="default",
        tags=["rocket"],
        click_url=f"{base_url}/job/{job_id}",
    )


async def notify_job_completed(topic: str, job_id: str, title: str, base_url: str) -> bool:
    """Send notification when a job completes successfully."""
    return await send_notification(
        topic=topic,
        title="Job Completed",
        message=f"Your job '{title}' has completed successfully.",
        priority="default",
        tags=["white_check_mark"],
        click_url=f"{base_url}/job/{job_id}",
    )


async def notify_job_failed(topic: str, job_id: str, title: str, error: str, base_url: str) -> bool:
    """Send notification when a job fails."""
    short_error = error[:100] + "..." if len(error) > 100 else error
    return await send_notification(
        topic=topic,
        title="Job Failed",
        message=f"Your job '{title}' has failed: {short_error}",
        priority="high",
        tags=["x"],
        click_url=f"{base_url}/job/{job_id}",
    )


async def notify_job_cancelled(
    topic: str, job_id: str, title: str, reason: str | None, base_url: str
) -> bool:
    """Send notification when a job is cancelled."""
    msg = f"Your job '{title}' was cancelled."
    if reason:
        msg += f" Reason: {reason}"
    return await send_notification(
        topic=topic,
        title="Job Cancelled",
        message=msg,
        priority="default",
        tags=["warning"],
        click_url=f"{base_url}/job/{job_id}",
    )


async def notify_job_awaiting_feedback(
    topic: str, job_id: str, title: str, iteration: int, base_url: str
) -> bool:
    """Send notification when a job is awaiting user feedback."""
    return await send_notification(
        topic=topic,
        title="Feedback Requested",
        message=f"Your job '{title}' has completed iteration {iteration} and is awaiting your feedback.",
        priority="high",
        tags=["question"],
        click_url=f"{base_url}/job/{job_id}",
    )


async def notify_job_status_change(
    user_id: UUID,
    job_id: str,
    job_title: str,
    new_status: str,
    error_message: str | None = None,
    cancellation_reason: str | None = None,
    iteration: int | None = None,
    ntfy_topic: str | None = None,
) -> bool:
    """Send notification for a job status change if user has ntfy enabled.

    This is the main entry point for job notifications. It handles:
    - Checking if the user has ntfy enabled
    - Getting or creating the user's ntfy topic
    - Sending the appropriate notification based on status

    Args:
        user_id: The job owner's user ID
        job_id: The job ID
        job_title: The job title/research question
        new_status: The new job status (running, completed, failed, cancelled, awaiting_feedback)
        error_message: Error message if status is 'failed'
        cancellation_reason: Reason if status is 'cancelled'
        iteration: Current iteration if status is 'awaiting_feedback'
        ntfy_topic: The user's ntfy topic. If provided, skips database lookup.

    Returns:
        True if notification was sent, False otherwise
    """
    topic = ntfy_topic

    # Only query database if topic was not passed by caller.
    if not topic:
        enabled, topic = await get_user_ntfy_settings(user_id)
        if not enabled:
            logger.debug("ntfy disabled for user %s, skipping notification", user_id)
            return False

    # Ensure user has a topic
    if not topic:
        topic = await ensure_user_has_topic(user_id)
        if not topic:
            logger.warning("Could not get/create ntfy topic for user %s", user_id)
            return False

    settings = get_settings()
    base_url = settings.base_url

    # Truncate title for notification
    short_title = job_title[:50] + "..." if len(job_title) > 50 else job_title

    # Send appropriate notification based on status
    if new_status == "running":
        return await notify_job_started(topic, job_id, short_title, base_url)
    if new_status == "completed":
        return await notify_job_completed(topic, job_id, short_title, base_url)
    if new_status == "failed":
        return await notify_job_failed(
            topic, job_id, short_title, error_message or "Unknown error", base_url
        )
    if new_status == "cancelled":
        return await notify_job_cancelled(topic, job_id, short_title, cancellation_reason, base_url)
    if new_status == "awaiting_feedback":
        return await notify_job_awaiting_feedback(
            topic, job_id, short_title, iteration or 1, base_url
        )
    logger.debug("No notification for status: %s", new_status)
    return False
