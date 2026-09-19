"""Bug 43: persist the charging start time (Bug 34's ``_charging_started_at``) across a restart.

Pure, stdlib-only helpers so they can be unit-tested standalone (like
soc_estimate.py) without importing Home Assistant.  OCPPCoordinator wraps these
in _save_state() / _load_state().

Why the start time is persisted: after a restart mid-charge it used to reset to
"now", which moved the Charge Windows block's left edge and Planned Charge Start,
and re-fired the "charging started" push.  ``_charging_started_at`` and
``_cable_session_start_notified`` are always set together, so a restored start
time also means "the start notice was already sent in this cable session".

A restored value is only trusted when it is plausible; otherwise the caller
falls back to today's behaviour (the start branch sets "now").
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# A charging start older than this is treated as belonging to an earlier cable
# session (e.g. HA was down while the cable was swapped) and is not restored.
CHARGING_START_MAX_AGE = timedelta(hours=24)


def serialize_charging_start(started_at: datetime | None) -> str | None:
    """ISO string (with UTC offset) for the Store, or None when there is no start."""
    return started_at.isoformat() if started_at is not None else None


def restore_charging_start(
    raw,
    now: datetime,
    *,
    cable_connected: bool,
    max_age: timedelta = CHARGING_START_MAX_AGE,
) -> datetime | None:
    """Return the persisted charging start as an aware UTC datetime, or None.

    None when the cable was out when it was saved, the value is missing or
    corrupt, has no timezone (ambiguous), lies in the future, or is older than
    ``max_age``.  Never raises: it runs during startup on Store data we don't control.
    """
    if not cable_connected or not isinstance(raw, str):
        return None
    try:
        started_at = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if started_at.tzinfo is None:
        return None
    started_at = started_at.astimezone(timezone.utc)
    if started_at > now or now - started_at > max_age:
        return None
    return started_at
