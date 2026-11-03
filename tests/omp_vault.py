"""Build a temp omp vault (agent.db) with fake credentials, for the tests.

The schema is the real vault's (verified on a host ``~/.omp/agent/agent.db``);
no real values are used or stored.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = Path(__file__).with_name("fixtures") / "omp_vault_schema.sql"


def make_jwt(exp: datetime | None) -> str:
    """A signed-looking JWT whose ``exp`` claim the signing reads."""
    payload: dict[str, object] = (
        {"exp": int(exp.timestamp())} if exp is not None else {"sub": "acct-1"}
    )

    def b64url(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{b64url({'alg': 'RS256', 'typ': 'JWT'})}.{b64url(payload)}.fakesig"


def make_vault(
    vault_dir: Path,
    credentials: dict[str, dict],
    *,
    now_s: int | None = None,
) -> Path:
    """Write ``agent.db`` into ``vault_dir`` with one oauth row per provider.

    ``credentials`` maps a vault provider name (``anthropic``, ``github-copilot``,
    ``openai-codex``) to its ``data`` JSON blob. Returns the database path.
    """
    now_s = now_s if now_s is not None else int(datetime.now(UTC).timestamp())
    vault_dir.mkdir(parents=True, exist_ok=True)
    db = vault_dir / "agent.db"
    if db.exists():
        db.unlink()
    schema = "\n".join(
        line for line in _SCHEMA.read_text().splitlines() if "sqlite_sequence" not in line
    )
    con = sqlite3.connect(db)
    try:
        con.executescript(schema)
        con.execute("INSERT INTO auth_schema_version (id, version) VALUES (1, 8)")
        con.execute("INSERT INTO schema_version (version) VALUES (5)")
        con.execute("INSERT INTO auth_change_revision (id, revision) VALUES (1, 0)")
        for provider, data in credentials.items():
            con.execute(
                "INSERT INTO auth_credentials "
                "(provider, credential_type, data, created_at, updated_at) "
                "VALUES (?, 'oauth', ?, ?, ?)",
                (provider, json.dumps(data), now_s, now_s),
            )
        con.commit()
    finally:
        con.close()
    return db


def now_ms() -> int:
    return int(time.time() * 1000)
