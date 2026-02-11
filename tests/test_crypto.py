"""Tests for database encryption utilities."""

import os
from unittest.mock import patch

import pytest

from shandy.database.crypto import (
    EncryptionError,
    decrypt,
    encrypt,
    encryption_available,
    generate_key,
)


class TestEncryptionAvailable:
    """Tests for encryption_available()."""

    def test_available_when_key_set(self):
        """Encryption is available when key is configured."""
        with patch.dict(os.environ, {"TOKEN_ENCRYPTION_KEY": generate_key()}):
            # Clear the cached Fernet instance
            from shandy.database import crypto

            crypto._get_fernet.cache_clear()
            assert encryption_available() is True

    def test_unavailable_when_key_not_set(self):
        """Encryption is unavailable when key is not configured."""
        with patch.dict(os.environ, {}, clear=True):
            from shandy.database import crypto

            crypto._get_fernet.cache_clear()
            # Also need to remove TOKEN_ENCRYPTION_KEY if it exists
            os.environ.pop("TOKEN_ENCRYPTION_KEY", None)
            crypto._get_fernet.cache_clear()
            assert encryption_available() is False


class TestEncryptDecrypt:
    """Tests for encrypt() and decrypt() functions."""

    @pytest.fixture(autouse=True)
    def setup_encryption_key(self):
        """Set up a test encryption key for each test."""
        test_key = generate_key()
        with patch.dict(os.environ, {"TOKEN_ENCRYPTION_KEY": test_key}):
            from shandy.database import crypto

            crypto._get_fernet.cache_clear()
            yield
            crypto._get_fernet.cache_clear()

    def test_encrypt_decrypt_roundtrip(self):
        """Encrypted data can be decrypted back to original."""
        plaintext = "my-secret-oauth-token-12345"
        encrypted = encrypt(plaintext)
        decrypted = decrypt(encrypted)
        assert decrypted == plaintext

    def test_encrypt_produces_different_output(self):
        """Encryption produces ciphertext different from plaintext."""
        plaintext = "my-secret-token"
        encrypted = encrypt(plaintext)
        assert encrypted != plaintext
        assert len(encrypted) > len(plaintext)

    def test_encrypt_different_each_time(self):
        """Fernet uses random IV, so same plaintext encrypts differently."""
        plaintext = "same-token"
        encrypted1 = encrypt(plaintext)
        encrypted2 = encrypt(plaintext)
        # Ciphertexts should differ due to random IV
        assert encrypted1 != encrypted2
        # But both should decrypt to same plaintext
        assert decrypt(encrypted1) == plaintext
        assert decrypt(encrypted2) == plaintext

    def test_decrypt_wrong_key_fails(self):
        """Decryption fails with wrong key."""
        plaintext = "secret-token"
        encrypted = encrypt(plaintext)

        # Change to different key
        from shandy.database import crypto

        crypto._get_fernet.cache_clear()
        with patch.dict(os.environ, {"TOKEN_ENCRYPTION_KEY": generate_key()}):
            crypto._get_fernet.cache_clear()
            with pytest.raises(EncryptionError, match="invalid token"):
                decrypt(encrypted)

    def test_decrypt_invalid_ciphertext_fails(self):
        """Decryption fails with invalid ciphertext."""
        with pytest.raises(EncryptionError, match="invalid token"):
            decrypt("not-valid-ciphertext")

    def test_encrypt_empty_string(self):
        """Empty string can be encrypted and decrypted."""
        plaintext = ""
        encrypted = encrypt(plaintext)
        decrypted = decrypt(encrypted)
        assert decrypted == plaintext

    def test_encrypt_unicode(self):
        """Unicode characters are handled correctly."""
        plaintext = "token-with-unicode-\u00e9\u00e0\u00fc-\U0001f600"
        encrypted = encrypt(plaintext)
        decrypted = decrypt(encrypted)
        assert decrypted == plaintext


class TestEncryptWithoutKey:
    """Tests for encryption behavior when key is not configured."""

    @pytest.fixture(autouse=True)
    def clear_key(self):
        """Ensure no encryption key is set."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("TOKEN_ENCRYPTION_KEY", None)
            from shandy.database import crypto

            crypto._get_fernet.cache_clear()
            yield
            crypto._get_fernet.cache_clear()

    def test_encrypt_without_key_raises(self):
        """Encryption raises error when key not configured."""
        with pytest.raises(EncryptionError, match="not configured"):
            encrypt("secret")

    def test_decrypt_without_key_raises(self):
        """Decryption raises error when key not configured."""
        with pytest.raises(EncryptionError, match="not configured"):
            decrypt("ciphertext")


class TestGenerateKey:
    """Tests for generate_key() function."""

    def test_generates_valid_key(self):
        """Generated key is valid for Fernet."""
        key = generate_key()
        # Key should be URL-safe base64
        assert isinstance(key, str)
        assert len(key) == 44  # Fernet key length

    def test_generates_unique_keys(self):
        """Each call generates a different key."""
        key1 = generate_key()
        key2 = generate_key()
        assert key1 != key2

    def test_generated_key_works_for_encryption(self):
        """Generated key can be used for encryption."""
        key = generate_key()
        with patch.dict(os.environ, {"TOKEN_ENCRYPTION_KEY": key}):
            from shandy.database import crypto

            crypto._get_fernet.cache_clear()
            encrypted = encrypt("test-data")
            decrypted = decrypt(encrypted)
            assert decrypted == "test-data"


class TestDerivedKey:
    """Tests for key derivation from arbitrary strings."""

    def test_arbitrary_string_as_key(self):
        """Arbitrary string is derived into valid key."""
        # Use a passphrase instead of proper Fernet key
        with patch.dict(os.environ, {"TOKEN_ENCRYPTION_KEY": "my-secret-passphrase-123"}):
            from shandy.database import crypto

            crypto._get_fernet.cache_clear()
            assert encryption_available() is True
            encrypted = encrypt("test-token")
            decrypted = decrypt(encrypted)
            assert decrypted == "test-token"

    def test_same_passphrase_same_derived_key(self):
        """Same passphrase derives to same key (deterministic)."""
        passphrase = "consistent-passphrase"

        with patch.dict(os.environ, {"TOKEN_ENCRYPTION_KEY": passphrase}):
            from shandy.database import crypto

            crypto._get_fernet.cache_clear()
            encrypted = encrypt("data")

        # Clear and re-derive with same passphrase
        with patch.dict(os.environ, {"TOKEN_ENCRYPTION_KEY": passphrase}):
            from shandy.database import crypto

            crypto._get_fernet.cache_clear()
            decrypted = decrypt(encrypted)
            assert decrypted == "data"
