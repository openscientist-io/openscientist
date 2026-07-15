"""Guardrails for CI workflow configuration."""

from pathlib import Path


def test_secret_scan_uses_open_source_gitleaks_cli() -> None:
    """The CI workflow should not depend on the licensed gitleaks action."""
    workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "gitleaks/gitleaks-action@" not in workflow
    assert "ghcr.io/gitleaks/gitleaks:v8.22.1" in workflow
    assert "docker run --rm" in workflow
    assert '--log-opts="$log_opts"' in workflow
    assert "github.event.pull_request.base.sha" in workflow
