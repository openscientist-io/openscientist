#!/usr/bin/env python3
"""Fail if PR coverage drops below the baseline (main) by more than a tolerance.

Reads coverage.py JSON reports (``coverage json``/``pytest --cov-report=json``)
and compares the ``totals.percent_covered`` field. Used by the CI
`coverage-delta` job; see .github/workflows/ci.yml.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def read_percent_covered(path: Path) -> float:
    data = json.loads(path.read_text())
    return float(data["totals"]["percent_covered"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--current", type=Path, required=True, help="Coverage JSON for this PR/branch"
    )
    parser.add_argument(
        "--baseline", type=Path, required=True, help="Coverage JSON from the base branch (main)"
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=0.5,
        help="Allowed coverage drop in percentage points before failing (default: 0.5)",
    )
    args = parser.parse_args()

    current_pct = read_percent_covered(args.current)

    summary_lines = [f"Coverage (this branch): {current_pct:.2f}%"]

    if not args.baseline.exists():
        summary_lines.append("No baseline coverage report found on main; skipping delta check.")
        write_summary(summary_lines)
        print("\n".join(summary_lines))
        return 0

    baseline_pct = read_percent_covered(args.baseline)
    delta = current_pct - baseline_pct

    summary_lines = [
        f"Coverage: {current_pct:.2f}% (main: {baseline_pct:.2f}%, delta: {delta:+.2f}%)",
    ]
    write_summary(summary_lines)
    print("\n".join(summary_lines))

    if delta < -args.tolerance:
        print(
            f"::error::Coverage dropped by {-delta:.2f} percentage points versus main "
            f"(tolerance {args.tolerance}).",
        )
        return 1

    return 0


def write_summary(lines: list[str]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with Path(summary_path).open("a") as f:
        for line in lines:
            f.write(line + "\n")


if __name__ == "__main__":
    sys.exit(main())
