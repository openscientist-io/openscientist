"""Encrypt OAuth tokens at rest

Revision ID: b4e8d3c2f1a9
Revises: a3f9c2d1e4b7
Create Date: 2026-02-11 12:00:00.000000+00:00

This migration encrypts existing plaintext OAuth tokens using Fernet encryption.
Requires TOKEN_ENCRYPTION_KEY environment variable to be set.

If the key is not set, the migration will skip encryption and log a warning.
The EncryptedText TypeDecorator will handle this gracefully:
- Reads will attempt decryption, falling back to plaintext
- Writes will fail until the key is configured
"""

import logging
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

logger = logging.getLogger(__name__)

# revision identifiers, used by Alembic.
revision: str = "b4e8d3c2f1a9"
down_revision: Union[str, None] = "a3f9c2d1e4b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_encrypted(value: str) -> bool:
    """
    Check if a value looks like it's already Fernet-encrypted.

    Fernet tokens are base64-encoded and start with 'gAAAAA' (version byte + timestamp).
    """
    if not value:
        return False
    # Fernet tokens are base64 and typically start with 'gAAAAA'
    # They're also exactly 164+ characters for any content
    return value.startswith("gAAAAA") and len(value) >= 100


def upgrade() -> None:
    """Encrypt existing plaintext OAuth tokens."""
    # Import here to avoid circular imports and ensure env is loaded
    try:
        from shandy.database.crypto import encrypt, encryption_available
    except ImportError:
        logger.warning("Could not import crypto module. Skipping token encryption migration.")
        return

    if not encryption_available():
        logger.warning(
            "TOKEN_ENCRYPTION_KEY not set. Skipping token encryption. "
            "Set the key and re-run migration to encrypt existing tokens."
        )
        return

    # Get database connection
    conn = op.get_bind()

    # Fetch all OAuth accounts with tokens (bypass RLS for migration)
    conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))

    result = conn.execute(
        text(
            """
            SELECT id, access_token, refresh_token
            FROM oauth_accounts
            WHERE access_token IS NOT NULL OR refresh_token IS NOT NULL
            """
        )
    )
    rows = result.fetchall()

    encrypted_count = 0
    skipped_count = 0

    for row in rows:
        account_id = row[0]
        access_token = row[1]
        refresh_token = row[2]

        new_access = None
        new_refresh = None
        needs_update = False

        # Encrypt access token if present and not already encrypted
        if access_token:
            if _is_encrypted(access_token):
                skipped_count += 1
            else:
                new_access = encrypt(access_token)
                needs_update = True

        # Encrypt refresh token if present and not already encrypted
        if refresh_token:
            if _is_encrypted(refresh_token):
                pass  # Already counted in access_token check
            else:
                new_refresh = encrypt(refresh_token)
                needs_update = True

        if needs_update:
            # Build update query based on which tokens need updating
            if new_access and new_refresh:
                conn.execute(
                    text(
                        """
                        UPDATE oauth_accounts
                        SET access_token = :access, refresh_token = :refresh
                        WHERE id = :id
                        """
                    ),
                    {"access": new_access, "refresh": new_refresh, "id": account_id},
                )
            elif new_access:
                conn.execute(
                    text(
                        """
                        UPDATE oauth_accounts
                        SET access_token = :access
                        WHERE id = :id
                        """
                    ),
                    {"access": new_access, "id": account_id},
                )
            elif new_refresh:
                conn.execute(
                    text(
                        """
                        UPDATE oauth_accounts
                        SET refresh_token = :refresh
                        WHERE id = :id
                        """
                    ),
                    {"refresh": new_refresh, "id": account_id},
                )
            encrypted_count += 1

    logger.info(
        "OAuth token encryption complete: %d accounts encrypted, %d already encrypted",
        encrypted_count,
        skipped_count,
    )


def downgrade() -> None:
    """
    Decrypt OAuth tokens back to plaintext.

    WARNING: This exposes tokens in plaintext. Only use for development/testing.
    """
    try:
        from shandy.database.crypto import decrypt, encryption_available
    except ImportError:
        logger.warning("Could not import crypto module. Skipping token decryption.")
        return

    if not encryption_available():
        logger.warning("TOKEN_ENCRYPTION_KEY not set. Cannot decrypt tokens without the key.")
        return

    conn = op.get_bind()

    # Bypass RLS for migration
    conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))

    result = conn.execute(
        text(
            """
            SELECT id, access_token, refresh_token
            FROM oauth_accounts
            WHERE access_token IS NOT NULL OR refresh_token IS NOT NULL
            """
        )
    )
    rows = result.fetchall()

    decrypted_count = 0

    for row in rows:
        account_id = row[0]
        access_token = row[1]
        refresh_token = row[2]

        new_access = None
        new_refresh = None
        needs_update = False

        # Decrypt access token if it looks encrypted
        if access_token and _is_encrypted(access_token):
            try:
                new_access = decrypt(access_token)
                needs_update = True
            except Exception as e:
                logger.warning("Failed to decrypt access token for %s: %s", account_id, e)

        # Decrypt refresh token if it looks encrypted
        if refresh_token and _is_encrypted(refresh_token):
            try:
                new_refresh = decrypt(refresh_token)
                needs_update = True
            except Exception as e:
                logger.warning("Failed to decrypt refresh token for %s: %s", account_id, e)

        if needs_update:
            if new_access and new_refresh:
                conn.execute(
                    text(
                        """
                        UPDATE oauth_accounts
                        SET access_token = :access, refresh_token = :refresh
                        WHERE id = :id
                        """
                    ),
                    {"access": new_access, "refresh": new_refresh, "id": account_id},
                )
            elif new_access:
                conn.execute(
                    text(
                        """
                        UPDATE oauth_accounts
                        SET access_token = :access
                        WHERE id = :id
                        """
                    ),
                    {"access": new_access, "id": account_id},
                )
            elif new_refresh:
                conn.execute(
                    text(
                        """
                        UPDATE oauth_accounts
                        SET refresh_token = :refresh
                        WHERE id = :id
                        """
                    ),
                    {"refresh": new_refresh, "id": account_id},
                )
            decrypted_count += 1

    logger.info("OAuth token decryption complete: %d accounts decrypted", decrypted_count)
