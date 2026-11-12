"""Account access shared by cookie and API key authentication."""

from datetime import UTC, datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.sql.elements import ColumnElement

from openscientist.database.models import ReviewToken, User


def user_access_allowed() -> ColumnElement[bool]:
    """Require an active account and no revoked or expired reviewer grant."""
    denied_review = (
        select(ReviewToken.id)
        .where(
            ReviewToken.redeemed_by_id == User.id,
            or_(
                ReviewToken.is_active.is_(False),
                ReviewToken.expires_at <= datetime.now(UTC),
            ),
        )
        .exists()
    )
    return and_(User.is_active.is_(True), ~denied_review)
