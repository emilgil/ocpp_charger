"""Regressionstest Bug 43 – laddstartstiden persisteras inte över en HA-omstart.

Körs fristående utan Home Assistant:
    python3 tests/test_bug43.py

charging_start.py importerar bara stdlib, så vi lägger modulkatalogen på
sys.path och importerar modulen direkt (paketets __init__.py drar in
HA-beroenden och ska INTE importeras).

Testar den rena (de)serialiseringen av ``_charging_started_at``. Nyckelvalet:
ett återställt värde ska bara tas emot om det går att lita på – annars ska
koordinatorn falla tillbaka på dagens beteende (start-grenen sätter "nu").
Ett dåligt värde från Store får aldrig krascha uppstarten eller låta en gammal
kabelsessions starttid återuppstå.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "custom_components" / "ocpp_charger"),
)
import charging_start as cs  # noqa: E402

UTC = timezone.utc
START = datetime(2026, 9, 19, 10, 48, tzinfo=UTC)      # verklig laddstart i Bug 42-fallet
NOW = datetime(2026, 9, 19, 11, 37, 22, tzinfo=UTC)    # omstartstiden i Bug 42-deployen


def restore(raw, **kwargs):
    kwargs.setdefault("cable_connected", True)
    return cs.restore_charging_start(raw, NOW, **kwargs)


def test_serialized_start_keeps_the_utc_offset():
    """Store-formatet är ISO med offset. restore() avvisar naiva tider, så en
    serialisering som tappar tidszonen skulle göra att inget någonsin återställs."""
    assert cs.serialize_charging_start(START) == "2026-09-19T10:48:00+00:00"


def test_no_start_serializes_to_none():
    """Ingen laddstart (före start / efter urkoppling) ska sparas som None, inte kraschas på."""
    assert cs.serialize_charging_start(None) is None


def test_restore_round_trips_the_saved_start():
    assert restore(cs.serialize_charging_start(START)) == START


def test_restore_treats_other_offsets_as_the_same_instant():
    """12:48 i CEST (+02:00) är samma ögonblick som 10:48 UTC."""
    assert restore("2026-09-19T12:48:00+02:00") == START


def test_restore_ignores_the_saved_start_when_the_cable_was_out():
    """Kabeln var urkopplad vid sparandet → ingen session att återuppta.
    Fångar en gammal starttid som överlever en urkoppling."""
    assert restore(cs.serialize_charging_start(START), cable_connected=False) is None


def test_restore_never_raises_on_corrupt_store_data():
    """Trasigt Store-innehåll får inte fälla uppstarten (_load_state körs vid HA-start)."""
    for raw in (None, "", "not-a-date", 12345, ["2026-09-19T10:48:00+00:00"], "2026-13-45T99:00:00+00:00"):
        assert restore(raw) is None, raw


def test_restore_rejects_a_naive_timestamp():
    """En tid utan tidszon är tvetydig; aware-naive-aritmetik skulle dessutom kasta TypeError."""
    assert restore("2026-09-19T10:48:00") is None


def test_restore_rejects_a_start_in_the_future():
    """Klockdrift eller skräp: en start efter 'nu' är omöjlig."""
    future = NOW + timedelta(minutes=1)
    assert restore(cs.serialize_charging_start(future)) is None


def test_restore_keeps_a_recent_start_but_drops_one_older_than_a_day():
    """Om HA låg nere medan kabeln byttes får en gammal sessions starttid inte återuppstå
    som ett dygnslångt Laddfönster. 23 h tillbaka är rimligt, 25 h är det inte."""
    recent = NOW - timedelta(hours=23)
    stale = NOW - timedelta(hours=25)
    assert restore(cs.serialize_charging_start(recent)) == recent
    assert restore(cs.serialize_charging_start(stale)) is None


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
