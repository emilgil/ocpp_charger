"""Regressionstest Bug 44 – kabelsessionens ackumulatorer nollställdes inte vid inkoppling efter omstart.

`_cable_was_available` (Bug 13A/38: "en äkta Available har setts sedan förra inkopplingen") sparades inte i
Store. Efter en (om)laddning av integrationen var flaggan False, Garo skickade inte om StatusNotification vid
reconnect, och nästa `Preparing` klassades som Garo-reset i stället för genuin inkoppling – så
`_cable_session_energy_kwh`/`_cable_session_cost_sek` nollställdes aldrig.

Körs:
    python3 tests/test_bug44.py
        ren logik (cable_flag.py, stdlib-only). Koordinatortesterna hoppas över (SKIP) – Home Assistant saknas.
    /mnt/c/temp/github/claude/venv/bin/python tests/test_bug44.py
        även round-trip genom den riktiga OCPPCoordinator._save_state/_load_state (HA 2025.1.4 i rot-venv).

Koordinatortesterna bygger en OCPPCoordinator utan __init__ (den startar OCPP-server, MQTT m.m.) och ger den bara
de fält _save_state/_load_state läser. Under dem ligger bara Store-lagret fejkat (disken); nycklar och
JSON-serialisering är riktiga.
"""
import asyncio
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "custom_components" / "ocpp_charger"
sys.path.insert(0, str(PKG))
import cable_flag as cf  # noqa: E402


class Skip(Exception):
    pass


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


class FakeStore:
    """Diskläget under HA:s Store. JSON-rundturen speglar att Store bara klarar JSON-serialiserbart."""

    def __init__(self):
        self.data = None

    async def async_save(self, data):
        self.data = json.loads(json.dumps(data))

    async def async_load(self):
        return self.data


def _coordinator_class():
    sys.path.insert(0, str(ROOT))
    try:
        from custom_components.ocpp_charger import OCPPCoordinator
    except ImportError as exc:
        raise Skip(f"Home Assistant saknas ({exc}) – kör med rot-venv") from exc
    return OCPPCoordinator


def _coordinator(store, *, cable_was_available, cable_connected):
    """OCPPCoordinator utan __init__, med bara de fält _save_state/_load_state rör vid."""
    coordinator_cls = _coordinator_class()   # först: lägger repo-roten på sys.path, SKIP utan HA
    from custom_components.ocpp_charger.ocpp_client import ChargerState

    c = object.__new__(coordinator_cls)
    c._store = store
    c.ocpp = types.SimpleNamespace(state=ChargerState(cable_connected=cable_connected))
    c._cable_was_available = cable_was_available
    c._cable_session_energy_kwh = 51.25   # sluttotalen från förra kabelsessionen i incidenten
    c._cable_session_cost_sek = 0.0
    c._session_start_soc = None
    c._session_total_kwh = 0.0
    c._session_plan_intervals = None
    c._charging_started_at = None
    c._last_cost_energy_kwh = 0.0
    c._day_charging_manual_override = False
    c.charge_mode = "Smart (price-optimised)"
    c.price_cap_ore_kwh = 0.0
    c.target_soc = 80.0
    c.target_kwh = 0.0
    c.allow_day_charging = False
    c.active_vehicle = None
    return c


def test_flag_survives_a_save_then_load_cycle():
    """Kärnan i Bug 44 genom den riktiga koden: koordinator A har sett Available (flagga True, kabel ur) och
    sparar; en ny koordinator B (flaggan False som efter en omladdning) laddar Store och ska få True."""
    store = FakeStore()
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
    store = FakeStore()
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
        store = FakeStore()
        writer = _coordinator(store, cable_was_available=False, cable_connected=cable_connected)
        asyncio.run(writer._save_state())
        store.data.pop("cable_was_available", None)   # så såg Store ut före Bug 44

        reader = _coordinator(store, cable_was_available=False, cable_connected=False)
        asyncio.run(reader._load_state())

        assert reader._cable_was_available is expected, f"cable_connected={cable_connected}"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = skipped = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Skip as exc:
            skipped += 1
            print(f"SKIP  {t.__name__}: {exc}")
        except Exception as exc:  # AssertionError = fel resultat, annat = kraschade
            failed += 1
            print(f"FAIL  {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed - skipped} passed, {failed} failed, {skipped} skipped")
    sys.exit(1 if failed else 0)
