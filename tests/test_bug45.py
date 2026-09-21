"""Regressionstest Bug 45 – anslutningsstatus förblir `Unknown` efter HA-omstart/omladdning.

Garo återansluter 22–31 s efter att OCPP-servern kommit upp och skickar inte om StatusNotification. Startup-triggern
(`TriggerMessage`, +10 s) skickades bara om laddaren redan var ansluten då → status `Unknown` i timmar, ingen `Available`
observerades (Bug 44) och `Preparing` klassades fel.

  Del 1  TriggerMessage vid varje (åter)anslutning efter att Store lästs, med retry medan status är Unknown.
  Del 2  Första kända status efter en omstart är en resync, inte en Garo-reset: ackumulera inte energin en gång till.
  Del 3  En gammal anslutnings `finally` får inte städa en nyare anslutning (stale handler).

Körs med rot-venv (koordinatortester, se tests/coordinator_harness.py):
    /mnt/c/temp/github/claude/venv/bin/python tests/test_bug45.py
Utan Home Assistant hoppas de över (SKIP).
"""
import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coordinator_harness as h  # noqa: E402

# Collaborators som testerna inte handlar om (planering, SOC, prishistorik, push av data)
QUIET = ("_update_price_from_ha", "_apply_current_schedule", "_update_soc_from_ha", "_update_smart_charging",
         "_check_notify_events", "async_set_updated_data", "_seed_price_history")


def _module():
    h.coordinator_class()   # SKIP utan HA; lägger repo-roten på sys.path
    import custom_components.ocpp_charger as mod
    return mod


class Charger:
    """Ersätter nätverkslagret: räknar TriggerMessage och kan låtsas svara med en StatusNotification.
    replies = {anropsnummer: connector_status som Garo svarar med}; anrop utan svar lämnar status oförändrad."""

    def __init__(self, coordinator, replies=None):
        self.calls = 0
        self.replies = replies or {}
        self._state = coordinator.ocpp.state
        coordinator.ocpp.trigger_status_notification = self

    async def __call__(self):
        self.calls += 1
        if self.calls in self.replies:
            self._state.connector_status = self.replies[self.calls]


async def _settle():
    for _ in range(50):
        await asyncio.sleep(0)


async def _startup(coordinator, mod):
    """async_start() med fejkad OCPP-server/MQTT/timer. Returnerar den schemalagda _delayed_soc_refresh (+10 s)."""
    async def _noop(*a, **k):
        return None

    coordinator.ocpp.start = _noop
    coordinator._setup_mqtt = _noop
    scheduled = []
    with patch.object(mod, "async_call_later", lambda hass, delay, action: scheduled.append((delay, action))):
        await coordinator.async_start()
    (delay, action), = scheduled
    assert delay == 10
    return action


def _connect(coordinator, connected):
    coordinator.ocpp.state.connected = connected
    coordinator._on_charger_state_update_async(coordinator.ocpp.state)


def _scenario(replies, steps):
    """Bygger en koordinator, kör startup-timern och sedan `steps(coordinator, charger, refresh)`; returnerar antalet
    TriggerMessage. Retry-avståndet nollas så att testet inte väntar 10 s (samma kodväg, kortare sömn)."""
    mod = _module()

    async def run():
        c = h.silence(h.make_coordinator(), *QUIET)
        charger = Charger(c, replies)
        refresh = await _startup(c, mod)
        await steps(c, charger, refresh)
        await _settle()
        return charger.calls

    with patch.object(mod, "STATUS_TRIGGER_RETRY_SECONDS", 0, create=True):
        return asyncio.run(run())


# ── Del 1: TriggerMessage vid (åter)anslutning ───────────────────────────────────────────────────────────


def test_a_charger_that_connects_after_the_startup_load_gets_a_trigger():
    """Felet: laddaren ansluter 22–31 s efter serverstart, efter startup-timern (+10 s). Ingen kodväg skickade då
    någon TriggerMessage → status Unknown i timmar."""
    async def steps(c, charger, refresh):
        await refresh()                       # +10 s: Store laddad, laddaren inte ansluten än
        await _settle()
        assert charger.calls == 0
        _connect(c, True)                     # Garo ansluter

    assert _scenario({1: "Available"}, steps) == 1


def test_a_charger_that_connected_before_the_startup_load_gets_exactly_one_trigger_after_it():
    """Ansluter laddaren före Store-laddningen ska ingen trigger skickas då (_load_state skulle skriva över en färsk
    status); startup-timern skickar den efteråt. Därefter ingen ny per hjärtslag/statusuppdatering."""
    async def steps(c, charger, refresh):
        _connect(c, True)                     # ansluter före +10 s
        await _settle()
        assert charger.calls == 0
        await refresh()
        await _settle()
        assert charger.calls == 1
        c._on_charger_state_update_async(c.ocpp.state)   # hjärtslag: fortfarande ansluten

    assert _scenario({1: "Available"}, steps) == 1


def test_every_reconnect_triggers_again_even_when_the_status_is_already_known():
    """Beslut 2026-09-21: trigger vid varje återanslutning, inte bara när status är Unknown."""
    async def steps(c, charger, refresh):
        await refresh()
        _connect(c, True)
        await _settle()
        assert c.ocpp.state.connector_status == "Available"   # känd status efter första svaret
        _connect(c, False)
        _connect(c, True)                                      # återanslutning

    assert _scenario({1: "Available", 2: "Available"}, steps) == 2


