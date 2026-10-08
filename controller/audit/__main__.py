"""``python3 -m controller.audit [--out FILE]``: the full control check.

Runs every test and red-team case the control list names, checks each
control's evidence against the governance map, prints the table (and writes
it to FILE if given) and exits 0 only when every control is
observed. Read-only: it fires nothing, reads no ledger and touches no network.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from controller.audit.audit import render, run_audit
from controller.audit.controls import CONTROLS

ROOT = Path(__file__).resolve().parents[2]
MAP = ROOT / "docs" / "governance-map.md"


def _revision() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown (not a git checkout)"
    dirty = subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    return out.stdout.strip() + (" plus uncommitted changes" if dirty else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m controller.audit")
    parser.add_argument("--out", help="also write the table to this file")
    parser.add_argument(
        "--autofix",
        action="store_true",
        help="instead, read every worker PR on the pilot repo for signs auto-fix ran",
    )
    args = parser.parse_args(argv)
    if args.autofix:
        return _autofix()
    report = run_audit(CONTROLS, MAP.read_text(encoding="utf-8"))
    text = render(report, revision=_revision())
    sys.stdout.write(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 0 if report.all_observed else 1


def _autofix() -> int:
    from controller.audit.autofix import check_autofix
    from controller.loop.collect import GhApi, GitHubUnreadable
    from controller.recovery import PILOT_REPO
    from controller.recovery.recovery import WORKER_LOGINS

    try:
        report = check_autofix(GhApi(), PILOT_REPO, WORKER_LOGINS)
    except GitHubUnreadable as e:
        print(f"Couldn't read the pilot repo's PRs: {e}")
        return 1
    print(f"Worker PRs checked: {', '.join(f'#{n}' for n in report.checked) or 'none'}")
    for f in report.findings:
        print(f"  - {f}")
    print("No sign of auto-fix on any of them." if report.clean else "Auto-fix check: NOT clean.")
    return 0 if report.clean else 1


if __name__ == "__main__":
    sys.exit(main())
