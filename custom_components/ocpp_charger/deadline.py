"""Feature 4: compute the charging deadline.

Pure, stdlib-only helpers (mirrors charge_planner.py) so they can be unit-tested
standalone without importing Home Assistant.  The coordinator's _compute_deadline
and the ManualDeadlineText entity both consume these.
"""
from __future__ import annotations

import re
from datetime import datetime, time as dtime, timedelta, timezone

_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_hhmm(value: str) -> tuple[int, int] | None:
    """Parse 'H:MM' / 'HH:MM' → (hour, minute), or None if invalid/out of range."""
    m = _HHMM_RE.match(value.strip())
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def compute_deadline(
    now_local: datetime,
    local_tz,
    all_prices: list,
    manual_deadline_str: str = "",
    deadline_hour: int = 6,
    allow_day_charging: bool = False,
) -> datetime:
    """Return the charging deadline.

    Priority:
    1. Manual HH:MM (manual_deadline_str) – rolls to tomorrow if already past.
    2. allow_day_charging=True → end of last available price interval (+15 min),
       else now + 48h. Same logic as weekend – planner can use full price horizon.
    3. Weekday → deadline_hour:00 (rolls to tomorrow if past).
    4. Weekend → end of last available price interval (+15 min), else now + 48h.
    """
    parsed = parse_hhmm(manual_deadline_str)
    if parsed is not None:
        hour, minute = parsed
        candidate = datetime.combine(
            now_local.date(), dtime(hour, minute), tzinfo=local_tz,
        )
        if candidate <= now_local:
            candidate += timedelta(days=1)
        return candidate

    is_weekend = now_local.weekday() >= 5  # Sat=5, Sun=6
    if allow_day_charging or is_weekend:
        if all_prices:
            last_time = max(_to_utc(iv["time"]) for iv in all_prices)
            return (last_time + timedelta(minutes=15)).astimezone(local_tz)
        return now_local + timedelta(hours=48)

    today_deadline = datetime.combine(
        now_local.date(), dtime(deadline_hour, 0), tzinfo=local_tz,
    )
    if today_deadline > now_local:
        return today_deadline
    return datetime.combine(
        now_local.date() + timedelta(days=1), dtime(deadline_hour, 0), tzinfo=local_tz,
    )
