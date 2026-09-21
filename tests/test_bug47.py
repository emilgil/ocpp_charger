"""Regressionstest Bug 47 – manuell start skickade en prisskalad ström (11 A) som skrev över 16 A.

Två fel som tillsammans gav 11 A hela sessionen (21/9 kl 15:12:54 lokal tid):
  A. async_start_charging() (Start-knappen och Immediate-auto-start) använde smart_controller.recommended_current(), som i
     Smart-läge skalar ned strömmen nära prisgränsen (11 A) eller över den (6 A). Auto-start skickar self.max_current.
  B. OCPPClient.set_charging_limit() registrerade _pending_limit_a först när boxen svarat. StartTransaction-handlern läser
     _pending_limit_a och hann före 16-svaret → skickade den föråldrade 11 A sist, och gränsen blev kvar på 11 A.

Körs med rot-venv (koordinatortester, se tests/coordinator_harness.py):
    /mnt/c/temp/github/claude/venv/bin/python tests/test_bug47.py
Utan Home Assistant hoppas de över (SKIP).
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, call

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coordinator_harness as h  # noqa: E402


# ── A: manuell start ─────────────────────────────────────────────────────────────────────────────────────────────────

def _smart_coordinator(price):
    """Riktig koordinator i Smart-läge, 16 A max och prishistorik 10..100 (tröskeln, 40:e percentilen, blir 50)."""
    h.coordinator_class()   # Skip utan HA; lägger även ROOT på sys.path
    from custom_components.ocpp_charger.const import CHARGE_MODE_SMART

    c = h.make_coordinator()
    c.charge_mode = CHARGE_MODE_SMART
    c.max_current = 16.0
    c.current_price = price
    c.smart_controller._price_history.clear()
    for p in range(10, 101, 10):
        c.smart_controller.update_price(float(p))
    c.ocpp.state.connected = True
    c.ocpp.set_charging_limit = AsyncMock(return_value=True)
    c.ocpp.remote_start_transaction = AsyncMock(return_value=True)
    c.async_refresh = AsyncMock()
    return c


def test_manual_start_sends_full_current_whatever_the_price():
    """Felet: Start-knappen fick 11 A (pris nära gränsen) eller 6 A (över gränsen) i stället för schemats 16 A."""
    for price, price_scaled_amps in ((49.0, 11), (80.0, 6), (10.0, 16)):
        c = _smart_coordinator(price)
        # Vakt: prisscenariot ska verkligen ge den gamla, prisskalade strömmen – annars testar vi ingenting.
        assert int(c.smart_controller.recommended_current(c.max_current, price, c.charge_mode)) == price_scaled_amps

        asyncio.run(c.async_start_charging())

        assert c.ocpp.set_charging_limit.await_args_list == [call(16.0)], f"pris {price}"
        assert c.current_limit_a == 16.0, f"pris {price}"
        c.ocpp.remote_start_transaction.assert_awaited_once()
        assert c._manual_start_requested is True   # manuell override oförändrad


# ── B: _pending_limit_a ──────────────────────────────────────────────────────────────────────────────────────────────

class FakeBox:
    """Fejkad laddbox. Varje ChangeConfiguration väntar på en Future som testet öppnar med reply(), så ordningen mellan
    'begärt' och 'besvarat' styrs exakt (det var den ordningen som gav 11 A). Övriga anrop (profil-fallbacken) avvisas."""

    def __init__(self):
        self.sent = []       # ströminställningar i den ordning de skickades
        self._replies = []   # en Future per skickat ChangeConfiguration

    async def send_call(self, action, payload, timeout=10.0):
        if action != "ChangeConfiguration":
            return {"status": "Rejected"}
        self.sent.append(int(payload["value"]))
        reply = asyncio.get_running_loop().create_future()
        self._replies.append(reply)
        return await reply

    def reply(self, index, status="Accepted"):
        self._replies[index].set_result({"status": status})


def _client_with_box():
    h.coordinator_class()   # Skip utan HA; lägger även ROOT på sys.path
    from custom_components.ocpp_charger.ocpp_client import OCPPClient

    client = OCPPClient("127.0.0.1", 0, "GaroCS-TEST", lambda state: None)
    box = FakeBox()
    client._send_call = box.send_call
    client._send_call_result = AsyncMock()
    return client, box


async def _tick():
    """Ge schemalagda uppgifter ett varv, så att en nyss startad set_charging_limit hinner skicka sitt anrop."""
    await asyncio.sleep(0)


async def _settle():
    """Låt kvarvarande uppgifter (t.ex. handlerns återapplicering) köras klart. Timeout: fel ska ge FAIL, inte hänga."""
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.wait_for(asyncio.gather(*pending), timeout=1.0)


def test_requested_limit_is_registered_before_the_box_replies():
    """StartTransaction-handlern läser _pending_limit_a: gränsen som senast BEGÄRTS ska stå där direkt."""

    async def scenario():
        client, box = _client_with_box()
        task = asyncio.ensure_future(client.set_charging_limit(16.0))
        await _tick()
        registered = client._pending_limit_a   # boxen har inte svarat än
        box.reply(0)
        await task
        return registered

    assert asyncio.run(scenario()) == 16.0


def test_start_transaction_reapplies_the_latest_requested_limit():
    """Felet (15:12:54): 11 A besvarat, sedan begärs 16 A, och innan 16-svaret hunnit hanteras kommer StartTransaction.
    Handlern läste det senast BESVARADE värdet (11) och skickade det sist → boxen blev kvar på 11 A."""

    async def scenario():
        client, box = _client_with_box()
        manual = asyncio.ensure_future(client.set_charging_limit(11.0))
        await _tick()
        box.reply(0)
        await manual                                                    # 11 A besvarat (den manuella starten)
        auto = asyncio.ensure_future(client.set_charging_limit(16.0))   # auto-start begär 16 A, svaret dröjer
        await _tick()
        await client._handle_call("1", "StartTransaction", {"connectorId": 1, "idTag": "HA_USER", "meterStart": 0})
        await _tick()                                                   # handlerns återapplicering hinner skicka
        box.reply(1)
        box.reply(2)
        await auto
        await _settle()
        return box.sent

    sent = asyncio.run(scenario())
    assert sent == [11, 16, 16], sent   # sista skickade värdet är det boxen behåller


if __name__ == "__main__":
    h.run_tests(globals())
