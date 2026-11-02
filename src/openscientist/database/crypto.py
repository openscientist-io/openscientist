"""Cryptographic utilities for encrypting sensitive database fields."""

import base64
import hashlib
import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from openscientist.settings import get_settings

logger = logging.getLogger(__name__)


class EncryptionError(Exception):
    """Raised when encryption or decryption fails."""


@lru_cache(maxsize=1)
def _get_fernet() -> Fernet | None:
    """Get or create a Fernet instance from settings."""
    key = get_settings().auth.token_encryption_key
    if not key:
        return None

    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError):
        pass

    # Fall back to deriving a key from the string
    key_bytes = hashlib.sha256(key.encode()).digest()
    fernet_key = base64.urlsafe_b64encode(key_bytes)
    return Fernet(fernet_key)


def encryption_available() -> bool:
    """Check if encryption is configured and available."""
    return _get_fernet() is not None


def encrypt(plaintext: str) -> str:
    fernet = _get_fernet()
    if fernet is None:
        raise EncryptionError(
            "Encryption key not configured. Set OPENSCIENTIST_SECRET_KEY environment variable."
        )

    encrypted = fernet.encrypt(plaintext.encode())
    return encrypted.decode()


def decrypt(ciphertext: str) -> str:
    fernet = _get_fernet()
    if fernet is None:
        raise EncryptionError(
            "Encryption key not configured. Set OPENSCIENTIST_SECRET_KEY environment variable."
        )

    try:
        decrypted = fernet.decrypt(ciphertext.encode())
        return decrypted.decode()
    except InvalidToken as e:
        raise EncryptionError("Failed to decrypt: invalid token or wrong encryption key") from e


def generate_key() -> str:
    """Generate a new Fernet encryption key."""
    return Fernet.generate_key().decode()
