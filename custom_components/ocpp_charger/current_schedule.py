"""Day/night current schedule with optional Lovelace override."""
from __future__ import annotations

from datetime import datetime, time, timezone
import logging

_LOGGER = logging.getLogger(__name__)


def _parse_hhmm(value: str) -> time:
    """Parse 'HH:MM' → time object."""
    h, m = value.split(":")
    return time(int(h), int(m))


class CurrentSchedule:
    """
    Two-period day/night schedule.

    Periods are defined by their *start* times:
      - Day period:   day_start  → night_start  (exclusive)
      - Night period: night_start → day_start   (exclusive, wraps midnight)

    The schedule can be overridden: when override is active the schedule
    is bypassed and override_current_a is used instead.
    """

    def __init__(
        self,
        day_start: str   = "06:00",
        night_start: str = "22:00",
        day_current_a: float   = 6.0,
        night_current_a: float = 16.0,
        local_tz=None,
    ) -> None:
        self.day_start   = _parse_hhmm(day_start)
        self.night_start = _parse_hhmm(night_start)
        self.day_current_a   = float(day_current_a)
        self.night_current_a = float(night_current_a)
        self.local_tz = local_tz

        self.override_active: bool    = False
        self.override_current_a: float = night_current_a

    # ── Public API ────────────────────────────────────────────────────────

    def current_limit(self, now: time | None = None) -> float:
        """Return the current limit in Amperes for the given time."""
        if self.override_active:
            _LOGGER.debug("[Schedule] Override active: %.0f A", self.override_current_a)
            return self.override_current_a
        limit = self._scheduled_limit(now or self._now())
        _LOGGER.debug("[Schedule] Period=%s limit=%.0f A", self.period_name(now), limit)
        return limit

    def is_day_time(self, t: "time | None" = None) -> bool:
        """Return True if the given time falls within the day period."""
        return self._is_day(t or self._now())

    def current_limit_at(self, local_dt: "datetime") -> float:
        """Return the scheduled limit at a specific local datetime.

        Used by the charge planner to assign per-interval power.
        Override is intentionally ignored here – planning always uses
        the base schedule so the planner reflects real slot capacity.
        """
        return self._scheduled_limit(local_dt.time())

    def period_name(self, now: time | None = None) -> str:
        """Return 'Day', 'Night' or 'Override'."""
        if self.override_active:
            return "Override"
        t = now or self._now()
        return "Day" if self._is_day(t) else "Night"

    def set_override(self, active: bool, current_a: float | None = None) -> None:
        self.override_active = active
        if current_a is not None:
            self.override_current_a = current_a

    def update_settings(
        self,
        day_start: str | None = None,
        night_start: str | None = None,
        day_current_a: float | None = None,
        night_current_a: float | None = None,
    ) -> None:
        if day_start:
            self.day_start = _parse_hhmm(day_start)
        if night_start:
            self.night_start = _parse_hhmm(night_start)
        if day_current_a is not None:
            self.day_current_a = float(day_current_a)
        if night_current_a is not None:
            self.night_current_a = float(night_current_a)

    # ── Internal ──────────────────────────────────────────────────────────

    def _scheduled_limit(self, t: time) -> float:
        return self.day_current_a if self._is_day(t) else self.night_current_a

    def _is_day(self, t: time) -> bool:
        """True if t is within [day_start, night_start)."""
        ds = self.day_start
        ns = self.night_start
        if ds < ns:
            # Normal case: day 06:00–22:00, night wraps midnight
            return ds <= t < ns
        else:
            # Inverted (e.g. day_start after night_start – unusual but handled)
            return not (ns <= t < ds)

    def _now(self) -> time:
        if self.local_tz is not None:
            return datetime.now(self.local_tz).time()
        return datetime.now().time()
