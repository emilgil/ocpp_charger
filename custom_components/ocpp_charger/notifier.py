"""Push notifications via Home Assistant notify services."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

_LOGGER = logging.getLogger(__name__)


class ChargerNotifier:
    """Sends HA notify service calls for charger events."""

    def __init__(self, hass, notify_target: str, enabled: bool = True) -> None:
        self.hass = hass
        self.notify_target = notify_target   # e.g. "notify.mobile_app_my_phone"
        self.enabled = enabled

    def _send(self, title: str, message: str) -> None:
        if not self.enabled or not self.notify_target:
            return
        try:
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "notify",
                    self.notify_target.replace("notify.", "", 1),
                    {"title": title, "message": message},
                )
            )
            _LOGGER.info("[Notify] %s: %s", title, message)
        except Exception as err:
            _LOGGER.warning("[Notify] Failed to send notification: %s", err)

    def on_cable_connected(
        self,
        soc_pct: float | None,
        plan_start: datetime | None,
        plan_end: datetime | None,
        energy_kwh: float | None,
        estimated_cost_sek: float | None,
        vehicle_name: str = "",
        detection_reason: str = "",
    ) -> None:
        """Notify when cable is plugged in."""
        header = "🔌 Laddkabel inkopplad"
        if vehicle_name:
            header += f" – {vehicle_name}"
        lines = [header]
        if detection_reason:
            lines.append(f"({detection_reason})")

        if soc_pct is not None:
            lines.append(f"Batterinivå: {soc_pct:.0f}%")

        if plan_start and plan_end:
            start_str = _fmt_time(plan_start)
            end_str   = _fmt_time(plan_end)
            lines.append(f"Planerad laddning: {start_str}–{end_str}")
        else:
            lines.append("Laddplan: ej beräknad ännu")

        if energy_kwh is not None:
            lines.append(f"Energibehov: {energy_kwh:.1f} kWh")

        if estimated_cost_sek is not None:
            lines.append(f"Beräknad kostnad: {estimated_cost_sek:.2f} SEK")

        self._send("EV Laddning – Inkopplad", "\n".join(lines))

    def on_charging_started(
        self,
        soc_pct: float | None,
        current_a: float,
        power_kw: float,
        plan_end: datetime | None = None,
        estimated_end: datetime | None = None,
    ) -> None:
        """Notify when charging session starts."""
        lines = ["⚡ Laddning startad"]

        if soc_pct is not None:
            lines.append(f"Batterinivå: {soc_pct:.0f}%")

        lines.append(f"Laddström: {current_a:.0f} A  ({power_kw:.1f} kW)")

        # Bug 2: prioritize plan_end over ETA estimate
        end_time = plan_end or estimated_end
        if end_time:
            lines.append(f"Beräknat klart: {_fmt_time(end_time)}")

        self._send("EV Laddning – Startad", "\n".join(lines))

    def on_charging_stopped(
        self,
        soc_pct: float | None,
        energy_kwh: float,
        actual_cost_sek: float,
        duration_minutes: int,
    ) -> None:
        """Notify when charging session ends."""
        lines = ["✅ Laddning avslutad"]

        if soc_pct is not None:
            lines.append(f"Batterinivå: {soc_pct:.0f}%")

        lines.append(f"Energi levererad: {energy_kwh:.2f} kWh")
        lines.append(f"Kostnad: {actual_cost_sek:.2f} SEK")
        lines.append(f"Tid: {duration_minutes} min")

        self._send("EV Laddning – Avslutad", "\n".join(lines))


    def on_day_charging_chosen(
        self,
        day_start: datetime,
        day_end: datetime,
        day_cost: float,
        day_avg_ore: float,
        night_start: datetime | None,
        night_end: datetime | None,
        night_cost: float | None,
        night_avg_ore: float | None,
    ) -> None:
        """Actionable notification: user can choose day or night charging."""
        from .const import NOTIFY_ACTION_USE_DAY, NOTIFY_ACTION_USE_NIGHT, NOTIFY_ACTION_DISMISS

        day_line = (
            f"☀️ Dag {_fmt_time(day_start)}–{_fmt_time(day_end)}"
            f" · {day_avg_ore:.1f} öre/kWh · ≈{day_cost:.2f} SEK"
        )
        if night_start and night_end and night_cost is not None:
            night_line = (
                f"🌙 Natt {_fmt_time(night_start)}–{_fmt_time(night_end)}"
                f" · {night_avg_ore:.1f} öre/kWh · ≈{night_cost:.2f} SEK"
            )
            msg = f"Dagladdning är billigare:\n{day_line}\n{night_line}"
        else:
            msg = f"Dagladdning är billigast:\n{day_line}"

        if not self.enabled or not self.notify_target:
            return
        try:
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "notify",
                    self.notify_target.replace("notify.", "", 1),
                    {
                        "title": "EV Laddning – Välj period",
                        "message": msg,
                        "data": {
                            "tag": "ocpp_day_night_choice",
                            "actions": [
                                {
                                    "action": NOTIFY_ACTION_USE_DAY,
                                    "title": f"☀️ Dag ({day_avg_ore:.0f} öre)",
                                },
                                {
                                    "action": NOTIFY_ACTION_USE_NIGHT,
                                    "title": f"🌙 Natt ({night_avg_ore:.0f} öre)" if night_avg_ore else "🌙 Natt",
                                },
                                {
                                    "action": NOTIFY_ACTION_DISMISS,
                                    "title": "🚫 Avsluta",
                                },
                            ]
                        },
                    },
                )
            )
            _LOGGER.info("[Notify] Actionable day/night choice sent")
        except Exception as err:
            _LOGGER.warning("[Notify] Failed to send actionable notification: %s", err)


    def dismiss_day_night_notification(self) -> None:
        """Clear the day/night choice notification from the phone."""
        if not self.enabled or not self.notify_target:
            return
        try:
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "notify",
                    self.notify_target.replace("notify.", "", 1),
                    {
                        "message": "clear_notification",
                        "data": {"tag": "ocpp_day_night_choice"},
                    },
                )
            )
            _LOGGER.info("[Notify] Dismissed day/night choice notification")
        except Exception as err:
            _LOGGER.warning("[Notify] Failed to dismiss notification: %s", err)

    def on_charger_disconnected(self, minutes: int) -> None:
        """Notify when charger WebSocket has been disconnected for a while."""
        self._send(
            "EV Laddning – Frånkopplad",
            f"⚠️ Laddboxen har tappat anslutningen i {minutes} minuter.\n"
            "Kontrollera nätverket eller laddboxen.",
        )


def _fmt_time(dt: datetime) -> str:
    """Format a UTC datetime to local HH:MM."""
    try:
        local = dt.astimezone()
        return local.strftime("%H:%M")
    except Exception:
        return dt.strftime("%H:%M")
