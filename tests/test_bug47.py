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


if __name__ == "__main__":
    h.run_tests(globals())