def test_status_that_stays_unknown_is_retried_but_only_three_times():
    """Garo kan svara Accepted utan att skicka StatusNotification (t.ex. före handshaken är klar). Försök igen, men
    inte i evighet."""
    async def steps(c, charger, refresh):
        await refresh()
        _connect(c, True)

    assert _scenario({}, steps) == 3


def test_retry_stops_as_soon_as_the_status_is_known():
    async def steps(c, charger, refresh):
        await refresh()
        _connect(c, True)

    assert _scenario({2: "Available"}, steps) == 2


def test_retry_stops_when_the_charger_disconnects():
    """Ingen mening att fråga en laddare som försvunnit (och efter en unload är socketen borta): varje ytterligare
    försök vore bara en 'ingen ansluten laddare'-varning."""
    async def steps(c, charger, refresh):
        await refresh()
        _connect(c, True)
        await asyncio.sleep(0)   # första försöket skickas, svaret uteblir
        _connect(c, False)

    assert _scenario({}, steps) == 1


# ── Del 2: första kända status efter omstart är en resync ────────────────────────────────────────────────


def _restored_mid_session():
    """Omstart mitt i en kabelsession: Store har 12.3 kWh energibas (Bug 30), 8.1 kWh i den pågående transaktionen och
    flaggan False (kabeln inkopplad). Fordonsbytesflaggan är inte armerad tack vare Bug 46."""
    store = h.FakeStore()
    writer = h.make_coordinator(store)
    writer.active_vehicle = writer._vehicles[1]
    writer._session_total_kwh = 12.3
    writer._session_start_soc = 41.0
    writer.ocpp.state.energy_kwh = 8.1
    writer.ocpp.state.cable_connected = True
    writer._cable_was_available = False
    asyncio.run(writer._save_state())

    reader = h.silence(h.make_coordinator(store), "_update_soc_from_ha", "_update_charge_plan", "async_set_updated_data")
    asyncio.run(reader._load_state())
    return reader


def test_first_preparing_after_a_restart_does_not_double_count_the_restored_energy():
    """TriggerMessage-svaret är en StatusNotification med nuvarande status. Är den Preparing och ingen status var känd
    förut (bara en omstart, ingen verklig ändring) tolkades det som Garo-reset: state.energy_kwh (8.1, redan inräknad i
    de återställda 12.3) lades på en gång till → 20.4 och ett SOC-estimat som ligger för högt."""
    for last_status in ("", "Unknown"):   # "" före första uppdateringen, "Unknown" efter den
        c = _restored_mid_session()
        c.ocpp.state.connector_status = "Preparing"
        c._last_connector_status_notify = last_status

        c._check_notify_events()

        assert c._session_total_kwh == 12.3, f"föregående status {last_status!r}"


# ── Del 3: gammal anslutnings finally städar inte en nyare ───────────────────────────────────────────────


class FakeWebSocket:
    """Gränsen mot websockets-biblioteket: en anslutning som avslutas när drop() anropas."""

    def __init__(self, charger_id="GaroCS-TEST"):
        self.request = types.SimpleNamespace(path=f"/{charger_id}")
        self.remote_address = ("192.168.1.111", 39324)
        self._queue = asyncio.Queue()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if await self._queue.get() is None:
            raise StopAsyncIteration
        raise AssertionError("testet skickar inga meddelanden")

    def drop(self):
        self._queue.put_nowait(None)

    async def close(self, *args):
        self.drop()


def _client():
    _module()
    from custom_components.ocpp_charger.ocpp_client import OCPPClient
    return OCPPClient("0.0.0.0", 9000, "GaroCS-TEST", state_callback=lambda state: None)


def test_an_old_connection_closing_does_not_wipe_a_newer_one():
    """Garo återansluter medan HA:s gamla socket är halvöppen. När den gamla handlern så småningom avslutas körde dess
    finally ovillkorligen städningen: connected=False, _ws=None, effekt nollad – trots att den nya anslutningen lever
    (RemoteStart/ChangeConfiguration går då ingenstans)."""
    async def run():
        client = _client()
        old, new = FakeWebSocket(), FakeWebSocket()
        old_task = asyncio.create_task(client._handle_charger(old))
        await asyncio.sleep(0)
        new_task = asyncio.create_task(client._handle_charger(new))
        await asyncio.sleep(0)
        client.state.charging = True
        client.state.power_w = 7400.0

        old.drop()
        await old_task

        result = (client.state.connected, client._ws is new, client.state.charging, client.state.power_w)
        new.drop()
        await new_task
        return result

    assert asyncio.run(run()) == (True, True, True, 7400.0)


def test_the_current_connection_closing_still_cleans_up():
    """Den vanliga vägen är oförändrad: när den aktuella anslutningen stängs nollas state och _ws."""
    async def run():
        client = _client()
        ws = FakeWebSocket()
        task = asyncio.create_task(client._handle_charger(ws))
        await asyncio.sleep(0)
        client.state.charging = True
        client.state.power_w = 7400.0

        ws.drop()
        await task
        return (client.state.connected, client._ws, client.state.charging, client.state.power_w)

    assert asyncio.run(run()) == (False, None, False, 0.0)


if __name__ == "__main__":
    h.run_tests(globals())
