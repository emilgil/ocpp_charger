"""Feature 10 – config flow: plug_entity/wake_action per bil (tre ställen), validering och väntetidssteget, plus UI-strängarna.

Flödesstegen körs på riktiga HA-klasser (rot-venv) mot en MagicMock-hass; strängtesterna läser bara JSON och körs även
utan Home Assistant.
    /mnt/c/temp/github/claude/venv/bin/python tests/test_feature10_config_flow.py
"""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coordinator_harness as h  # noqa: E402

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "ocpp_charger"
OLD = {"name": "Kia eNiro", "capacity_kwh": 64.0, "soc_entity": "", "soc_unit": "percent", "max_current_a": 0}
BASE_INPUT = {"name": "Skoda Enyaq", "capacity_kwh": 77.0}
STRING_FILES = ("strings.json", "translations/sv.json")


def _cf():
    h.coordinator_class()   # SKIP utan HA; lägger repo-roten på sys.path
    from custom_components.ocpp_charger import config_flow
    return config_flow


def _options_flow(cf, **data):
    entry = MagicMock()
    entry.data = {"vehicles": [dict(OLD)], **data}
    flow = cf.OCPPChargerOptionsFlow(entry)
    flow.hass = MagicMock()
    return flow


def _saved_vehicles(flow):
    return flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]["vehicles"]


def _load(rel):
    return json.loads((PKG / rel).read_text(encoding="utf-8"))


# ── validering och schema ────────────────────────────────────────────────────────────────────────────────────────

def test_plug_entity_error_rules():
    cf = _cf()
    assert cf._plug_entity_error({}) is None
    assert cf._plug_entity_error({"plug_entity": ""}) is None
    assert cf._plug_entity_error({"plug_entity": "   "}) is None
    assert cf._plug_entity_error({"plug_entity": " binary_sensor.x "}) is None
    assert cf._plug_entity_error({"plug_entity": "sensor.x"}) == "plug_entity_invalid"
    assert cf._plug_entity_error({"plug_entity": "switch.binary_sensor.x"}) == "plug_entity_invalid"


def test_schema_puts_the_plug_field_right_after_the_soc_entity():
    cf = _cf()
    keys = [str(k) for k in cf._vehicle_schema().schema]
    assert keys.index("plug_entity") == keys.index("soc_entity") + 1


def test_plug_field_has_no_default_so_an_emptied_field_can_be_saved():
    """default= skulle återinjicera det sparade värdet när frontend utelämnar det tömda fältet (Feature 9-lärdomen)."""
    cf = _cf()
    schema = cf._vehicle_schema({**OLD, "plug_entity": "binary_sensor.old"})
    marker = next(k for k in schema.schema if str(k) == "plug_entity")
    assert marker.description == {"suggested_value": "binary_sensor.old"}
    assert "plug_entity" not in schema({"name": "A", "capacity_kwh": 64.0})


# ── wake_action (feature10c) ────────────────────────────────────────────────────────────────────────────────────

def test_wake_action_error_rules():
    cf = _cf()
    assert cf._wake_action_error({}) is None
    assert cf._wake_action_error({"wake_action": ""}) is None
    assert cf._wake_action_error({"wake_action": "   "}) is None
    assert cf._wake_action_error({"wake_action": " button.skoda_enyaq_wake_up_car "}) is None
    assert cf._wake_action_error({"wake_action": "kia_uvo.force_update"}) is None
    assert cf._wake_action_error({"wake_action": "no_dot_at_all"}) == "wake_action_invalid"
    assert cf._wake_action_error({"wake_action": "domain."}) == "wake_action_invalid"
    assert cf._wake_action_error({"wake_action": ".service"}) == "wake_action_invalid"
    assert cf._wake_action_error({"wake_action": "a.b.c"}) == "wake_action_invalid"
    assert cf._wake_action_error({"wake_action": "Domain.Service"}) == "wake_action_invalid"
    assert cf._wake_action_error({"wake_action": "domain.ser vice"}) == "wake_action_invalid"


def test_schema_puts_the_wake_field_right_after_the_plug_entity():
    cf = _cf()
    keys = [str(k) for k in cf._vehicle_schema().schema]
    assert keys.index("wake_action") == keys.index("plug_entity") + 1


def test_wake_field_has_no_default_so_an_emptied_field_can_be_saved():
    cf = _cf()
    schema = cf._vehicle_schema({**OLD, "wake_action": "kia_uvo.force_update"})
    marker = next(k for k in schema.schema if str(k) == "wake_action")
    assert marker.description == {"suggested_value": "kia_uvo.force_update"}
    assert "wake_action" not in schema({"name": "A", "capacity_kwh": 64.0})


# ── de tre lagringsställena ──────────────────────────────────────────────────────────────────────────────────────

