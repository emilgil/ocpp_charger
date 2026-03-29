"""Smart charging logic: optimize charging based on electricity prices."""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

_LOGGER = logging.getLogger(__name__)


class SmartChargeController:
    """
    Decides whether to charge and at what rate, based on electricity prices.

    Strategy:
    - Immediate mode: Always charge at max allowed current.
    - Smart mode: Charge when price is below a rolling threshold.
      The threshold is calculated from the available price history:
      we charge during the cheapest X% of hours (configurable).
    - Scheduled mode: Charge only during a user-defined time window.
    """

    def __init__(
        self,
        threshold_percentile: float = 0.5,
        local_tz: Any = None,
    ):
        self.threshold_percentile = threshold_percentile
        self._price_history: deque[float] = deque(maxlen=48)
        self.local_tz = local_tz

    def update_price(self, price: float) -> None:
        """Add a new price observation."""
        self._price_history.append(price)

    def should_charge(
        self,
        mode: str,
        current_price: Optional[float],
        target_soc: Optional[float] = None,
        current_soc: Optional[float] = None,
        target_kwh: Optional[float] = None,
        session_kwh: float = 0.0,
        scheduled_start: Optional[str] = None,
        scheduled_end: Optional[str] = None,
    ) -> tuple[bool, str]:
        """
        Return (should_charge, reason).
        """
        _LOGGER.debug(
            "[SmartCharge] Evaluating: mode=%s price=%s soc=%s/%s kwh=%.2f/%.2f",
            mode, current_price, current_soc, target_soc, session_kwh, target_kwh or 0,
        )
        # Check SOC target
        if target_soc is not None and current_soc is not None:
            if current_soc >= target_soc:
                return False, f"SOC target {target_soc:.0f}% reached ({current_soc:.0f}%)"

        # Check kWh target
        if target_kwh is not None and target_kwh > 0:
            if session_kwh >= target_kwh:
                return False, f"kWh target {target_kwh:.1f} kWh reached ({session_kwh:.1f} kWh)"

        if mode == "Immediate":
            return True, "Immediate läge aktivt"

        elif mode == "Smart (price-optimised)":
            if current_price is None:
                return True, "No price data, charging anyway"

            threshold = self._calculate_threshold()
            if threshold is None:
                return True, "Insufficient price history, charging anyway"

            # Use a 3% deadband: only charge if price is clearly below threshold,
            # avoiding edge cases where current price == threshold exactly.
            charge_ceiling = threshold * 0.97
            if current_price <= charge_ceiling:
                return (
                    True,
                    f"Low price {current_price:.2f} ≤ ceiling {charge_ceiling:.2f} öre/kWh (threshold {threshold:.2f})",
                )
            else:
                return (
                    False,
                    f"High price {current_price:.2f} > ceiling {charge_ceiling:.2f} öre/kWh (threshold {threshold:.2f})",
                )

        elif mode == "Scheduled":
            if scheduled_start and scheduled_end:
                tz = self.local_tz or timezone.utc
                now = datetime.now(tz).strftime("%H:%M")
                if scheduled_start <= scheduled_end:
                    in_window = scheduled_start <= now <= scheduled_end
                else:
                    # Midnight crossing (e.g. 22:00–06:00)
                    in_window = now >= scheduled_start or now <= scheduled_end
                if in_window:
                    return True, f"Within scheduled time {scheduled_start}–{scheduled_end}"
                else:
                    return False, f"Outside scheduled time {scheduled_start}–{scheduled_end}"
            return True, "Scheduled tid ej angiven, laddar"

        return True, "Unknown mode, charging"

    def _calculate_threshold(self) -> Optional[float]:
        """Return price threshold below which we should charge."""
        if len(self._price_history) < 4:
            return None
        sorted_prices = sorted(self._price_history)
        idx = int(len(sorted_prices) * self.threshold_percentile)
        return sorted_prices[max(0, idx)]

    def recommended_current(
        self,
        max_current: float,
        current_price: Optional[float],
        mode: str,
    ) -> float:
        """
        Return recommended current in Amperes.
        In smart mode, scale down slightly at medium prices, full blast at low.
        """
        if mode != "Smart (price-optimised)" or current_price is None:
            return max_current

        threshold = self._calculate_threshold()
        if threshold is None:
            return max_current

        # Scale: full power below threshold, taper off above
        if current_price <= threshold * 0.7:
            return max_current
        elif current_price <= threshold:
            ratio = 1.0 - 0.3 * (current_price - threshold * 0.7) / (threshold * 0.3)
            return max(6.0, max_current * ratio)
        else:
            return 6.0  # Minimum charge if we must charge

    def estimate_completion_time(
        self,
        session_kwh: float,
        target_kwh: Optional[float],
        target_soc: Optional[float],
        current_soc: Optional[float],
        power_w: float,
        battery_kwh: float = 64.0,
    ) -> Optional[datetime]:
        """Estimate when charging will be complete."""
        if power_w <= 0:
            return None

        remaining_kwh: Optional[float] = None

        if target_kwh is not None and target_kwh > 0:
            remaining_kwh = max(0.0, target_kwh - session_kwh)
        elif target_soc is not None and current_soc is not None:
            remaining_pct = max(0.0, target_soc - current_soc)
            remaining_kwh = (remaining_pct / 100.0) * battery_kwh
        else:
            return None

        if remaining_kwh <= 0:
            return datetime.now(timezone.utc)

        hours_remaining = remaining_kwh / (power_w / 1000.0)
        from datetime import timedelta
        return datetime.now(timezone.utc) + timedelta(hours=hours_remaining)
