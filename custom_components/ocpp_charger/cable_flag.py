"""Bug 44: persist the ``_cable_was_available`` flag (Bug 13A/38) across a restart.

Pure, stdlib-only helper so it can be unit-tested standalone (like charging_start.py)
without importing Home Assistant.  OCPPCoordinator wraps it in _save_state() /
_load_state().

The flag means "a genuine ``Available`` has been seen since the last cable connect", and
it is what lets the next ``Preparing`` count as a genuine connect (which resets the cable
session accumulators) instead of a Garo 15-min reset.  It used to be in-memory only, so
after an integration reload it was False again; Garo doesn't resend StatusNotification on
reconnect, no ``Available`` followed, and the next real connect was misclassified as a
Garo reset - the previous cable session's energy was never cleared.

It cannot be derived from ``cable_connected`` alone: that is only True for Preparing /
Charging / SuspendedEV / SuspendedEVSE / Finishing (ocpp_client.py), so Faulted /
Unavailable with the cable still plugged in also read as "cable out".
"""
from __future__ import annotations


def restore_cable_was_available(saved, *, cable_connected: bool, current: bool = False) -> bool:
    """Return the flag to use after loading the Store.

    ``saved`` is the persisted flag; ``cable_connected`` is the cable state saved with it;
    ``current`` is the in-memory flag when the Store is loaded (~10 s after start).

    * ``current`` True wins: a genuine ``Available`` already arrived this run and a saved
      False (cable was plugged in when saved) must not overwrite it.
    * A saved real ``bool`` is trusted as is.
    * Otherwise (Store from before Bug 44, or a non-bool value): cable out when saved -> True
      (the next Preparing is a genuine connect), cable plugged in -> False (mid-session; Bug 38).
    """
    if current:
        return True
    if isinstance(saved, bool):
        return saved
    return not cable_connected
