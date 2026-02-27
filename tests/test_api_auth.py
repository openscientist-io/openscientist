"""Tests for API key authentication dependency logic."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from shandy.api import auth as api_auth
from shandy.database.models import APIKey, User
from tests.helpers import fake_admin_session


class _FakeResult:
    """Minimal SQLAlchemy-result shim for scalar_one_or_none calls."""

    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


@pytest.mark.asyncio
async def test_api_key_authentication_queries_by_secret_hash(monkeypatch: pytest.MonkeyPatch):
    """Authentication query should key off hashed secret, not ambiguous key names."""
    user = User(email="u2@example.com", name="User 2")
    user.id = uuid4()

    api_key = APIKey(user_id=user.id, name="shared-name", key_hash="hashed-secret", is_active=True)
    api_key.id = uuid4()

    fake_session = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _FakeResult(api_key),  # key lookup
                _FakeResult(None),  # update usage
                _FakeResult(user),  # user lookup
            ]
        ),
        commit=AsyncMock(),
    )

    monkeypatch.setattr(api_auth, "get_admin_session", fake_admin_session(fake_session))
    monkeypatch.setattr(api_auth, "hash_secret", lambda _secret: "hashed-secret")

    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials="shared-name:real-secret",
    )

    resolved_user = await api_auth.get_current_user_from_api_key(credentials=credentials)
    assert resolved_user.id == user.id

    lookup_stmt = fake_session.execute.call_args_list[0].args[0]
    where_sql = str(lookup_stmt.whereclause)
    assert "api_keys.key_hash" in where_sql
    assert "api_keys.is_active" in where_sql


@pytest.mark.asyncio
async def test_api_key_authentication_rejects_name_mismatch(monkeypatch: pytest.MonkeyPatch):
    """Name and secret components must belong to the same key record."""
    api_key = APIKey(
        user_id=uuid4(), name="expected-name", key_hash="hashed-secret", is_active=True
    )
    api_key.id = uuid4()

    fake_session = SimpleNamespace(
        execute=AsyncMock(side_effect=[_FakeResult(api_key)]),
        commit=AsyncMock(),
    )

    monkeypatch.setattr(api_auth, "get_admin_session", fake_admin_session(fake_session))
    monkeypatch.setattr(api_auth, "hash_secret", lambda _secret: "hashed-secret")

    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials="wrong-name:real-secret",
    )

    with pytest.raises(HTTPException) as exc_info:
        await api_auth.get_current_user_from_api_key(credentials=credentials)

    assert exc_info.value.status_code == 401
    fake_session.commit.assert_not_called()
