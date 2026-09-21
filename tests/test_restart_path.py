"""Bug 44 + 45 + 46 tillsammans: omstartsvägen, spelad genom riktiga koordinatormetoder.

Incidenten 21/9: integrationen laddades om, Garo återanslöt 22–31 s senare och skickade inte om sin status. Status stod
på Unknown i ett dygn (Bug 45), flaggan `_cable_was_available` var glömd (Bug 44), och den felklassade inkopplingen
tog Bug 41-grenen eftersom återställningen av sparat fordon räknats som byte (Bug 46) – Session Energy nollställdes
inte och fortsatte från förra sessionens 51.25 kWh.

Varje test startar en färsk koordinator mot en Store skriven före omladdningen, kör startup-timern (+10 s), låter
laddaren ansluta sent och spelar sedan upp vad Garo gör. Den riktiga _check_notify_events körs; bara nätverket
(TriggerMessage), telefonen (notifier) och timers är fejkade.

Körs med rot-venv:  /mnt/c/temp/github/claude/venv/bin/python tests/test_restart_path.py
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coordinator_harness as h  # noqa: E402

QUIET = ("_update_price_from_ha", "_apply_current_schedule", "_update_soc_from_ha", "_update_smart_charging",
         "async_set_updated_data", "_seed_price_history", "_update_charge_plan")
CABLE_IN = {"Preparing", "Charging", "SuspendedEV", "SuspendedEVSE", "Finishing"}   # som ocpp_client.py


def _module():
    h.coordinator_class()
    import custom_components.ocpp_charger as mod
    return mod


class Garo:
    """Laddaren. status() gör vad ocpp_client gör vid en StatusNotification; på TriggerMessage svarar den med
    `reply` (None = svarar aldrig)."""

    def __init__(self, coordinator, reply):
        self.c = coordinator
        self.reply = reply
        self.triggers = 0
        coordinator.ocpp.trigger_status_notification = self._trigger

    async def _trigger(self):
        self.triggers += 1
        if self.reply:
            self.status(self.reply)

    def connect(self):
        self.c.ocpp.state.connected = True
        self.c._on_charger_state_update_async(self.c.ocpp.state)

    def status(self, status):
        state = self.c.ocpp.state
        state.connector_status = status
        state.cable_connected = status in CABLE_IN
        state.charging = status == "Charging"
        self.c._on_charger_state_update_async(state)


def _store_before_reload(*, cable_in, flag, mid_session=False):
    """Store som den såg ut när integrationen laddades om: Enyaq aktiv, förra kabelsessionens 51.25 kWh kvar."""
    store = h.FakeStore()
    w = h.make_coordinator(store)
    w.active_vehicle = w._vehicles[1]
    w._cable_session_energy_kwh = 51.25
    w._cable_was_available = flag
    w.ocpp.state.cable_connected = cable_in
    if mid_session:
        w._session_total_kwh = 12.3
        w._session_start_soc = 41.0
        w.ocpp.state.energy_kwh = 8.1
    asyncio.run(w._save_state())
    return store


def _reload(store, reply, script):
    """Ladda om: färsk koordinator, startup-timern (+10 s), sedan `script(c, garo, timers)`; timers är de
    (fördröjning, åtgärd) koordinatorn schemalagt och som scriptet själv får köra "när tiden gått". Returnerar
    (coordinator, garo). Retry-avståndet nollas; tid (async_call_later) är fejkad."""
    mod = _module()
    timers = []

    async def run():
        c = h.silence(h.make_coordinator(store), *QUIET)
        c.notifier = MagicMock()   # telefonen
        garo = Garo(c, reply)

        async def _noop(*a, **k):
            return None

        c.ocpp.start = _noop
        c._setup_mqtt = _noop
        await c.async_start()
        (delay, refresh), = timers
        assert delay == 10
        timers.clear()
        await refresh()            # +10 s: Store laddad, laddaren inte ansluten än
        await script(c, garo, timers)
        return c, garo

    with patch.object(mod, "async_call_later", lambda hass, delay, action: timers.append((delay, action))), \
            patch.object(mod, "STATUS_TRIGGER_RETRY_SECONDS", 0, create=True):
        c, garo = asyncio.run(run())
    return c, garo


async def _settle():
    for _ in range(50):
        await asyncio.sleep(0)


def test_late_connecting_charger_answers_available_and_the_next_plug_in_resets_the_cable_session():
    """Huvudfallet. Kabeln urdragen vid omladdningen; Garo ansluter sent, får en TriggerMessage och svarar Available.
    När kabeln sedan kopplas in är det en genuin inkoppling: Session Energy nollställs och 'Inkopplad' pushas en gång.
    Trigger-svarets Available får inte ge en falsk stopp-push (kabelsessionens 51.25 kWh ligger kvar tills nästa
    inkoppling, och _send_stop_notification körs vid Available)."""
    store = _store_before_reload(cable_in=False, flag=True)

    async def script(c, garo, timers):
        garo.connect()
        await _settle()
        assert c.ocpp.state.connector_status == "Available"     # Bug 45: status blev känd
        for delay, action in timers:                            # 60 s senare: den fördröjda stopp-notisen
            assert delay == 60
            await action()                                      # ...avbryts, kabeln är fortfarande ur (Bug 12)
        timers.clear()
        c.notifier.on_charging_stopped.assert_not_called()
        garo.status("Preparing")                                # kabeln kopplas in

    c, garo = _reload(store, "Available", script)

    assert c._cable_session_energy_kwh == 0.0
    assert c.notifier.on_cable_connected.call_count == 1
    c.notifier.on_charging_stopped.assert_not_called()


def test_charger_that_never_answers_the_trigger_still_resets_the_cable_session_at_the_next_plug_in():
    """Bug 44:s skyddsnät: svarar Garo aldrig på TriggerMessage (tre försök) står status kvar på Unknown, precis som
    21/9. Ändå ska nästa inkoppling vara genuin – flaggan överlevde omladdningen – och Bug 41-grenen (Bug 46) inte
    tas."""
    store = _store_before_reload(cable_in=False, flag=True)

    async def script(c, garo, timers):
        garo.connect()
        await _settle()
        assert garo.triggers == 3                               # Bug 45: försökte, gav upp
        assert c.ocpp.state.connector_status == "Unknown"
        garo.status("Preparing")

    c, garo = _reload(store, None, script)

    assert c._cable_session_energy_kwh == 0.0
    assert c._vehicle_switch_pending_reset is False
    assert c.notifier.on_cable_connected.call_count == 1


def test_restart_in_the_middle_of_a_paused_session_keeps_everything_when_garo_reports_preparing():
    """Bug 38-skyddet genom hela kedjan: omstart mitt i en kabelsession (kabeln i, flaggan False, 12.3 kWh energibas, 8.1
    kWh i pågående transaktion). Trigger-svaret Preparing är den första kända statusen, inte en inkoppling och inte
    en Garo-reset: ingen falsk 'Inkopplad'-push, kabelsessionens ackumulatorer och energibasen orörda."""
    store = _store_before_reload(cable_in=True, flag=False, mid_session=True)

    async def script(c, garo, timers):
        garo.connect()
        await _settle()

    c, garo = _reload(store, "Preparing", script)

    assert c.ocpp.state.connector_status == "Preparing"
    assert c._cable_session_energy_kwh == 51.25
    assert c._session_total_kwh == 12.3
    assert c._cable_was_available is False
    assert c.notifier.on_cable_connected.call_count == 0


if __name__ == "__main__":
    h.run_tests(globals())
