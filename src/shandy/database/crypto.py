"""
Cryptographic utilities for encrypting sensitive database fields.

Uses Fernet symmetric encryption from the cryptography library.
The encryption key should be stored securely in environment variables.
"""

import base64
import hashlib
import logging
import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# Environment variable for the encryption key
TOKEN_ENCRYPTION_KEY_ENV = "TOKEN_ENCRYPTION_KEY"


class EncryptionError(Exception):
    """Raised when encryption or decryption fails."""

    pass


@lru_cache(maxsize=1)
def _get_fernet() -> Fernet | None:
    """
    Get or create Fernet instance from environment key.

    The key can be either:
    - A 32-byte URL-safe base64-encoded key (Fernet format)
    - Any string that will be hashed to derive a key

    Returns:
        Fernet instance or None if no key is configured
    """
    key = os.getenv(TOKEN_ENCRYPTION_KEY_ENV)
    if not key:
        return None

    # Try to use the key directly as a Fernet key
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError):
        pass

    # Fall back to deriving a key from the string
    # Use SHA-256 to get 32 bytes, then base64 encode for Fernet
    key_bytes = hashlib.sha256(key.encode()).digest()
    fernet_key = base64.urlsafe_b64encode(key_bytes)
    return Fernet(fernet_key)


def encryption_available() -> bool:
    """Check if encryption is configured and available."""
    return _get_fernet() is not None


def encrypt(plaintext: str) -> str:
    """
    Encrypt a plaintext string.

    Args:
        plaintext: The string to encrypt

    Returns:
        Base64-encoded encrypted string

    Raises:
        EncryptionError: If encryption key is not configured
    """
    fernet = _get_fernet()
    if fernet is None:
        raise EncryptionError(
            f"Encryption key not configured. Set {TOKEN_ENCRYPTION_KEY_ENV} environment variable."
        )

    encrypted = fernet.encrypt(plaintext.encode())
    return encrypted.decode()


def decrypt(ciphertext: str) -> str:
    """
    Decrypt an encrypted string.

    Args:
        ciphertext: Base64-encoded encrypted string

    Returns:
        Decrypted plaintext string

    Raises:
        EncryptionError: If decryption fails or key is not configured
    """
    fernet = _get_fernet()
    if fernet is None:
        raise EncryptionError(
            f"Encryption key not configured. Set {TOKEN_ENCRYPTION_KEY_ENV} environment variable."
        )

    try:
        decrypted = fernet.decrypt(ciphertext.encode())
        return decrypted.decode()
    except InvalidToken as e:
        raise EncryptionError("Failed to decrypt: invalid token or wrong encryption key") from e


def generate_key() -> str:
    """
    Generate a new Fernet encryption key.

    Returns:
        URL-safe base64-encoded 32-byte key suitable for TOKEN_ENCRYPTION_KEY
    """
    return Fernet.generate_key().decode()
