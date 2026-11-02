"""Custom SQLAlchemy column types for OpenScientist."""

import logging

from sqlalchemy import Text
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from openscientist.database.crypto import decrypt, encrypt, encryption_available

logger = logging.getLogger(__name__)


class EncryptedText(TypeDecorator[str]):
    """SQLAlchemy column type that transparently encrypts/decrypts text.

    Requires OPENSCIENTIST_SECRET_KEY. Without it, reads fall back to plaintext
    and writes raise an error.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, _dialect: Dialect) -> str | None:
        """Encrypt value before storing in database."""
        if value is None:
            return None

        if not encryption_available():
            raise ValueError(
                "Cannot store sensitive data: OPENSCIENTIST_SECRET_KEY not configured. "
                "Set this environment variable to enable encryption."
            )

        return encrypt(value)

    def process_result_value(self, value: str | None, _dialect: Dialect) -> str | None:
        """Decrypt value after retrieving from database."""
        if value is None:
            return None

        if not encryption_available():
            logger.warning(
                "OPENSCIENTIST_SECRET_KEY not set - returning potentially unencrypted value"
            )
            return value

        try:
            return decrypt(value)
        except Exception:
            # Value might be plaintext (pre-migration data)
            logger.warning(
                "Failed to decrypt value - may be plaintext from before encryption was enabled"
            )
            return value
