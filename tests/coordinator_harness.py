"""Delat testsele för koordinatortester (Bug 44/45/46): en RIKTIG OCPPCoordinator utan Home Assistant-instans.

Kräver Home Assistant (rot-venv: /mnt/c/temp/github/claude/venv/bin/python). Utan HA höjs Skip, och testfilernas
runner skriver SKIP i stället för FAIL.

Koordinatorn byggs med den riktiga __init__ (alla fält och standardvärden är därmed riktiga, inte handbyggda), mot en
MagicMock-hass och en MagicMock-config entry. Bara det som ligger under koordinatorn är fejkat: disken (Store),
hass-tjänsterna och tid (async_call_later). Anropas något av det koordinatorn själv äger, körs den riktiga koden.
"""
import asyncio
import json
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]

# Komponentens varningar (t.ex. "Charger ... disconnected") är väntade i testerna: en NullHandler hindrar Pythons
# lastResort-handler från att skriva dem mitt i PASS/FAIL-utskriften.
logging.getLogger("custom_components").addHandler(logging.NullHandler())

# Fordonsordning som i driftsatt konfiguration: eNiro först, Enyaq sparad som aktiv (Bug 46).
VEHICLES = [
    {"name": "Kia eNiro", "capacity_kwh": 64.0, "soc_entity": "sensor.e_niro_ev_battery_level",
     "soc_unit": "percent", "max_current_a": 0},
    {"name": "Skoda Enyaq", "capacity_kwh": 77.0, "soc_entity": "sensor.skoda_enyaq_battery_percentage",
     "soc_unit": "percent"},
]


class Skip(Exception):
    pass


def coordinator_class():
    sys.path.insert(0, str(ROOT))
    try:
        from custom_components.ocpp_charger import OCPPCoordinator
    except ImportError as exc:
        raise Skip(f"Home Assistant saknas ({exc}) – kör med rot-venv") from exc
    return OCPPCoordinator


class FakeStore:
    """Diskläget under HA:s Store. JSON-rundturen speglar att Store bara klarar JSON-serialiserbart."""

    def __init__(self, data=None):
        self.data = data

    async def async_save(self, data):
        self.data = json.loads(json.dumps(data))

    async def async_load(self):
        return self.data


def make_coordinator(store=None, *, vehicles=VEHICLES):
    """Riktig OCPPCoordinator. hass.async_create_task kör coroutinen på den löpande loopen (kräver att testet
    kör inuti asyncio); hass.states.get ger None (ingen SOC-entitet finns)."""
    cls = coordinator_class()
    hass = MagicMock()
    hass.config.time_zone = "Europe/Stockholm"
    hass.states.get.return_value = None
    hass.services.async_call = AsyncMock(return_value=None)   # t.ex. kia_uvo.force_update vid Available
    hass.async_create_task = lambda coro, *a, **k: asyncio.ensure_future(coro)
    entry = MagicMock()
    entry.entry_id = "test"
    entry.options = {}
    entry.data = {
        "charger_id": "GaroCS-TEST", "port": 9000, "host": "127.0.0.1", "max_current": 16.0,
        "num_phases": "3", "notify_on_connect": True, "vehicles": [dict(v) for v in vehicles],
    }
    coordinator = cls(hass, entry)
    coordinator._store = store if store is not None else FakeStore()
    return coordinator


def silence(coordinator, *names):
    """Byt ut collaborators som testet inte handlar om (planering, SOC-läsning, push av data) mot no-ops."""
    for name in names:
        setattr(coordinator, name, lambda *a, **k: None)
    return coordinator


def run_tests(namespace):
    """Gemensam runner: PASS/FAIL/SKIP, exit 1 vid fel."""
    tests = [v for k, v in sorted(namespace.items()) if k.startswith("test_")]
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