def test_options_add_stores_a_stripped_plug_entity_and_an_empty_string_when_omitted():
    cf = _cf()
    flow = _options_flow(cf)
    result = asyncio.run(flow.async_step_add_vehicle({**BASE_INPUT, "plug_entity": " binary_sensor.enyaq_plug "}))
    assert result["type"] == "create_entry"
    assert _saved_vehicles(flow)[-1]["plug_entity"] == "binary_sensor.enyaq_plug"

    flow = _options_flow(cf)
    asyncio.run(flow.async_step_add_vehicle(dict(BASE_INPUT)))
    assert _saved_vehicles(flow)[-1]["plug_entity"] == ""


def test_options_add_rejects_a_non_binary_sensor():
    cf = _cf()
    flow = _options_flow(cf)
    result = asyncio.run(flow.async_step_add_vehicle({**BASE_INPUT, "plug_entity": "sensor.enyaq"}))
    assert result["type"] == "form" and result["errors"] == {"plug_entity": "plug_entity_invalid"}
    flow.hass.config_entries.async_update_entry.assert_not_called()


def test_options_edit_stores_replaces_and_clears_the_plug_entity():
    cf = _cf()
    flow = _options_flow(cf)
    flow._edit_index = 0
    asyncio.run(flow.async_step_edit_vehicle({"name": "Kia eNiro", "capacity_kwh": 64.0,
                                              "plug_entity": " binary_sensor.niro "}))
    assert _saved_vehicles(flow)[0]["plug_entity"] == "binary_sensor.niro"

    # tömt fält = frontend utelämnar nyckeln → sparad sensor ska försvinna
    flow = _options_flow(cf, vehicles=[{**OLD, "plug_entity": "binary_sensor.niro"}])
    flow._edit_index = 0
    asyncio.run(flow.async_step_edit_vehicle({"name": "Kia eNiro", "capacity_kwh": 64.0}))
    assert _saved_vehicles(flow)[0]["plug_entity"] == ""


def test_options_edit_rejects_a_non_binary_sensor():
    cf = _cf()
    flow = _options_flow(cf)
    flow._edit_index = 0
    result = asyncio.run(flow.async_step_edit_vehicle({"name": "Kia eNiro", "capacity_kwh": 64.0,
                                                       "plug_entity": "sensor.niro"}))
    assert result["type"] == "form" and result["errors"] == {"plug_entity": "plug_entity_invalid"}
    flow.hass.config_entries.async_update_entry.assert_not_called()


def test_config_flow_add_vehicle_validates_and_stores():
    cf = _cf()
    flow = cf.OCPPChargerConfigFlow()
    flow.hass = MagicMock()
    bad = asyncio.run(flow.async_step_add_vehicle({**BASE_INPUT, "plug_entity": "sensor.x"}))
    assert bad["type"] == "form" and bad["errors"] == {"plug_entity": "plug_entity_invalid"}
    assert flow._vehicles == []

    asyncio.run(flow.async_step_add_vehicle({**BASE_INPUT, "plug_entity": " binary_sensor.enyaq_plug "}))
    assert flow._vehicles[-1]["plug_entity"] == "binary_sensor.enyaq_plug"


def test_options_add_stores_a_stripped_wake_action_and_an_empty_string_when_omitted():
    cf = _cf()
    flow = _options_flow(cf)
    result = asyncio.run(flow.async_step_add_vehicle({**BASE_INPUT, "wake_action": " button.skoda_enyaq_wake_up_car "}))
    assert result["type"] == "create_entry"
    assert _saved_vehicles(flow)[-1]["wake_action"] == "button.skoda_enyaq_wake_up_car"

    flow = _options_flow(cf)
    asyncio.run(flow.async_step_add_vehicle(dict(BASE_INPUT)))
    assert _saved_vehicles(flow)[-1]["wake_action"] == ""


def test_options_add_rejects_a_bad_wake_action():
    cf = _cf()
    flow = _options_flow(cf)
    result = asyncio.run(flow.async_step_add_vehicle({**BASE_INPUT, "wake_action": "not_an_action"}))
    assert result["type"] == "form" and result["errors"] == {"wake_action": "wake_action_invalid"}
    flow.hass.config_entries.async_update_entry.assert_not_called()


def test_options_edit_stores_replaces_and_clears_the_wake_action():
    cf = _cf()
    flow = _options_flow(cf)
    flow._edit_index = 0
    asyncio.run(flow.async_step_edit_vehicle({"name": "Kia eNiro", "capacity_kwh": 64.0,
                                              "wake_action": " kia_uvo.force_update "}))
    assert _saved_vehicles(flow)[0]["wake_action"] == "kia_uvo.force_update"

    flow = _options_flow(cf, vehicles=[{**OLD, "wake_action": "kia_uvo.force_update"}])
    flow._edit_index = 0
    asyncio.run(flow.async_step_edit_vehicle({"name": "Kia eNiro", "capacity_kwh": 64.0}))
    assert _saved_vehicles(flow)[0]["wake_action"] == ""


