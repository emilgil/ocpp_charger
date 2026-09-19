"""Tester för Feature 8 – tjänsterna get_composite_schedule och clear_charging_profile.

Körs fristående utan Home Assistant:
    python3 tests/test_feature8.py

ocpp_client.py importerar bara stdlib + websockets, så modulkatalogen läggs på sys.path och
modulen importeras direkt (paketets __init__.py drar in HA och ska INTE importeras).
OCPPClient._send_call byts mot en stub som spelar in (action, payload) och returnerar ett
färdigt svar – ingen websocket behövs. Förväntade värden är handskrivna litteraler.
"""
import asyncio
import sys
from pathlib import Path

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
