"""Sentinel for the air-gap chat guard in :func:`openscientist.job_chat.send_chat_message`.

Codex Review-6 BUG (fixed): Luca's PR #195 routed in-page chat through
``get_agent()``, which in air-gap mode returns ``AirgapCodexAgent``. That
agent's ``_codex_home()`` reads ``OPENSCIENTIST_AIRGAP_CODEX_HOME_ROOT``
to find a per-job tmpfs mount the runner provisions ONLY for agent
containers — the web process where chat runs has no such mount. Chat
would crash on first message. RFC §2's PR-1 stance is to disable chat
in airgap mode; this test pins that the guard fires.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_chat_refused_in_airgap_mode() -> None:
    from openscientist.job_chat import send_chat_message

    settings = SimpleNamespace(airgap=SimpleNamespace(enabled=True))
    with patch("openscientist.settings.get_settings", return_value=settings):
        with pytest.raises(RuntimeError, match="air-gap mode"):
            await send_chat_message(
                session=None,  # type: ignore[arg-type]  # never reached
                job_id=uuid4(),
                message="test",
                job_dir=Path("/tmp"),
            )


@pytest.mark.asyncio
async def test_chat_does_not_fire_airgap_guard_when_airgap_disabled() -> None:
    # Regression sentinel: the guard must not interfere with non-airgap.
    # We force a failure downstream and assert the failure ISN'T the
    # airgap guard's RuntimeError. Any other exception is fine — we just
    # need to know the guard didn't block.
    from openscientist.job_chat import send_chat_message

    settings = SimpleNamespace(airgap=SimpleNamespace(enabled=False))

    with patch("openscientist.settings.get_settings", return_value=settings):
        try:
            await send_chat_message(
                session=None,  # type: ignore[arg-type]
                job_id=uuid4(),
                message="test",
                job_dir=Path("/tmp/does-not-exist"),
            )
        except RuntimeError as e:
            assert "air-gap mode" not in str(e), "Airgap guard fired despite airgap.enabled=False"
        except Exception:
            # Any non-RuntimeError (or RuntimeError without 'air-gap mode')
            # is fine — we ONLY care that the airgap guard didn't fire.
            pass
