"""Feature 10 – identify_by_plug_sensor: beslutstabellen för bilidentifiering via "inkopplad"-sensor.

Ren funktion (ingen timer, inga sidoeffekter, ingen notis). Modulen importeras via paketet, så testet kräver rot-venv:
    /mnt/c/temp/github/claude/venv/bin/python tests/test_feature10_detection.py
Utan Home Assistant hoppas de över (SKIP). Scenarionumren följer claude_feature10.md § Verifiering.
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coordinator_harness as h  # noqa: E402

ENYAQ_PLUG = "binary_sensor.skoda_enyaq_charger_connected"
NIRO_PLUG = "binary_sensor.e_niro_ev_battery_plug"


def _vd():
    h.coordinator_class()   # SKIP utan HA; lägger repo-roten på sys.path
    from custom_components.ocpp_charger import vehicle_detection
    return vehicle_detection


def _vehicles(enyaq_plug=ENYAQ_PLUG, niro_plug=NIRO_PLUG):
    """Samma ordning som driftsatt konfiguration (eNiro först). None = nyckeln saknas helt (gammal konfiguration)."""
    niro = {"name": "Kia eNiro", "capacity_kwh": 64.0, "soc_entity": "sensor.e_niro_ev_battery_level"}
    enyaq = {"name": "Skoda Enyaq", "capacity_kwh": 77.0, "soc_entity": "sensor.skoda_enyaq_battery_percentage"}
    if niro_plug is not None:
        niro["plug_entity"] = niro_plug
    if enyaq_plug is not None:
        enyaq["plug_entity"] = enyaq_plug
    return [niro, enyaq]


class _State:
    def __init__(self, state):
        self.state = state


class FakeHass:
    """Bara hass.states.get(entity_id). states = {entity_id: råstate}; saknad nyckel = entiteten finns inte."""

    def __init__(self, states):
        self._states = states
        self.states = self

    def get(self, entity_id):
        raw = self._states.get(entity_id)
        return None if raw is None else _State(raw)


def _identify(vehicles, states):
    return _vd().identify_by_plug_sensor(vehicles, FakeHass(states))


def test_no_sensor_configured_keeps_the_old_logic():                                   # scenario 7
    for vehicles in (_vehicles(None, None), _vehicles("", "")):   # nyckeln saknas / tom sträng
        r = _identify(vehicles, {ENYAQ_PLUG: "on", NIRO_PLUG: "on"})   # sensorernas state ska inte ens läsas
        assert r.outcome == "no_sensors" and r.vehicle is None and r.reason_code is None


def test_one_plugged_and_all_covered_matches_that_vehicle():                           # scenario 1
    vehicles = _vehicles()
    r = _identify(vehicles, {ENYAQ_PLUG: "on", NIRO_PLUG: "off"})
    assert r.outcome == "matched"
    assert r.vehicle is vehicles[1]            # samma dict-objekt: koordinatorn jämför på identitet
    assert ENYAQ_PLUG in r.reason              # reason nämner sensorn
    assert r.reason_code is None


def test_the_other_vehicle_can_match_too():                                            # scenario 2
    vehicles = _vehicles()
    r = _identify(vehicles, {ENYAQ_PLUG: "off", NIRO_PLUG: "on"})
    assert r.outcome == "matched" and r.vehicle is vehicles[0] and NIRO_PLUG in r.reason


def test_two_plugged_asks_the_user_at_once():                                          # scenario 3
    r = _identify(_vehicles(), {ENYAQ_PLUG: "on", NIRO_PLUG: "on"})
    assert (r.outcome, r.reason_code, r.vehicle) == ("notify", "multiple_plugged", None)


def test_none_plugged_means_wait():                                                    # scenario 4/5
    r = _identify(_vehicles(), {ENYAQ_PLUG: "off", NIRO_PLUG: "off"})
    assert (r.outcome, r.reason_code, r.vehicle) == ("wait", "none_plugged", None)


def test_one_plugged_but_only_one_vehicle_has_a_sensor_asks_the_user():                # scenario 6
    r = _identify(_vehicles(niro_plug=None), {ENYAQ_PLUG: "on"})
    assert (r.outcome, r.reason_code, r.vehicle) == ("notify", "partial_sensors", None)


def test_unavailable_sensor_counts_as_not_covered():                                   # scenario 8
    r = _identify(_vehicles(), {ENYAQ_PLUG: "unavailable", NIRO_PLUG: "on"})
    assert (r.outcome, r.reason_code) == ("notify", "partial_sensors")


def test_every_unusable_state_is_treated_as_no_usable_sensor():
    for raw in ("unknown", "", "Plugged in", "unavailable", None):   # None = entiteten saknas
        states = {NIRO_PLUG: "on"}
        if raw is not None:
            states[ENYAQ_PLUG] = raw
        r = _identify(_vehicles(), states)
        assert (r.outcome, r.reason_code) == ("notify", "partial_sensors"), repr(raw)


def test_unusable_sensor_and_nothing_plugged_still_waits():
    r = _identify(_vehicles(), {ENYAQ_PLUG: "unavailable", NIRO_PLUG: "off"})
    assert (r.outcome, r.reason_code) == ("wait", "none_plugged")


def test_raw_states_and_the_decision_are_logged():
    vd = _vd()
    logger = logging.getLogger("custom_components.ocpp_charger.vehicle_detection")

    class Capture(logging.Handler):
        def __init__(self):
            super().__init__(logging.DEBUG)
            self.messages = []

        def emit(self, record):
            self.messages.append(record.getMessage())

    capture, old_level = Capture(), logger.level
    logger.addHandler(capture)
    logger.setLevel(logging.DEBUG)
    try:
        vd.identify_by_plug_sensor(_vehicles(), FakeHass({ENYAQ_PLUG: "on", NIRO_PLUG: "unavailable"}))
    finally:
        logger.removeHandler(capture)
        logger.setLevel(old_level)
    text = "\n".join(capture.messages)
    assert "[VehicleDetect]" in text
    assert f"{ENYAQ_PLUG}:'on'" in text and f"{NIRO_PLUG}:'unavailable'" in text   # råa states, så misstolkningar syns
    assert "notify" in text                                                        # och beslutet


if __name__ == "__main__":
    h.run_tests(globals())
