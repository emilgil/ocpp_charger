"""Regressionstest Bug 46 – återställningen av sparat fordon räknades som fordonsbyte (Bug 41-flaggan armerades).

`_load_state()` satte tillbaka det sparade fordonet via set_active_vehicle(). En färsk koordinator startar alltid med
vehicles[0] (eNiro) som aktivt, och det sparade fordonet var Enyaq → namnen skilde → "[Vehicle] Switching Kia eNiro →
Skoda Enyaq" vid varje omstart/omladdning (sex gånger 20/9): _session_total_kwh nollades och
_vehicle_switch_pending_reset armerades. Nästa Preparing som klassades som Garo-reset tog då Bug 41-grenen och nollade
_session_total_kwh (SOC-estimatets energibas) i stället för att ackumulera ("[Bug41] Preparing efter fordonsbyte", 21/9).

Körs med rot-venv (koordinatortester, se tests/coordinator_harness.py):
    /mnt/c/temp/github/claude/venv/bin/python tests/test_bug46.py
Utan Home Assistant hoppas de över (SKIP).
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coordinator_harness as h  # noqa: E402

QUIET = ("_update_soc_from_ha", "_update_charge_plan", "async_set_updated_data")   # planering/SOC-läsning under bytet


def _restored_after_restart():
    """Store skriven av en koordinator med Enyaq aktiv och en pågående kabelsession (12.3 kWh levererat sedan
    start-SOC 41 %); läst av en färsk koordinator (som alltid startar med vehicles[0] = eNiro)."""
    store = h.FakeStore()
    writer = h.make_coordinator(store)
    writer.active_vehicle = writer._vehicles[1]
    writer._session_total_kwh = 12.3
    writer._session_start_soc = 41.0
    asyncio.run(writer._save_state())

    reader = h.silence(h.make_coordinator(store), *QUIET)
    asyncio.run(reader._load_state())
    return reader


def test_restoring_the_saved_vehicle_is_not_a_vehicle_switch():
    """Felet: återställningen tolkades som byte eNiro → Enyaq och armerade Bug 41-flaggan vid varje omstart."""
    c = _restored_after_restart()

    assert c.active_vehicle["name"] == "Skoda Enyaq"
    assert c._vehicle_switch_pending_reset is False
    assert c._session_total_kwh == 12.3   # Bug 30-återställningen, ovillkorligen ensam sanning


def test_garo_reset_after_a_restore_accumulates_instead_of_zeroing():
    """Symptomet 21/9: efter en omstart mitt i en kabelsession kommer en äkta Finishing → Preparing (Garo-reset).
    Den ska lägga till sub-sessionens 8.1 kWh på 12.3, inte nollställa energibasen via Bug 41-grenen."""
    c = _restored_after_restart()
    c.ocpp.state.connector_status = "Preparing"
    c._last_connector_status_notify = "Finishing"   # känd föregående status: en riktig statusändring
    c.ocpp.state.energy_kwh = 8.1

    c._check_notify_events()

    assert round(c._session_total_kwh, 6) == 20.4


def test_a_real_vehicle_switch_still_resets_and_arms_the_bug41_flag():
    """Bug 41 är oförändrad för riktiga byten: energibasen nollas och nästa Preparing får inte ackumulera den
    gamla bilens kvarliggande state.energy_kwh."""
    c = h.silence(h.make_coordinator(), *QUIET)
    c._session_total_kwh = 5.0

    c.set_active_vehicle(c._vehicles[1])

    assert c._session_total_kwh == 0.0
    assert c._vehicle_switch_pending_reset is True


if __name__ == "__main__":
    h.run_tests(globals())