def test_options_edit_rejects_a_bad_wake_action():
    cf = _cf()
    flow = _options_flow(cf)
    flow._edit_index = 0
    result = asyncio.run(flow.async_step_edit_vehicle({"name": "Kia eNiro", "capacity_kwh": 64.0,
                                                       "wake_action": "not_an_action"}))
    assert result["type"] == "form" and result["errors"] == {"wake_action": "wake_action_invalid"}
    flow.hass.config_entries.async_update_entry.assert_not_called()


def test_config_flow_add_vehicle_validates_wake_action_and_stores():
    cf = _cf()
    flow = cf.OCPPChargerConfigFlow()
    flow.hass = MagicMock()
    bad = asyncio.run(flow.async_step_add_vehicle({**BASE_INPUT, "wake_action": "not_an_action"}))
    assert bad["type"] == "form" and bad["errors"] == {"wake_action": "wake_action_invalid"}
    assert flow._vehicles == []

    asyncio.run(flow.async_step_add_vehicle({**BASE_INPUT, "wake_action": " kia_uvo.force_update "}))
    assert flow._vehicles[-1]["wake_action"] == "kia_uvo.force_update"


# ── väntetidssteget ──────────────────────────────────────────────────────────────────────────────────────────────

def test_menu_offers_the_detection_settings_and_routes_to_the_step():
    cf = _cf()
    flow = _options_flow(cf)
    menu = asyncio.run(flow.async_step_init(None))
    choices = list(menu["data_schema"].schema.values())[0].container
    assert choices["detection"] == "🔎 Edit vehicle detection settings"

    step = asyncio.run(flow.async_step_init({"action": "detection"}))
    assert step["type"] == "form" and step["step_id"] == "edit_detection"


def test_detection_step_defaults_to_90_or_the_saved_value():
    cf = _cf()
    for data, expected in (({}, 90), ({"plug_wait_seconds": 120}, 120)):
        flow = _options_flow(cf, **data)
        form = asyncio.run(flow.async_step_edit_detection(None))
        key = next(k for k in form["data_schema"].schema if str(k) == "plug_wait_seconds")
        assert key.default() == expected


def test_detection_step_saves_whole_seconds_into_the_entry_data():
    cf = _cf()
    flow = _options_flow(cf)
    back = asyncio.run(flow.async_step_edit_detection({"plug_wait_seconds": 90.0}))
    assert back["type"] == "form" and back["step_id"] == "init"      # tillbaka till menyn
    flow._save()
    data = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert data["plug_wait_seconds"] == 90 and isinstance(data["plug_wait_seconds"], int)


def test_detection_step_accepts_0_and_600_and_rejects_601():
    cf = _cf()                    # SKIP utan HA – måste komma före importen (voluptuous följer med HA)
    import voluptuous as vol
    schema = asyncio.run(_options_flow(cf).async_step_edit_detection(None))["data_schema"]
    assert schema({"plug_wait_seconds": 0})["plug_wait_seconds"] == 0
    assert schema({"plug_wait_seconds": 600})["plug_wait_seconds"] == 600
    try:
        schema({"plug_wait_seconds": 601})
    except vol.Invalid:
        return
    raise AssertionError("601 s borde avvisas av NumberSelector (max 600)")


# ── UI-strängar (ingen HA behövs) ────────────────────────────────────────────────────────────────────────────────

def test_strings_have_label_and_description_for_the_plug_entity_in_all_three_vehicle_steps():
    for rel in STRING_FILES:
        d = _load(rel)
        steps = (d["config"]["step"]["add_vehicle"], d["options"]["step"]["add_vehicle"],
                 d["options"]["step"]["edit_vehicle"])
        for step in steps:
            assert step["data"]["plug_entity"].strip(), rel
            assert step["data_description"]["plug_entity"].strip(), rel


def test_strings_have_the_plug_entity_error_in_config_and_options():
    for rel in STRING_FILES:
        d = _load(rel)
        assert d["config"]["error"]["plug_entity_invalid"].strip(), rel
        assert d["options"]["error"]["plug_entity_invalid"].strip(), rel


def test_strings_have_label_and_description_for_the_wake_action_in_all_three_vehicle_steps():
    for rel in STRING_FILES:
        d = _load(rel)
        steps = (d["config"]["step"]["add_vehicle"], d["options"]["step"]["add_vehicle"],
                 d["options"]["step"]["edit_vehicle"])
        for step in steps:
            assert step["data"]["wake_action"].strip(), rel
            assert step["data_description"]["wake_action"].strip(), rel


def test_strings_have_the_wake_action_error_in_config_and_options():
    for rel in STRING_FILES:
        d = _load(rel)
        assert d["config"]["error"]["wake_action_invalid"].strip(), rel
        assert d["options"]["error"]["wake_action_invalid"].strip(), rel


def test_strings_have_the_detection_step():
    for rel in STRING_FILES:
        step = _load(rel)["options"]["step"]["edit_detection"]
        assert step["title"].strip() and step["description"].strip(), rel
        assert step["data"]["plug_wait_seconds"].strip(), rel


if __name__ == "__main__":
    h.run_tests(globals())
