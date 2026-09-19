"""Feature 8: tolkning av service-data för ocpp_charger.clear_charging_profile.

Rena, stdlib-only hjälpfunktioner (som deadline.py och soc_estimate.py). Service-handlern i
__init__.py importerar Home Assistant och kan inte köras av de fristående testerna, så
tolkningen av service-datan och vakten mot att rensa ALLA laddprofiler ligger här.

Home Assistant varken validerar eller konverterar service-data (services.yaml är bara
UI-metadata). Värden kan därför vara strängar – t.ex. "off"/"false" från en mall eller ett
citerat YAML-värde – och en icke-tom sträng är sann i Python. Endast ett uttryckligt ja får
bekräfta "rensa alla profiler".
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_CONFIRM_YES = frozenset({"true", "yes", "on"})


def opt_int(value: Any) -> int | None:
    """None/tom sträng → None, annars int(value). 0 är ett giltigt värde.

    Ogiltig text (t.ex. "abc") ger ValueError och sväljs INTE – ett skräpvärde får inte
    tyst bli "inget filter".
    """
    if value is None or value == "":
        return None
    return int(value)


def parse_confirm(value: Any) -> bool:
    """True bara för ett uttryckligt ja: bool True eller texten true/yes/on (skiftlägesokänsligt).

    Allt annat är False – även "false"/"off"/"no" (sanna strängar) och alla tal (fail-safe).
    """
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in _CONFIRM_YES
    return False


def parse_clear_request(data: Mapping[str, Any]) -> dict | None:
    """Tolka call.data till nyckelordsargument för OCPPClient.clear_charging_profile.

    Returnerar dict med exakt nycklarna profile_id, connector_id, purpose och stack_level,
    eller None = AVVISA (inget filter och ingen uttrycklig bekräftelse via confirm_clear_all).
    """
    request = {
        "profile_id": opt_int(data.get("profile_id")),
        "connector_id": opt_int(data.get("connector_id")),
        "purpose": data.get("purpose") or None,
        "stack_level": opt_int(data.get("stack_level")),
    }
    no_filter = all(value is None for value in request.values())
    if no_filter and not parse_confirm(data.get("confirm_clear_all", False)):
        return None
    return request


def refused_result() -> dict:
    """Svaret när clear_charging_profile avvisas. Ny dict (och ny inre dict) vid varje anrop."""
    return {
        "status": "Refused",
        "request": {},
        "error": "Inga filter angivna. Sätt confirm_clear_all: true för att rensa alla profiler.",
    }
