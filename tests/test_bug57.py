"""Bug 57 – Inkopplad-notisen skickas efter fordonsidentifieringen, inte före.

Riktig OCPPCoordinator (tests/coordinator_harness.py). Fejkat: hass.states, tid, sensorlyssnare, SoC-fallbacken och
notifieraren. Push-vägen (`_on_charger_state_update_async`) körs utan poll-cykel. Se bug57.md.
    /mnt/c/temp/github/claude/venv/bin/python tests/test_bug57.py
Utan Home Assistant hoppas de över (SKIP).
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coordinator_harness as h  # noqa: E402
import test_feature10_coordinator as t10  # noqa: E402

ENYAQ_PLUG, NIRO_PLUG = t10.ENYAQ_PLUG, t10.NIRO_PLUG
PUSH_QUIET = ("_update_price_from_ha", "_apply_current_schedule", "_update_smart_charging")


def _push_coordinator(states, *, vehicles=t10.VEHICLES):
    c = h.silence(t10._coordinator(states, vehicles=vehicles), *PUSH_QUIET)
    c._cable_was_available = True
    c._last_connector_status = "Available"
    c._last_connector_status_notify = "Available"
    c.ocpp.state.cable_connected = True
    c.ocpp.state.soc_percent = 86.0
    # Spion: vilken bil var aktiv när notisen byggdes / när planen räknades
    c.seen = {"notify": [], "plan": []}
    c.notifier.on_cable_connected.side_effect = lambda **kw: c.seen["notify"].append(
        (c.active_vehicle["name"], kw))
    c._update_charge_plan = lambda *a, **k: c.seen["plan"].append(c.active_vehicle["name"])
    return c


def _push(c, status):
    """En OCPP-push med ny connector-status – ingen poll-cykel."""
    async def run():
        c.ocpp.state.connector_status = status
        c._on_charger_state_update_async(c.ocpp.state)
        await asyncio.sleep(0)
    asyncio.run(run())


def _poll_detect(c):
    c._check_vehicle_auto_detect()


def test_plug_match_on_push_identifies_before_the_notification():
    with t10._patched() as (wires, soc):
        c = _push_coordinator({ENYAQ_PLUG: "on", NIRO_PLUG: "off"})
        assert c.active_vehicle["name"] == "Kia eNiro"

        _push(c, "Preparing")

        assert c.active_vehicle["name"] == "Skoda Enyaq"
        assert len(c.seen["notify"]) == 1
        name, kw = c.seen["notify"][0]
        assert name == "Skoda Enyaq" and kw["vehicle_name"] == "Skoda Enyaq"
        assert ENYAQ_PLUG in kw["detection_reason"]


def test_soc_fallback_on_push_identifies_before_the_notification():
    with t10._patched() as (wires, soc):
        vehicles = [t10._without_plug(t10.NIRO), t10._without_plug(t10.ENYAQ)]
        c = _push_coordinator({}, vehicles=vehicles)
        import custom_components.ocpp_charger as mod
        # SoC-fallbacken väljer Skoda (lägst SOC)
        mod.identify_vehicle = lambda vs, soc_pct, hass: (vs[1], "SoC fallback")

        _push(c, "Preparing")

        assert c.seen["notify"][0][1]["vehicle_name"] == "Skoda Enyaq"


def test_poll_after_the_push_does_not_identify_again():
    with t10._patched() as (wires, soc):
        c = _push_coordinator({ENYAQ_PLUG: "on", NIRO_PLUG: "off"})
        _push(c, "Preparing")
        switches = []
        orig = c.set_active_vehicle
        c.set_active_vehicle = lambda *a, **k: (switches.append(a), orig(*a, **k))[1]

        _poll_detect(c)   # nästa poll ser Preparing → Preparing

        assert switches == []
        assert soc.calls == 0
        assert len(c.seen["notify"]) == 1


def test_plan_is_recalculated_for_the_new_vehicle_before_the_notification():
    with t10._patched() as (wires, soc):
        c = _push_coordinator({ENYAQ_PLUG: "on", NIRO_PLUG: "off"})

        _push(c, "Preparing")

        assert c.seen["plan"] and c.seen["plan"][0] == "Skoda Enyaq"
        assert c.active_vehicle["capacity_kwh"] == 77.0


def test_bug41_flag_is_consumed_by_the_same_push():
    with t10._patched() as (wires, soc):
        c = _push_coordinator({ENYAQ_PLUG: "on", NIRO_PLUG: "off"})
        c._session_total_kwh = 12.0

        _push(c, "Preparing")

        assert c._session_total_kwh == 0.0
        assert c._vehicle_switch_pending_reset is False


def test_restart_state_does_not_identify():
    with t10._patched() as (wires, soc):
        c = _push_coordinator({ENYAQ_PLUG: "on", NIRO_PLUG: "off"})
        c._last_connector_status = ""     # färsk koordinator efter omstart

        _push(c, "Preparing")

        assert c.active_vehicle["name"] == "Kia eNiro"
        assert soc.calls == 0


def test_same_vehicle_keeps_it_but_records_the_reason():
    with t10._patched() as (wires, soc):
        c = _push_coordinator({ENYAQ_PLUG: "on", NIRO_PLUG: "off"})
        c.active_vehicle = c._vehicles[1]     # Skoda redan aktiv
        switches = []
        orig = c.set_active_vehicle
        c.set_active_vehicle = lambda *a, **k: (switches.append(a), orig(*a, **k))[1]

        _push(c, "Preparing")

        assert switches == []
        assert len(c.seen["notify"]) == 1
        assert ENYAQ_PLUG in c.seen["notify"][0][1]["detection_reason"]


if __name__ == "__main__":
    h.run_tests(globals())
