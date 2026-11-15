"""Bug 60 – närvaro, hemma-zoner, starttimme och sluttid-helper kommer från options (entry.data), inte const.py.
Se bug60.md. Riktig OCPPCoordinator (tests/coordinator_harness.py).
    /mnt/c/temp/github/claude/venv/bin/python tests/test_bug60.py
"""
import asyncio
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coordinator_harness as h  # noqa: E402

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "ocpp_charger"


def _module():
    h.coordinator_class()
    import custom_components.ocpp_charger as mod
    return mod


def _st(state, name=None):
    s = MagicMock()
    s.state = state
    s.name = name
    return s


def _coord(states=None, **data):
    c = h.make_coordinator(entry_data=data)
    c.notifier = MagicMock()
    states = states or {}
    c.hass.states.get.side_effect = lambda eid: states.get(eid)
    return c


def _run(fn, *args, **kwargs):
    async def go():
        r = fn(*args, **kwargs)
        await asyncio.sleep(0)
        return r
    return asyncio.run(go())


# ── närvaro ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_empty_presence_list_is_never_home():
    c = _coord({"device_tracker.a": _st("home")})
    assert c._someone_home() is False


def test_tracker_in_home_state_without_zones():
    c = _coord({"device_tracker.a": _st("home")}, presence_entities=["device_tracker.a"])
    assert c._someone_home() is True


def test_zone_state_counts_only_when_zone_selected():
    states = {"device_tracker.a": _st("Framsidan"), "zone.framsidan": _st("0", "Framsidan")}
    c = _coord(states, presence_entities=["device_tracker.a"], presence_zones=["zone.framsidan"])
    assert c._someone_home() is True
    c = _coord(states, presence_entities=["device_tracker.a"])
    assert c._someone_home() is False


def test_zone_match_is_case_insensitive():
    states = {"device_tracker.a": _st("framsidan"), "zone.f": _st("0", "Framsidan")}
    c = _coord(states, presence_entities=["device_tracker.a"], presence_zones=["zone.f"])
    assert c._someone_home() is True


def test_missing_zone_is_skipped():
    c = _coord({"device_tracker.a": _st("not_home")},
               presence_entities=["device_tracker.a"], presence_zones=["zone.gone"])
    assert c._someone_home() is False


def test_person_entity_home():
    c = _coord({"person.emil": _st("home")}, presence_entities=["person.emil"])
    assert c._someone_home() is True


def test_missing_presence_entity_does_not_raise():
    c = _coord({}, presence_entities=["device_tracker.gone"])
    assert c._someone_home() is False


# ── starttimme ───────────────────────────────────────────────────────────────────────────────────────────────

def test_broken_hour_falls_back_to_9():
    for bad in ("x", None, 99, -1):
        assert _coord(day_offer_earliest_hour=bad)._day_offer_earliest_hour == 9
    assert _coord()._day_offer_earliest_hour == 9
    assert _coord(day_offer_earliest_hour=7)._day_offer_earliest_hour == 7


def _offer_coordinator(mod, **data):
    """Dagladdning av, kabel i, dagplan billigare än natt, någon hemma."""
    now = datetime.now(timezone.utc)
    c = _coord({"device_tracker.a": _st("home")}, **data)
    c.config[mod.CONF_PRICE_FORECAST_ENTITY] = "sensor.price"
    price = MagicMock()
    price.attributes = {"today_interval_prices": [{"time": now, "price": 0.5}], "tomorrow_interval_prices": []}
    base = c.hass.states.get.side_effect
    c.hass.states.get.side_effect = lambda eid: price if eid == "sensor.price" else base(eid)
    c.allow_day_charging = False
    c._force_day_plan = False
    c._day_charging_dismissed = False
    c._day_offer_notified_date = None
    c.ocpp.state.cable_connected = True
    c.ocpp.state.soc_percent = 20.0
    c.target_soc = 80.0
    c._compute_deadline = lambda *a, **k: now + timedelta(hours=20)
    c.schedule = MagicMock()
    c.schedule.is_day_time.return_value = False
    c.schedule.current_limit.return_value = 16
    h.silence(c, "_rebuild_charge_windows")

    def plan(avg):
        p = MagicMock()
        p.feasible = True
        p.start = now + timedelta(hours=1)
        p.end = now + timedelta(hours=3)
        p.avg_price_ore_kwh = avg
        p.estimated_cost_sek = 10.0
        return p

    calls = []

    def fake(**kw):
        calls.append(kw)
        return plan(90.0 if len(calls) <= 2 else 50.0)   # natt först, sedan dag

    return c, patch.object(mod, "plan_cheapest_window", side_effect=fake)


