"""
Custom SQLAlchemy column types for SHANDY.

Provides encrypted column types for storing sensitive data at rest.
"""

import logging

from sqlalchemy import Text
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from shandy.database.crypto import decrypt, encrypt, encryption_available

logger = logging.getLogger(__name__)


class EncryptedText(TypeDecorator):
    """
    SQLAlchemy column type that transparently encrypts/decrypts text.

    Data is encrypted before being stored in the database and decrypted
    when retrieved. Uses Fernet symmetric encryption.

    The underlying database column is TEXT to store the base64-encoded
    ciphertext.

    Usage:
        access_token: Mapped[str | None] = mapped_column(
            EncryptedText(),
            nullable=True,
        )

    Requires SHANDY_SECRET_KEY environment variable to be set.
    If encryption is not available:
    - Reads will attempt decryption, falling back to plaintext for migration
    - Writes will raise an error (encryption required for new data)
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        """Encrypt value before storing in database."""
        if value is None:
            return None

        if not encryption_available():
            raise ValueError(
                "Cannot store sensitive data: SHANDY_SECRET_KEY not configured. "
                "Set this environment variable to enable encryption."
            )

        return encrypt(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> str | None:
        """Decrypt value after retrieving from database."""
        if value is None:
            return None

        if not encryption_available():
            # No encryption key - return as-is (might be plaintext from before migration)
            logger.warning("SHANDY_SECRET_KEY not set - returning potentially unencrypted value")
            return value

        try:
            return decrypt(value)
        except Exception:
            # Value might be plaintext (pre-migration data)
            # Log warning and return as-is to allow reading legacy data
            logger.warning(
                "Failed to decrypt value - may be plaintext from before encryption was enabled"
            )
            return value
