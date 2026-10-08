"""``python -m redteam``: run every seeded case and print the results."""

import sys

from redteam import Result, render, run_all


def main() -> int:
    results = run_all()
    sys.stdout.write(render(results))
    offline_failures = [r for r in results if r.result not in (Result.BLOCKED, Result.NOT_RUN)]
    return 1 if offline_failures else 0


if __name__ == "__main__":
    sys.exit(main())