def _local_hour(c):
    return datetime.now(ZoneInfo("Europe/Stockholm")).hour


def test_offer_off_without_presence_entities():
    mod = _module()
    c, planner = _offer_coordinator(mod)
    with planner:
        _run(c._update_charge_plan)
    c.notifier.on_day_charging_chosen.assert_not_called()


def test_offer_sent_when_hour_reached():
    mod = _module()
    c, planner = _offer_coordinator(mod, presence_entities=["device_tracker.a"], day_offer_earliest_hour=0)
    with planner:
        _run(c._update_charge_plan)
    c.notifier.on_day_charging_chosen.assert_called_once()


def test_offer_skipped_before_configured_hour():
    mod = _module()
    c, planner = _offer_coordinator(mod, presence_entities=["device_tracker.a"], day_offer_earliest_hour=23)
    if _local_hour(c) >= 23:
        raise h.Skip("klockan är 23+ lokalt")
    with planner:
        _run(c._update_charge_plan)
    c.notifier.on_day_charging_chosen.assert_not_called()


# ── sluttid-helper ───────────────────────────────────────────────────────────────────────────────────────────

def test_empty_deadline_entity_is_automatic_and_inert():
    c = _coord({"input_datetime.x": _st("07:30:00")})
    assert c._deadline_entity_id == ""
    assert c._get_manual_deadline_str() == ""
    c._reset_deadline_helper()
    c.hass.services.async_call.assert_not_called()


def test_configured_deadline_entity_is_read_and_reset():
    c = _coord({"input_datetime.my": _st("07:30:00")}, deadline_entity="input_datetime.my")
    assert c._get_manual_deadline_str() == "07:30"

    async def go():
        c._reset_deadline_helper()
        await asyncio.sleep(0)
    asyncio.run(go())
    c.hass.services.async_call.assert_called_once()
    assert c.hass.services.async_call.call_args.args[2]["entity_id"] == "input_datetime.my"


def test_listener_registered_only_when_helper_set():
    src = (PKG / "__init__.py").read_text(encoding="utf-8")
    assert re.search(r"if coordinator\._deadline_entity_id:\s+entry\.async_on_unload\(\s+async_track_state_change_event",
                     src)


# ── inga personliga värden kvar i koden ──────────────────────────────────────────────────────────────────────

def test_no_hardcoded_installation_values_in_package():
    banned = ["PRESENCE_ENTITIES", "PRESENCE_HOME_STATES", "DAY_OFFER_EARLIEST_HOUR", "INPUT_DATETIME_DEADLINE",
              "skoda_enyaq_position", "emil_s_s23_ultra", "e_niro_location", "framsidan", "charger_target_time"]
    hits = []
    for f in PKG.rglob("*.py"):
        if ".claude" in f.parts:
            continue
        text = f.read_text(encoding="utf-8")
        for b in banned:
            # \b-gränser: CONF_PRESENCE_ENTITIES / DEFAULT_DAY_OFFER_EARLIEST_HOUR är de nya, tillåtna namnen
            # (understreck är ordtecken, så de matchar inte som egna ord)
            if re.search(r"(?<![A-Za-z0-9_])" + re.escape(b) + r"(?![A-Za-z0-9_])", text,
                         flags=re.I if b.islower() else 0):
                hits.append(f"{f.name}:{b}")
    assert not hits, hits


if __name__ == "__main__":
    h.run_tests(globals())
