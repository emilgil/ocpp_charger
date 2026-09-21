"""Regressionstest Bug 44 – kabelsessionens ackumulatorer nollställdes inte vid inkoppling efter omstart.

`_cable_was_available` (Bug 13A/38: "en äkta Available har setts sedan förra inkopplingen") sparades inte i
Store. Efter en (om)laddning av integrationen var flaggan False, Garo skickade inte om StatusNotification vid
reconnect, och nästa `Preparing` klassades som Garo-reset i stället för genuin inkoppling – så
`_cable_session_energy_kwh`/`_cable_session_cost_sek` nollställdes aldrig.

Körs:
    python3 tests/test_bug44.py
        ren logik (cable_flag.py, stdlib-only). Koordinatortesterna hoppas över (SKIP) – Home Assistant saknas.
    /mnt/c/temp/github/claude/venv/bin/python tests/test_bug44.py
        även round-trip genom den riktiga OCPPCoordinator._save_state/_load_state (HA 2025.1.4 i rot-venv,
        se tests/coordinator_harness.py).
"""
import asyncio
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "ocpp_charger"
sys.path.insert(0, str(PKG))
import cable_flag as cf  # noqa: E402
import coordinator_harness as h  # noqa: E402


def restore(saved, **kwargs):
    kwargs.setdefault("cable_connected", False)
    return cf.restore_cable_was_available(saved, **kwargs)


# ── Ren logik ───────────────────────────────────────────────────────────────────────────────────────────


def test_saved_true_survives_restart_with_cable_out():
    """Incidenten 21/9: en Available sågs, integrationen laddades om, flaggan glömdes. Med kabeln urdragen
    måste sparad True leva kvar så att nästa Preparing tas som genuin inkoppling."""
    assert restore(True, cable_connected=False) is True


def test_saved_false_mid_session_stays_false():
    """Bug 38-skyddet: en omstart mitt i en kabelsession (kabeln inkopplad, flaggan förbrukad) får inte armera
    en falsk genuin inkoppling vid nästa transaktionspaus (Finishing → Preparing)."""
    assert restore(False, cable_connected=True) is False


def test_saved_false_stays_false_when_cable_reads_as_out_but_no_available_was_seen():
    """cable_connected är bara True för Preparing/Charging/SuspendedEV/SuspendedEVSE/Finishing
    (ocpp_client.py). Faulted/Unavailable med kabeln i ger False utan att någon Available setts – flaggan får då
    inte härledas ur cable_connected, utan den sparade Falsen gäller."""
    assert restore(False, cable_connected=False) is False


def test_store_without_the_key_falls_back_to_the_saved_cable_state():
    """Första omstarten efter deploy: den gamla Store saknar nyckeln. Kabel urdragen → nästa Preparing är
    genuin (True); kabel inkopplad → mitt i en session (False)."""
    assert restore(None, cable_connected=False) is True
    assert restore(None, cable_connected=True) is False


def test_non_bool_saved_value_behaves_like_a_missing_key():
    """Strängen "false" är truthy: passerade den som bool() skulle en sparad skräpsträng armera en falsk genuin
    inkoppling mitt i en session. Ett icke-bool-värde ska följa migreringsregeln."""
    assert restore("false", cable_connected=True) is False


def test_flag_set_by_a_live_available_is_not_downgraded_by_the_restore():
    """_load_state() körs ~10 s efter start. Hinner en äkta Available (flaggan True) komma före, får den sparade
    Falsen (kabeln var inkopplad när Store skrevs) inte skriva över den."""
    assert restore(False, cable_connected=True, current=True) is True


# ── Round-trip genom riktiga OCPPCoordinator._save_state/_load_state (kräver Home Assistant) ──────────────


def _coordinator(store, *, cable_was_available, cable_connected):
    c = h.make_coordinator(store)
    c._cable_was_available = cable_was_available
    c.ocpp.state.cable_connected = cable_connected
    c._cable_session_energy_kwh = 51.25   # sluttotalen från förra kabelsessionen i incidenten
    return c


def test_flag_survives_a_save_then_load_cycle():
    """Kärnan i Bug 44 genom den riktiga koden: koordinator A har sett Available (flagga True, kabel ur) och
    sparar; en ny koordinator B (flaggan False som efter en omladdning) laddar Store och ska få True."""
    store = h.FakeStore()
    a = _coordinator(store, cable_was_available=True, cable_connected=False)
    asyncio.run(a._save_state())

    b = _coordinator(store, cable_was_available=False, cable_connected=False)
    asyncio.run(b._load_state())

    assert b._cable_was_available is True


def test_saved_false_flag_is_written_and_read_back_even_when_cable_reads_as_out():
    """Faulted/Unavailable med kabeln i: cable_connected=False men ingen Available setts (flagga False).
    Flaggan måste faktiskt skrivas till Store – annars faller återställningen tillbaka på migreringsregeln
    (kabel ur → True) och nästa Preparing efter felet tas som genuin inkoppling och raderar en pågående
    sessions ackumulatorer. Skiljer 'sparad' från 'härledd ur cable_connected', vilket True-fallet ovan inte gör."""
    store = h.FakeStore()
    a = _coordinator(store, cable_was_available=False, cable_connected=False)
    asyncio.run(a._save_state())

    b = _coordinator(store, cable_was_available=False, cable_connected=False)
    asyncio.run(b._load_state())

    assert b._cable_was_available is False


def test_old_store_without_the_key_is_migrated_from_the_saved_cable_state():
    """Store från före Bug 44 (samma struktur som dagens _save_state, minus nyckeln). Kabeln inkopplad när den
    sparades → False (Bug 38-skyddet); urdragen → True. Fångar också att återställningen läser
    cable_connected EFTER att _load_state läst in den från Store – en färsk koordinators default är False."""
    for cable_connected, expected in ((True, False), (False, True)):
        store = h.FakeStore()
        writer = _coordinator(store, cable_was_available=False, cable_connected=cable_connected)
        asyncio.run(writer._save_state())
        store.data.pop("cable_was_available", None)   # så såg Store ut före Bug 44

        reader = _coordinator(store, cable_was_available=False, cable_connected=False)
        asyncio.run(reader._load_state())

        assert reader._cable_was_available is expected, f"cable_connected={cable_connected}"


if __name__ == "__main__":
    h.run_tests(globals())
