"""Feature 11 – Charge Goal-sensor: aktivt mål i både kWh och SOC %.
    /mnt/c/temp/github/claude/venv/bin/python tests/test_feature11.py
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coordinator_harness as h  # noqa: E402


def _c(*, target_kwh=0.0, target_soc=80.0, start_soc=None, cable=True, soc=None,
       capacity=77.0, session_total=0.0, tx_energy=0.0, tx_id=None, ha_soc=None):
    c = h.make_coordinator()
    c.target_kwh, c.target_soc = target_kwh, target_soc
    c._session_start_soc = start_soc
    c._session_total_kwh = session_total
    c.ocpp.state.cable_connected = cable
    c.ocpp.state.soc_percent = soc
    c.ocpp.state.energy_kwh = tx_energy
    c.ocpp.state.transaction_id = tx_id
    c.active_vehicle = {"name": "Skoda", "capacity_kwh": capacity, "soc_entity": "sensor.x"}
    from custom_components.ocpp_charger import const
    c.active_vehicle = {const.VEHICLE_NAME: "Skoda", const.VEHICLE_CAPACITY: capacity,
                        const.VEHICLE_SOC_ENTITY: "sensor.x"}
    st = MagicMock(); st.state = "unavailable" if ha_soc is None else str(ha_soc)
    c.hass.states.get = MagicMock(return_value=st)
    return c


def _sensor(c):
    from custom_components.ocpp_charger.sensor import ChargeGoalSensor
    s = ChargeGoalSensor.__new__(ChargeGoalSensor)
    s.coordinator = c
    return s


def test_kwh_goal_no_cable_current_soc():
    c = _c(target_kwh=20, cable=False, ha_soc=50)
    i = c.charge_goal_info()
    assert i["base_source"] == "current" and i["target_soc_equiv"] == 73.9
    assert _sensor(c).native_value == "20,0 kWh ≈ 74 %"


def test_soc_goal():
    c = _c(target_kwh=0, target_soc=80, cable=False, ha_soc=50)
    assert _sensor(c).native_value == "80 % ≈ 25,1 kWh"


def test_capped():
    c = _c(target_kwh=20, cable=False, ha_soc=90)
    assert _sensor(c).native_value == "20,0 kWh ≈ 100 % (max)"


def test_missing_soc_is_none_not_zero():
    c = _c(target_kwh=20, cable=False, ha_soc=None)
    i = c.charge_goal_info()
    assert i["base_soc"] is None and i["base_source"] == "missing"
    assert _sensor(c).native_value == "20,0 kWh (SOC okänd)"
    c = _c(target_kwh=0, cable=False, ha_soc=None)
    assert _sensor(c).native_value == "80 % (SOC okänd)"


def test_session_start_preferred_and_delivered():
    c = _c(target_kwh=20, start_soc=40, soc=60, session_total=5.0, tx_energy=2.0, tx_id=1)
    i = c.charge_goal_info()
    assert i["base_source"] == "session_start" and i["base_soc"] == 40.0
    assert i["delivered_kwh"] == 7.0 and i["remaining_kwh"] == 13.0


def test_cable_uses_state_soc():
    i = _c(target_kwh=20, soc=50).charge_goal_info()
    assert i["base_source"] == "current" and i["base_soc"] == 50.0


def test_out_of_range_soc_missing():
    assert _c(target_kwh=20, cable=False, ha_soc=150).charge_goal_info()["base_source"] == "missing"


if __name__ == "__main__":
    fails = 0
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            try:
                f(); print("PASS", n)
            except Exception as e:
                fails += 1; print("FAIL", n, repr(e))
    sys.exit(1 if fails else 0)
