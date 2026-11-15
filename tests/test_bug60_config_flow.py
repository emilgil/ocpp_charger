"""Bug 60 – options-stegen edit_day_offer och edit_deadline samt UI-strängarna.
    /mnt/c/temp/github/claude/venv/bin/python tests/test_bug60_config_flow.py
"""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coordinator_harness as h  # noqa: E402

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "ocpp_charger"
STRING_FILES = ("strings.json", "translations/sv.json")


def _cf():
    h.coordinator_class()
    from custom_components.ocpp_charger import config_flow
    return config_flow


def _flow(cf, **data):
    entry = MagicMock()
    entry.data = {"vehicles": [], **data}
    flow = cf.OCPPChargerOptionsFlow(entry)
    flow.hass = MagicMock()
    return flow


def _saved(flow):
    return flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]


def test_day_offer_step_saves_values():
    cf = _cf()
    flow = _flow(cf)
    asyncio.run(flow.async_step_edit_day_offer({
        "presence_entities": ["device_tracker.a"], "presence_zones": ["zone.f"], "day_offer_earliest_hour": 7.0}))
    flow._save()
    d = _saved(flow)
    assert d["presence_entities"] == ["device_tracker.a"]
    assert d["presence_zones"] == ["zone.f"]
    assert d["day_offer_earliest_hour"] == 7 and isinstance(d["day_offer_earliest_hour"], int)


def test_day_offer_cleared_fields_save_as_empty():
    cf = _cf()
    flow = _flow(cf, presence_entities=["device_tracker.a"], presence_zones=["zone.f"])
    asyncio.run(flow.async_step_edit_day_offer({"day_offer_earliest_hour": 9}))
    flow._save()
    d = _saved(flow)
    assert d["presence_entities"] == [] and d["presence_zones"] == []


def test_day_offer_form_uses_suggested_value_not_default():
    cf = _cf()
    flow = _flow(cf, presence_entities=["device_tracker.a"])
    res = asyncio.run(flow.async_step_edit_day_offer())
    marker = next(k for k in res["data_schema"].schema if str(k) == "presence_entities")
    assert marker.description == {"suggested_value": ["device_tracker.a"]}
    assert "presence_entities" not in res["data_schema"]({})


def test_deadline_step_saves_and_clears():
    cf = _cf()
    flow = _flow(cf, deadline_entity="input_datetime.old")
    asyncio.run(flow.async_step_edit_deadline({"deadline_entity": "input_datetime.new"}))
    flow._save()
    assert _saved(flow)["deadline_entity"] == "input_datetime.new"
    flow = _flow(cf, deadline_entity="input_datetime.old")
    asyncio.run(flow.async_step_edit_deadline({}))
    flow._save()
    assert _saved(flow)["deadline_entity"] == ""


def test_deadline_wrong_domain_is_rejected():
    cf = _cf()
    flow = _flow(cf)
    res = asyncio.run(flow.async_step_edit_deadline({"deadline_entity": "sensor.x"}))
    assert res["errors"] == {"deadline_entity": "deadline_entity_invalid"}


def test_menu_has_the_new_entries():
    cf = _cf()
    flow = _flow(cf)
    res = asyncio.run(flow.async_step_init())
    choices = list(res["data_schema"].schema["action"].container)
    assert "day_offer" in choices and "deadline" in choices
    assert choices.index("detection") < choices.index("day_offer") < choices.index("deadline") < choices.index("notify")


def test_strings_present_in_both_files():
    for rel in STRING_FILES:
        o = json.loads((PKG / rel).read_text(encoding="utf-8"))["options"]
        for step in ("edit_day_offer", "edit_deadline"):
            assert step in o["step"], (rel, step)
        assert "deadline_entity_invalid" in o["error"], rel
        assert set(o["step"]["edit_day_offer"]["data"]) == {
            "presence_entities", "presence_zones", "day_offer_earliest_hour"}
        assert "deadline_entity" in o["step"]["edit_deadline"]["data"]


if __name__ == "__main__":
    h.run_tests(globals())
