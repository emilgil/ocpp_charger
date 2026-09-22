"""Feature 10 – notifier: vals-notifisen ("vilken bil laddar?") och rensningen av den.

Riktig ChargerNotifier mot en MagicMock-hass; payloaden läses ur hass.services.async_call.
    /mnt/c/temp/github/claude/venv/bin/python tests/test_feature10_notifier.py
Utan Home Assistant hoppas de över (SKIP).
"""
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coordinator_harness as h  # noqa: E402

TARGET = "notify.mobile_app_sm_s918b"
VEHICLES = [{"name": "Kia eNiro"}, {"name": "Skoda Enyaq"}]


def _notifier(*, enabled=True, target=TARGET, url=""):
    h.coordinator_class()   # SKIP utan HA; lägger repo-roten på sys.path
    from custom_components.ocpp_charger.notifier import ChargerNotifier
    hass = MagicMock()
    return ChargerNotifier(hass, target, enabled, url), hass


def _sent(hass):
    """(tjänstnamn, payload) för det enda notify-anropet."""
    assert hass.services.async_call.call_count == 1
    domain, service, payload = hass.services.async_call.call_args.args
    assert domain == "notify"
    return service, payload


def test_message_text_per_reason():
    expected = {
        "multiple_plugged": "Flera bilar visar sig vara inkopplade. Välj vilken som laddar.",
        "partial_sensors": "Kan inte avgöra vilken bil som är inkopplad (alla bilar saknar inkopplad-sensor). Välj bil.",
        "none_plugged": "Ingen bil visar sig vara inkopplad. Välj vilken som laddar.",
    }
    for code, text in expected.items():
        n, hass = _notifier()
        n.on_vehicle_selection_needed(code, VEHICLES, "Kia eNiro")
        service, payload = _sent(hass)
        assert service == "mobile_app_sm_s918b"
        assert payload["message"] == text, code


def test_one_button_per_vehicle_reusing_the_select_prefix_and_marking_the_active_one():
    n, hass = _notifier()
    n.on_vehicle_selection_needed("multiple_plugged", VEHICLES, "Skoda Enyaq")
    _, payload = _sent(hass)
    assert payload["data"]["actions"] == [
        {"action": "ocpp_select_vehicle_0", "title": "Kia eNiro"},
        {"action": "ocpp_select_vehicle_1", "title": "Skoda Enyaq ✓"},
    ]


def test_own_tag_so_it_is_replaced_and_cleared_separately():
    n, hass = _notifier()
    n.on_vehicle_selection_needed("none_plugged", VEHICLES, "Kia eNiro")
    _, payload = _sent(hass)
    assert payload["data"]["tag"] == "ocpp_vehicle_select"
    assert payload["data"]["tag"] != "ocpp_cable_connected"


def test_dashboard_url_is_injected_like_in_the_other_notifications():
    n, hass = _notifier(url="https://ha.example/lovelace/ev")
    n.on_vehicle_selection_needed("none_plugged", VEHICLES, "Kia eNiro")
    _, payload = _sent(hass)
    assert payload["data"]["url"] == payload["data"]["clickAction"] == "https://ha.example/lovelace/ev"


def test_an_unknown_reason_code_still_asks():
    n, hass = _notifier()
    n.on_vehicle_selection_needed("something_else", VEHICLES, "Kia eNiro")
    _, payload = _sent(hass)
    assert "Välj bil" in payload["message"]


def test_without_a_notification_target_nothing_is_sent_and_a_warning_is_logged():
    logger = logging.getLogger("custom_components.ocpp_charger.notifier")

    class Capture(logging.Handler):
        def __init__(self):
            super().__init__(logging.WARNING)
            self.records = []

        def emit(self, record):
            self.records.append(record)

    for kwargs in ({"enabled": False}, {"target": ""}):
        n, hass = _notifier(**kwargs)
        capture = Capture()
        logger.addHandler(capture)
        try:
            n.on_vehicle_selection_needed("multiple_plugged", VEHICLES, "Kia eNiro")
        finally:
            logger.removeHandler(capture)
        hass.services.async_call.assert_not_called()
        assert [r.levelno for r in capture.records] == [logging.WARNING], kwargs
        assert "multiple_plugged" in capture.records[0].getMessage()


def test_clear_sends_clear_notification_with_the_tag():
    n, hass = _notifier()
    n.clear_vehicle_selection_notification()
    service, payload = _sent(hass)
    assert service == "mobile_app_sm_s918b"
    assert payload == {"message": "clear_notification", "data": {"tag": "ocpp_vehicle_select"}}


def test_clear_does_nothing_without_a_target():
    n, hass = _notifier(target="")
    n.clear_vehicle_selection_notification()
    hass.services.async_call.assert_not_called()


def test_dismiss_cable_connected_sends_clear_notification_with_the_tag():
    n, hass = _notifier()
    n.dismiss_cable_connected_notification()
    service, payload = _sent(hass)
    assert service == "mobile_app_sm_s918b"
    assert payload == {"message": "clear_notification", "data": {"tag": "ocpp_cable_connected"}}


def test_dismiss_cable_connected_does_nothing_without_a_target():
    n, hass = _notifier(target="")
    n.dismiss_cable_connected_notification()
    hass.services.async_call.assert_not_called()


def test_cable_connected_and_its_dismiss_share_the_same_tag():
    """[Bug] on_cable_connected's vehicle-select notification used a bare string literal with no matching dismiss
    method anywhere in the codebase, so it could never be cleared programmatically (only replaced by the next
    cable session's copy). This pins the two call sites to the same constant so they can't drift apart again."""
    n, hass = _notifier()
    n.on_cable_connected(None, None, None, None, None, vehicles=VEHICLES)
    _, connect_payload = _sent(hass)
    hass.services.async_call.reset_mock()

    n.dismiss_cable_connected_notification()
    _, dismiss_payload = _sent(hass)

    assert connect_payload["data"]["tag"] == dismiss_payload["data"]["tag"] == "ocpp_cable_connected"


if __name__ == "__main__":
    h.run_tests(globals())
