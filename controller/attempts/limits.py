"""The allowances Rolando approved on 2026-10-08 (docs/limits.md sections 3 and 4).

Changing any number here changes a signed decision: it needs a new sign-off on
docs/limits.md first.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class Limits:
    attempts_per_task: int = 3
    """Including the first (G-C1)."""
    attempts_alert_at: int = 2
    fires_per_attempt: int = 2
    """The original fire plus one re-fire after a definite not-launched (G-C12)."""
    fires_per_task: int = 6
    fires_per_task_alert_at: int = 5
    fires_per_window: int = 12
    """Fires per rolling window for the pilot, and for any repository without
    its own entry in ``repo_window_caps`` (G-C13)."""
    fires_per_window_alert_at: int = 9
    repo_window_caps: tuple[tuple[str, int, int], ...] = (("rNavarrete/neon-war", 24, 18),)
    """(repository, cap, alert at): a repository's own rolling weekly cap,
    instead of ``fires_per_window`` (Rolando's decision of 2026-10-26,
    docs/limits.md section 3). Each repository is counted on its own."""
    fire_window: timedelta = timedelta(days=7)
    snapshot_max_age: timedelta = timedelta(days=7)
    usage_alert_pct: float = 60
    usage_stop_pct: float = 75
    run_alert_after: timedelta = timedelta(minutes=45)
    """Advisory only: nothing can stop a running cloud session (G-C9)."""
    run_overdue_after: timedelta = timedelta(minutes=90)
    rate_limit_default_wait: timedelta = timedelta(minutes=15)
    """Used when a 429 has no Retry-After; doubles per consecutive 429."""
    rate_limit_max_wait: timedelta = timedelta(hours=2)

    def window_cap(self, repository: str) -> tuple[int, int]:
        """(cap, alert at) for ``repository``'s fires in the rolling window."""
        for repo, cap, alert_at in self.repo_window_caps:
            if repo == repository:
                return cap, alert_at
        return self.fires_per_window, self.fires_per_window_alert_at


PILOT_LIMITS = Limits()
