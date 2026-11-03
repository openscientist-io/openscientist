"""The ChatGPT refresh-token grant.

Shared by every OpenScientist surface that holds a ChatGPT login — the codex
CLI's ``auth.json`` and the omp vault's ``openai-codex`` row — so the refresh
protocol (token endpoint, client id, body encoding, error classes) lives in
one place instead of being copied per surface.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import requests

#: The OpenAI OAuth token endpoint that issues ChatGPT access tokens.
TOKEN_ENDPOINT = "https://auth.openai.com/oauth/token"
#: The public client id the codex CLI and omp use for the ChatGPT grant.
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
#: The grant exchange is a short single call; the proxy must not hang on it.
_GRANT_TIMEOUT = 10.0


class ChatgptRefreshRejectedError(Exception):
    """The token endpoint rejected the refresh grant (HTTP 4xx). The login is
    dead; re-authentication on the host is the only recovery."""


class ChatgptRefreshTransientError(Exception):
    """The token endpoint failed transiently (HTTP 5xx or a network error, or a
    response with no usable access token). The caller serves the stale token
    and retries the refresh after a backoff."""


@dataclass(frozen=True)
class ChatgptTokens:
    """The tokens one ChatGPT refresh grant returns."""

    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    id_token: str | None = None


def jwt_expires_at(token: str) -> datetime | None:
    """The ``exp`` claim of a JWT access token, or None when it is not a
    readable JWT."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        exp = payload.get("exp")
        return datetime.fromtimestamp(exp, UTC) if isinstance(exp, (int, float)) else None
    except (ValueError, TypeError):
        return None


def refresh_chatgpt_tokens(
    refresh_token: str,
    *,
    token_endpoint: str = TOKEN_ENDPOINT,
    client_id: str = CLIENT_ID,
    encoding: Literal["json", "form"] = "json",
    timeout: float = _GRANT_TIMEOUT,
) -> ChatgptTokens:
    """Exchange a ChatGPT refresh token for a fresh access token.

    ``encoding`` is the grant body the client on the wire uses: the pinned
    codex CLI (rust-v0.163.0, ``request_chatgpt_token_refresh``) sends JSON,
    while the omp binary (v18.16.0, the openai-codex policy's
    ``refresh.token.body: "form"``) sends form-urlencoded. The endpoint
    accepts both.

    Raises:
        ChatgptRefreshRejectedError: The endpoint rejected the grant (HTTP 4xx).
        ChatgptRefreshTransientError: The endpoint failed (HTTP 5xx or a network
            error) or returned no usable access token.
    """
    grant = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
    }
    try:
        response = (
            requests.post(token_endpoint, json=grant, timeout=timeout)
            if encoding == "json"
            else requests.post(token_endpoint, data=grant, timeout=timeout)
        )
    except requests.RequestException as exc:
        raise ChatgptRefreshTransientError(str(exc)) from exc
    if 400 <= response.status_code < 500:
        raise ChatgptRefreshRejectedError(f"HTTP {response.status_code}")
    if not 200 <= response.status_code < 300:
        raise ChatgptRefreshTransientError(f"HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ChatgptRefreshTransientError("the response is not JSON") from exc
    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        raise ChatgptRefreshTransientError("the response carried no access token")
    rotated = payload.get("refresh_token")
    id_token = payload.get("id_token")
    return ChatgptTokens(
        access_token=access,
        refresh_token=rotated if isinstance(rotated, str) and rotated else None,
        expires_at=_response_expires_at(payload) or jwt_expires_at(access),
        id_token=id_token if isinstance(id_token, str) and id_token else None,
    )


def _response_expires_at(payload: dict) -> datetime | None:
    """The expiry the grant reports, or None: ``expires_in`` seconds, else an
    ``expires_at`` epoch."""
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        return datetime.now(UTC) + timedelta(seconds=expires_in)
    expires_at = payload.get("expires_at")
    if isinstance(expires_at, (int, float)) and expires_at > 0:
        return datetime.fromtimestamp(expires_at, UTC)
    return None
