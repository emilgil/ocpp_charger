"""Bug 52 – manuell dagladdnings-override (Bug 26) nollställs vid äkta urkoppling.

Overriden sparades för alltid i Store och nollades aldrig, så ett tillfälligt "slå på för att jämföra" låg kvar
tills någon slog av den. Nu återgår dagladdning till veckoschemat när kabeln dras ur (övergång till Available,
Bug 51). Se bug52.md. Riktig OCPPCoordinator (tests/coordinator_harness.py).
    /mnt/c/temp/github/claude/venv/bin/python tests/test_bug52.py
Utan Home Assistant hoppas de över (SKIP).
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coordinator_harness as h  # noqa: E402


def _module():
    h.coordinator_class()
    import custom_components.ocpp_charger as mod
    return mod


def _coordinator(store=None):
    c = h.make_coordinator(store)
    c.notifier = MagicMock()
    return c


def _run(fn, *args, **kwargs):
    async def go():
        result = fn(*args, **kwargs)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return result
    return asyncio.run(go())


def _step(mod, c, status):
    c.ocpp.state.connector_status = status
    with patch.object(mod, "async_call_later"):
        _run(c._check_notify_events)


def _overridden(c, *, auto_allows_day):
    c._day_charging_manual_override = True
    c.allow_day_charging = not auto_allows_day
    c._compute_allow_day_charging = lambda now=None: auto_allows_day


def test_genuine_disconnect_clears_the_override_and_restores_the_schedule():
    mod = _module()
    c = _coordinator()
    _overridden(c, auto_allows_day=False)
    c.allow_day_charging = True            # användaren slog på dagladdning på en vardag
    c._last_connector_status_notify = "Charging"
    _step(mod, c, "Available")
    assert c._day_charging_manual_override is False
    assert c.allow_day_charging is False   # veckoschemat gäller igen


def test_disconnect_restores_day_charging_when_schedule_allows_it():
    """Override AV på en helg → efter urkoppling följer den schemat (PÅ)."""
    mod = _module()
    c = _coordinator()
    _overridden(c, auto_allows_day=True)
    c.allow_day_charging = False
    c._last_connector_status_notify = "Finishing"
    _step(mod, c, "Available")
    assert c._day_charging_manual_override is False
    assert c.allow_day_charging is True


def test_repeated_available_does_not_touch_a_new_override():
    """Bug 51: en override satt med kabeln ur ska överleva Garos Available var 15:e minut."""
    mod = _module()
    c = _coordinator()
    c._last_connector_status_notify = "Available"
    _overridden(c, auto_allows_day=False)
    c.allow_day_charging = True
    _step(mod, c, "Available")
    assert c._day_charging_manual_override is True
    assert c.allow_day_charging is True


def test_override_survives_cable_connect():
    mod = _module()
    c = _coordinator()
    _overridden(c, auto_allows_day=False)
    c.allow_day_charging = True
    c._cable_was_available = True
    c._last_connector_status_notify = "Available"
    c.ocpp.state.session_id = "sess-1"
    _step(mod, c, "Preparing")
    assert c._day_charging_manual_override is True
    assert c.allow_day_charging is True


def test_cleared_override_is_persisted():
    mod = _module()
    store = h.FakeStore()
    c = _coordinator(store)
    _overridden(c, auto_allows_day=False)
    c._last_connector_status_notify = "Charging"
    _step(mod, c, "Available")
    assert store.data["day_charging_manual_override"] is False

    c2 = _coordinator(store)
    asyncio.run(c2._load_state())
    assert c2._day_charging_manual_override is False


if __name__ == "__main__":
    h.run_tests(globals())
