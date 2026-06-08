#!/usr/bin/env python3
"""Tier-4 airgap validation — actually launch the Codex CLI subprocess
through :class:`AirgapCodexAgent`, talk to Ollama, get a real token back.

This is the empirical proof Tier 3 doesn't give you. The Tier 3 script
exercises every code path on our side; this one verifies the cross-process
contract: AirgapCodexAgent writes a config.toml that the Codex CLI parses,
launches the binary, the binary talks to the Ollama HTTP API, and a token
streams back.

Prerequisites
-------------

* Ollama running on 127.0.0.1:11434 with ``gpt-oss:120b`` (or :20b) pulled.
* Luca's fork of codex built locally; pass its binary path via
  ``OPENSCIENTIST_CODEX_BIN`` (default: ``/tmp/open-codex/codex-rs/target/
  release/codex``).
* Database URL not required — we don't persist anything; the MCP server is
  not exercised by the trivial prompt this script sends.

What this validates
-------------------

* AirgapCodexAgent constructs against the live ``openai-codex`` package.
* ``_write_codex_config`` produces a config.toml the real codex binary
  parses (the model_providers table for ``ollama``).
* ``_make_codex`` launches the binary with our env (notably CODEX_HOME
  pointing at the relocated tmpfs-substitute path).
* The codex app-server connects, ``thread_start`` returns an ``AsyncThread``.
* ``thread.run(prompt)`` returns a non-empty response from gpt-oss running
  in Ollama.

What this does NOT validate
---------------------------

* Tool calls. The prompt is reasoning-only on purpose so the MCP server
  doesn't need a real DB. Tool-use is what Luca's fork-fixes are for; that
  surface is covered by Luca's own validation, not by us.
* The full discovery loop. Tier 5.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

_DEFAULT_CODEX_BIN = "/tmp/open-codex/codex-rs/target/release/codex"
_DEFAULT_AIRGAP_CODEX_HOME_ROOT = "/tmp/airgap-codex-home"

# Trivial reasoning-only prompt so the MCP server doesn't need a real DB.
_PROBE_PROMPT = (
    "Reply with exactly one word: the result of 2 plus 2 in English. "
    "Do not use any tools."
)

# Per-turn ceiling. gpt-oss:120b is CPU-bound on most machines; a short
# reasoning-only prompt usually completes in 30-90s. Hard cap so a stuck
# turn doesn't run forever.
_TURN_TIMEOUT_S = 600


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


def _ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def _fail(msg: str) -> None:
    print(f"  ✗ {msg}", file=sys.stderr)


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="  [%(name)s] %(message)s")

    codex_bin = os.environ.get("OPENSCIENTIST_CODEX_BIN", _DEFAULT_CODEX_BIN)
    codex_root = os.environ.get(
        "OPENSCIENTIST_AIRGAP_CODEX_HOME_ROOT", _DEFAULT_AIRGAP_CODEX_HOME_ROOT
    )

    _section("Preflight")
    if not Path(codex_bin).exists():
        _fail(f"codex binary not found at {codex_bin}")
        _fail("Build it: cd /tmp/open-codex/codex-rs && cargo build --release --package codex-cli --bin codex")
        _fail("Or override: OPENSCIENTIST_CODEX_BIN=/path/to/your/codex python scripts/validate_airgap_live.py")
        return 1
    _ok(f"codex binary: {codex_bin}")

    # Ollama up?
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2) as r:
            body = r.read().decode()
    except (urllib.error.URLError, OSError) as exc:
        _fail(f"Ollama not reachable on 127.0.0.1:11434: {exc}")
        return 1
    if "gpt-oss" not in body:
        _fail("Ollama up but no gpt-oss model installed; pull one first")
        return 1
    _ok("Ollama reachable, gpt-oss installed")

    Path(codex_root).mkdir(parents=True, exist_ok=True)
    _ok(f"airgap CODEX_HOME root writable: {codex_root}")

    test_env = {
        "OPENSCIENTIST_AIR_GAPPED": "true",
        "OPENSCIENTIST_AIRGAP_LLM_ADDR": "127.0.0.1:11434",
        "OPENSCIENTIST_AIRGAP_PUBMED_ADDR": "127.0.0.1:9000",
        "OPENSCIENTIST_AIRGAP_CODEX_HOME_ROOT": codex_root,
        "OPENSCIENTIST_PROVIDER": "ollama",
        "OPENSCIENTIST_MODEL": "gpt-oss:120b",
        "OLLAMA_BASE_URL": "http://127.0.0.1:11434/v1",
        "OLLAMA_MODEL": "gpt-oss:120b",
        "OPENSCIENTIST_CODEX_BIN": codex_bin,
        "OPENSCIENTIST_SECRET_KEY": "test-secret-not-real",
        "DATABASE_URL": "postgresql+asyncpg://test@localhost/test",
        # Slow CPU-bound prefill; long per-turn ceiling.
        "OPENSCIENTIST_CODEX_TURN_TIMEOUT": str(_TURN_TIMEOUT_S),
    }

    with patch.dict(os.environ, test_env, clear=False):
        from openscientist.agent.base import AgentConfig
        from openscientist.agent.factory import get_agent
        from openscientist.airgap.codex_agent import AirgapCodexAgent
        from openscientist.settings import clear_settings_cache

        clear_settings_cache()

        _section("Agent construction (airgap+ollama via factory)")
        with tempfile.TemporaryDirectory(prefix="airgap-tier4-") as tmp:
            job_dir = Path(tmp) / "test-job-tier4"
            job_dir.mkdir()
            # Pre-create the per-job CODEX_HOME (the runner would normally
            # mount it; we're outside the runner here).
            per_job_codex_home = Path(codex_root) / job_dir.name
            per_job_codex_home.mkdir(parents=True, exist_ok=True)

            config = AgentConfig(
                job_dir=job_dir,
                system_prompt=(
                    "You are a probe in an air-gap validation harness. "
                    "Reply as briefly as possible. Do not call any tools."
                ),
            )
            agent = get_agent(config)
            assert isinstance(agent, AirgapCodexAgent), (
                f"factory returned {type(agent).__name__}; expected AirgapCodexAgent"
            )
            _ok(f"agent: {type(agent).__name__}")
            _ok(f"provider: {agent.provider.id} (model {agent.provider.codex_model_name()})")
            _ok(f"CODEX_HOME: {agent._codex_home()}")
            assert agent._codex_home().is_relative_to(Path(codex_root))
            assert not agent._codex_home().is_relative_to(job_dir)

            _section("Live turn against Ollama (this is the load-bearing step)")
            print(f"  prompt: {_PROBE_PROMPT!r}")
            print(f"  budget: {_TURN_TIMEOUT_S}s")
            print("  (gpt-oss:120b on CPU can take ~60-120s to first token; be patient)")
            print()

            start = time.perf_counter()
            try:
                result = await asyncio.wait_for(
                    agent.run_iteration(prompt=_PROBE_PROMPT, reset_session=False),
                    timeout=_TURN_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                _fail(f"Turn exceeded {_TURN_TIMEOUT_S}s without completing")
                await agent.shutdown()
                return 1
            elapsed = time.perf_counter() - start

            _section(f"Turn completed in {elapsed:.1f}s")
            if result.success:
                _ok(f"success: {result.success}")
                _ok(f"tool_calls: {result.tool_calls}")
                _ok(f"output (first 200 chars): {result.output[:200]!r}")
                if not result.output.strip():
                    _fail("output is empty — codex returned but produced no text")
                    return 1
            else:
                _fail(f"success=False; error: {result.error}")
                return 1

            _section("Token usage")
            usage = agent.token_usage
            _ok(f"input: {usage.input_tokens}  cache_read: {usage.cache_read_tokens}")
            _ok(f"output: {usage.output_tokens}  reasoning: {usage.reasoning_tokens}")

            await agent.shutdown()

    print()
    print("┌─────────────────────────────────────────────────────────────┐")
    print("│  Tier-4 airgap validation: PASSED                           │")
    print("│  AirgapCodexAgent successfully drove a live turn through    │")
    print("│  the Codex CLI against gpt-oss on Ollama.                   │")
    print("└─────────────────────────────────────────────────────────────┘")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
