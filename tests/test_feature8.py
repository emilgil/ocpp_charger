"""Tester för Feature 8 – tjänsterna get_composite_schedule och clear_charging_profile.

Körs fristående utan Home Assistant:
    python3 tests/test_feature8.py

ocpp_client.py importerar bara stdlib + websockets, så modulkatalogen läggs på sys.path och
modulen importeras direkt (paketets __init__.py drar in HA och ska INTE importeras).
OCPPClient._send_call byts mot en stub som spelar in (action, payload) och returnerar ett
färdigt svar – ingen websocket behövs. Förväntade värden är handskrivna litteraler.
"""
import ast
import asyncio
import sys
from pathlib import Path

import yaml  # PyYAML – ingår i HA:s beroenden (HA-venv) och finns i systempython

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "ocpp_charger"
sys.path.insert(0, str(PKG))
import ocpp_client as oc  # noqa: E402


def make_client():
    return oc.OCPPClient("127.0.0.1", 9000, "TEST-CP", lambda state: None)


def stub_send_call(client, response=None, error=None):
    """Byt ut _send_call mot en stub. Returnerar listan som fylls med (action, payload)."""
    calls = []

    async def fake(action, payload, timeout=10.0):
        calls.append((action, payload))
        if error is not None:
            raise error
        return response

    client._send_call = fake
    return calls


# ── get_composite_schedule ─────────────────────────────────────────────────────


def test_get_composite_schedule_sends_defaults():
    client = make_client()
    calls = stub_send_call(client, {"status": "Accepted"})
    asyncio.run(client.get_composite_schedule())
    assert calls == [
        ("GetCompositeSchedule", {"connectorId": 1, "duration": 3600, "chargingRateUnit": "A"})
    ]


def test_get_composite_schedule_sends_connector_zero_and_custom_arguments():
    # connector 0 = hela laddpunkten. Får inte ersättas av default 1.
    client = make_client()
    calls = stub_send_call(client, {"status": "Accepted"})
    asyncio.run(client.get_composite_schedule(connector_id=0, duration=7200, charging_rate_unit="W"))
    assert calls == [
        ("GetCompositeSchedule", {"connectorId": 0, "duration": 7200, "chargingRateUnit": "W"})
    ]


def test_get_composite_schedule_maps_accepted_response():
    schedule = {
        "duration": 3600,
        "chargingRateUnit": "A",
        "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 13.0, "numberPhases": 3}],
    }
    client = make_client()
    stub_send_call(client, {
        "status": "Accepted",
        "connectorId": 1,
        "scheduleStart": "2026-09-19T10:00:00Z",
        "chargingSchedule": schedule,
    })
    result = asyncio.run(client.get_composite_schedule(connector_id=1, duration=3600))
    assert result == {
        "status": "Accepted",
        "connector_id": 1,
        "schedule_start": "2026-09-19T10:00:00Z",
        "charging_schedule": schedule,
    }


def test_get_composite_schedule_rejected_falls_back_to_requested_connector():
    client = make_client()
    stub_send_call(client, {"status": "Rejected"})
    result = asyncio.run(client.get_composite_schedule(connector_id=0))
    assert result == {
        "status": "Rejected",
        "connector_id": 0,
        "schedule_start": None,
        "charging_schedule": None,
    }


def test_get_composite_schedule_failure_is_returned_not_raised():
    client = make_client()
    stub_send_call(client, error=TimeoutError("OCPP call GetCompositeSchedule timed out"))
    result = asyncio.run(client.get_composite_schedule())
    assert result == {"status": "Error", "error": "OCPP call GetCompositeSchedule timed out"}


def test_get_composite_schedule_without_charger_reports_error():
    # Ingen stub: den riktiga _send_call kastar ConnectionError när ingen box är ansluten.
    client = make_client()
    result = asyncio.run(client.get_composite_schedule())
    assert result == {"status": "Error", "error": "No charger connected"}


# ── clear_charging_profile ─────────────────────────────────────────────────────


def test_clear_without_filters_sends_empty_payload():
    # Klienten skyddar inte själv – det gör service-handlern (confirm_clear_all).
    client = make_client()
    calls = stub_send_call(client, {"status": "Accepted"})
    result = asyncio.run(client.clear_charging_profile())
    assert calls == [("ClearChargingProfile", {})]
    assert result == {"status": "Accepted", "request": {}}


def test_clear_maps_every_filter_to_its_ocpp_field():
    client = make_client()
    calls = stub_send_call(client, {"status": "Accepted"})
    asyncio.run(client.clear_charging_profile(
        profile_id=5, connector_id=1, purpose="TxDefaultProfile", stack_level=2,
    ))
    assert calls == [(
        "ClearChargingProfile",
        {"id": 5, "connectorId": 1, "chargingProfilePurpose": "TxDefaultProfile", "stackLevel": 2},
    )]


def test_clear_keeps_zero_valued_filters():
    # 0 är giltigt: connectorId 0 = hela laddpunkten, id 0 och stackLevel 0 är riktiga värden.
    client = make_client()
    calls = stub_send_call(client, {"status": "Accepted"})
    asyncio.run(client.clear_charging_profile(profile_id=0, connector_id=0, stack_level=0))
    assert calls == [("ClearChargingProfile", {"id": 0, "connectorId": 0, "stackLevel": 0})]


def test_clear_treats_empty_purpose_as_no_filter():
    client = make_client()
    calls = stub_send_call(client, {"status": "Accepted"})
    asyncio.run(client.clear_charging_profile(purpose=""))
    assert calls == [("ClearChargingProfile", {})]


def test_clear_passes_unknown_status_through():
    # Unknown = ingen profil matchade filtret (OCPP 1.6).
    client = make_client()
    stub_send_call(client, {"status": "Unknown"})
    result = asyncio.run(client.clear_charging_profile(purpose="TxProfile"))
    assert result == {"status": "Unknown", "request": {"chargingProfilePurpose": "TxProfile"}}


def test_clear_failure_is_returned_not_raised():
    client = make_client()
    stub_send_call(client, error=TimeoutError("OCPP call ClearChargingProfile timed out"))
    result = asyncio.run(client.clear_charging_profile(profile_id=7))
    assert result == {
        "status": "Error",
        "request": {"id": 7},
        "error": "OCPP call ClearChargingProfile timed out",
    }


def test_clear_without_charger_reports_error():
    client = make_client()
    result = asyncio.run(client.clear_charging_profile(purpose="TxProfile"))
    assert result == {
        "status": "Error",
        "request": {"chargingProfilePurpose": "TxProfile"},
        "error": "No charger connected",
    }


# ── services.yaml ──────────────────────────────────────────────────────────────


def load_services():
    return yaml.safe_load((PKG / "services.yaml").read_text(encoding="utf-8"))


def test_services_yaml_keeps_the_existing_services():
    assert {"rest_call", "change_configuration", "get_configuration"} <= set(load_services())


def test_get_composite_schedule_service_definition():
    fields = load_services()["get_composite_schedule"]["fields"]
    assert set(fields) == {"connector_id", "duration", "charging_rate_unit"}
    assert fields["connector_id"]["default"] == 1
    assert fields["duration"]["default"] == 3600
    assert fields["charging_rate_unit"]["default"] == "A"
    assert fields["charging_rate_unit"]["selector"]["select"]["options"] == ["A", "W"]


def test_clear_charging_profile_service_definition():
    fields = load_services()["clear_charging_profile"]["fields"]
    assert set(fields) == {"profile_id", "connector_id", "purpose", "stack_level", "confirm_clear_all"}
    assert fields["purpose"]["selector"]["select"]["options"] == [
        "ChargePointMaxProfile", "TxDefaultProfile", "TxProfile",
    ]
    # Skyddet mot att rensa allt av misstag: confirm_clear_all är av som standard …
    assert fields["confirm_clear_all"]["default"] is False
    # … och ingen filterruta får vara förifylld, annars går "inget filter"-vakten runt.
    for name in ("profile_id", "connector_id", "purpose", "stack_level"):
        assert "default" not in fields[name], name


# ── __init__.py: handlers ↔ services.yaml, registrering ↔ avregistrering ────────
# __init__.py drar in Home Assistant och kan inte importeras här, så handlers och
# registreringar granskas strukturellt via ast (inget körs).


def _init_tree():
    return ast.parse((PKG / "__init__.py").read_text(encoding="utf-8"))


def _find_function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} saknas i __init__.py")


def _call_data_keys(func):
    """Strängnycklarna i alla call.data.get("...")-anrop i en service-handler."""
    keys = set()
    for node in ast.walk(func):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            continue
        target = node.func.value  # ska vara call.data
        if (
            isinstance(target, ast.Attribute)
            and target.attr == "data"
            and isinstance(target.value, ast.Name)
            and target.value.id == "call"
        ):
            keys.add(node.args[0].value)
    return keys


def _service_name(node):
    """Tjänstenamn ur ett argument: strängliteral eller konstantnamn (SERVICE_REST_CALL)."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return node.id
    raise AssertionError(f"oväntat tjänsteargument: {ast.dump(node)}")


def test_handlers_read_exactly_the_fields_services_yaml_declares():
    tree = _init_tree()
    services = load_services()
    for handler, service in (
        ("_handle_get_composite_schedule", "get_composite_schedule"),
        ("_handle_clear_charging_profile", "clear_charging_profile"),
    ):
        keys = _call_data_keys(_find_function(tree, handler))
        assert keys == set(services[service]["fields"]), handler


def test_every_registered_service_is_removed_on_unload():
    tree = _init_tree()
    registered = {
        _service_name(n.args[1])
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "async_register"
        and len(n.args) >= 2
    }
    removed = set()
    for n in ast.walk(_find_function(tree, "async_unload_entry")):
        if isinstance(n, ast.For) and isinstance(n.iter, ast.Tuple):
            removed |= {_service_name(e) for e in n.iter.elts}
    assert {"get_composite_schedule", "clear_charging_profile"} <= registered
    assert registered == removed


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as exc:  # AssertionError = fel resultat, annat = kraschade
            failed += 1
            print(f"FAIL  {t.__name__}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failed else 0)
