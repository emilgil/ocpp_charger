"""OCPP 1.6 WebSocket client for EV charger communication."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

_LOGGER = logging.getLogger(__name__)

# OCPP 1.6 message types
CALL = 2
CALLRESULT = 3
CALLERROR = 4

# OCPP 1.6 Actions
ACTION_BOOT_NOTIFICATION = "BootNotification"
ACTION_HEARTBEAT = "Heartbeat"
ACTION_METER_VALUES = "MeterValues"
ACTION_STATUS_NOTIFICATION = "StatusNotification"
ACTION_START_TRANSACTION = "StartTransaction"
ACTION_STOP_TRANSACTION = "StopTransaction"
ACTION_AUTHORIZE = "Authorize"
ACTION_CHANGE_CONFIGURATION = "ChangeConfiguration"
ACTION_REMOTE_START = "RemoteStartTransaction"
ACTION_REMOTE_STOP = "RemoteStopTransaction"
ACTION_SET_CHARGING_PROFILE = "SetChargingProfile"
ACTION_CLEAR_CHARGING_PROFILE = "ClearChargingProfile"
ACTION_GET_CONFIGURATION = "GetConfiguration"
ACTION_DATA_TRANSFER = "DataTransfer"
ACTION_TRIGGER_MESSAGE = "TriggerMessage"


@dataclass
class ChargerState:
    """Current state of the charger."""
    status: str = "Unknown"
    connector_status: str = "Unknown"
    cable_connected: bool = False
    charging: bool = False
    transaction_id: Optional[int] = None
    session_id: Optional[str] = None

    # Meter values
    power_w: float = 0.0          # Watts
    current_a: float = 0.0        # Amperes
    voltage_v: float = 230.0      # Volts
    energy_kwh: float = 0.0       # kWh this session
    total_energy_kwh: float = 0.0 # Total meter reading

    # SOC
    soc_percent: Optional[float] = None

    # Session tracking
    session_start: Optional[datetime] = None
    session_energy_start: Optional[float] = None
    accumulated_charging_seconds: int = 0   # active charging time only
    _charging_start: Optional[datetime] = None  # when current charging segment started

    # Cost tracking
    accumulated_cost: float = 0.0  # SEK (current session)
    total_cost: float = 0.0        # SEK (cumulative across all sessions)

    # Limits
    max_current_a: float = 16.0
    active_limit_a: float = 16.0

    # Connection
    connected: bool = False
    last_heartbeat: Optional[datetime] = None

    # Errors
    error_code: str = "NoError"
    vendor_error_code: str = ""


class OCPPClient:
    """OCPP 1.6 client that acts as Central System (server) for the charger."""

    def __init__(
        self,
        host: str,
        port: int,
        charger_id: str,
        state_callback: Callable[[ChargerState], None],
        hass=None,
    ):
        self.host = host
        self.port = port
        self.charger_id = charger_id
        self._state_callback = state_callback
        self._hass = hass

        self.state = ChargerState()
        self._ws: Optional[websockets.WebSocketServerProtocol] = None
        self._server: Optional[websockets.WebSocketServer] = None
        self._pending_calls: dict[str, asyncio.Future] = {}
        self._running = False
        self._reconnect_interval = 10
        self._call_lock = asyncio.Lock()
        self._pending_limit_a: float | None = None
        self._current_l1: float | None = None
        self._current_l2: float | None = None
        self._current_l3: float | None = None
        self._default_limit_a: float = 6.0  # fallback if no limit set yet; coordinator updates this

    # ------------------------------------------------------------------ #
    #  Server lifecycle                                                     #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Start the OCPP WebSocket server and listen for charger connection."""
        self._running = True
        _LOGGER.info("[OCPP] Starting WebSocket server on %s:%s", self.host, self.port)
        try:
            self._server = await websockets.serve(
                self._handle_charger,
                "0.0.0.0",
                self.port,
                subprotocols=["ocpp1.6"],
            )
            _LOGGER.info("OCPP server started, waiting for charger %s", self.charger_id)
        except OSError as err:
            _LOGGER.error("Failed to start OCPP server: %s", err)
            raise

    async def stop(self) -> None:
        """Stop the OCPP server."""
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        _LOGGER.info("OCPP server stopped")

    async def _handle_charger(self, websocket) -> None:
        """Handle an incoming charger connection.

        Compatible with both websockets <12 (websocket, path) and >=12 (websocket only).
        """
        # websockets >=12 exposes the path via websocket.request.path
        try:
            path = websocket.request.path
        except AttributeError:
            # Older API: path was passed as second argument – shouldn't reach here
            path = "/"

        charger_id = path.strip("/").split("/")[-1]
        _LOGGER.info("[OCPP] Charger connected: id=%s path=%s remote=%s", charger_id, path, websocket.remote_address)

        if charger_id != self.charger_id:
            _LOGGER.warning("[OCPP] Rejecting unknown charger ID: %s (expected: %s)", charger_id, self.charger_id)
            await websocket.close(1008, "Unknown charger ID")
            return

        self._ws = websocket
        self.state.connected = True
        self._notify()

        try:
            async for raw_message in websocket:
                await self._handle_message(raw_message)
        except (ConnectionClosedOK, ConnectionClosedError):
            pass
        except Exception as err:
            _LOGGER.error("Error handling charger message: %s", err)
        finally:
            self.state.connected = False
            if self.state.charging and self.state._charging_start:
                delta = datetime.now(timezone.utc) - self.state._charging_start
                self.state.accumulated_charging_seconds += int(delta.total_seconds())
                self.state._charging_start = None
            self.state.charging = False
            self.state.power_w = 0.0
            self.state.current_a = 0.0
            self._ws = None
            _LOGGER.warning("[OCPP] Charger %s disconnected", charger_id)
            self._notify()

    # ------------------------------------------------------------------ #
    #  Message handling                                                     #
    # ------------------------------------------------------------------ #

    async def _handle_message(self, raw: str) -> None:
        """Parse and dispatch an OCPP message."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            _LOGGER.error("Invalid JSON from charger: %s", raw)
            return

        msg_type = msg[0]

        if msg_type == CALL:
            _, unique_id, action, payload = msg
            await self._handle_call(unique_id, action, payload)
        elif msg_type == CALLRESULT:
            _, unique_id, payload = msg
            self._handle_call_result(unique_id, payload)
        elif msg_type == CALLERROR:
            _, unique_id, error_code, error_desc, error_details = msg
            self._handle_call_error(unique_id, error_code, error_desc)

    async def _handle_call(
        self, unique_id: str, action: str, payload: dict
    ) -> None:
        """Handle a CALL from the charger and send a CALLRESULT."""
        _LOGGER.debug("[OCPP] ← CALL  action=%s payload=%s", action, payload)
        response = {}

        if action == ACTION_BOOT_NOTIFICATION:
            response = {
                "status": "Accepted",
                "currentTime": datetime.now(timezone.utc).isoformat(),
                "interval": 30,
            }
            _LOGGER.info(
                "Charger booted: %s %s",
                payload.get("chargePointVendor"),
                payload.get("chargePointModel"),
            )

        elif action == ACTION_HEARTBEAT:
            response = {"currentTime": datetime.now(timezone.utc).isoformat()}
            self.state.last_heartbeat = datetime.now(timezone.utc)

        elif action == ACTION_STATUS_NOTIFICATION:
            connector_id = payload.get("connectorId", 1)
            status = payload.get("status", "Unknown")
            error_code = payload.get("errorCode", "NoError")
            if connector_id == 1:
                self.state.connector_status = status
                self.state.error_code = error_code
                self.state.cable_connected = status in {
                    "Preparing", "Charging", "SuspendedEV",
                    "SuspendedEVSE", "Finishing",
                }
                self.state.charging = status == "Charging"
                if not self.state.charging:
                    self.state.power_w = 0.0
                    self.state.current_a = 0.0
                # Assign a connect-session ID at Preparing so the coordinator
                # can deduplicate cable-connect notifications correctly.
                # (StartTransaction sets a proper session_id later.)
                if status == "Preparing" and self.state.session_id is None:
                    self.state.session_id = "connect-" + str(uuid.uuid4())[:8].upper()
            elif connector_id == 0:
                _LOGGER.debug("[OCPP] StatusNotification connectorId=0 (charger-level), ignoreras")
                response = {}
                await self._send_call_result(unique_id, response)
                return
            self._notify()
            response = {}

        elif action == ACTION_AUTHORIZE:
            response = {"idTagInfo": {"status": "Accepted"}}

        elif action == ACTION_START_TRANSACTION:
            self.state.transaction_id = int(time.time())
            self.state.session_id = str(uuid.uuid4())[:8].upper()
            self.state.session_start = datetime.now(timezone.utc)
            self.state.energy_kwh = 0.0
            self.state.accumulated_cost = 0.0
            self.state.session_energy_start = payload.get("meterStart", 0) / 1000.0
            self.state.charging = True
            self.state.accumulated_charging_seconds = 0
            self.state._charging_start = datetime.now(timezone.utc)
            self._notify()
            response = {
                "transactionId": self.state.transaction_id,
                "idTagInfo": {"status": "Accepted"},
            }
            _LOGGER.info("[OCPP] Transaction started: id=%s session=%s meter_start=%.3f kWh",
                    self.state.transaction_id, self.state.session_id, self.state.session_energy_start)
            # Apply current limit immediately on transaction start so the charger
            # honours our limit from the very first second, even on Garo auto-start.
            limit = self._pending_limit_a if self._pending_limit_a is not None else self._default_limit_a
            _LOGGER.info("[OCPP] Applying current limit on transaction start: %.0f A", limit)
            if self._hass:
                self._hass.async_create_task(self.set_charging_limit(limit))
            else:
                asyncio.ensure_future(self.set_charging_limit(limit))

        elif action == ACTION_STOP_TRANSACTION:
            meter_stop = payload.get("meterStop", 0) / 1000.0
            tx_energy_kwh = meter_stop - (self.state.session_energy_start or meter_stop)
            self.state.energy_kwh = tx_energy_kwh
            self.state.transaction_id = None
            if self.state._charging_start:
                delta = datetime.now(timezone.utc) - self.state._charging_start
                self.state.accumulated_charging_seconds += int(delta.total_seconds())
                self.state._charging_start = None
            self.state.charging = False
            self.state.power_w = 0.0
            self.state.current_a = 0.0
            self.state.total_cost += self.state.accumulated_cost
            # Bug 6: Accumulate energy/cost into cable session (coordinator fields)
            tx_cost_sek = self.state.accumulated_cost
            if self._hass:
                from . import OCPPCoordinator
                from .const import DOMAIN
                for coord in self._hass.data.get(DOMAIN, {}).values():
                    if isinstance(coord, OCPPCoordinator) and coord.ocpp is self:
                        coord._cable_session_energy_kwh += tx_energy_kwh
                        coord._cable_session_cost_sek += tx_cost_sek
                        _LOGGER.info(
                            "[Session] OCPP tx avslutad: +%.2f kWh (totalt %.2f kWh denna kabelsession)",
                            tx_energy_kwh, coord._cable_session_energy_kwh,
                        )
                        break
            self._notify()
            response = {"idTagInfo": {"status": "Accepted"}}
            _LOGGER.info("[OCPP] Transaction stopped: energy=%.3f kWh session_cost=%.2f SEK total_cost=%.2f SEK",
                         self.state.energy_kwh, self.state.accumulated_cost, self.state.total_cost)

        elif action == ACTION_METER_VALUES:
            await self._parse_meter_values(payload)
            response = {}

        elif action == ACTION_DATA_TRANSFER:
            response = {"status": "Accepted"}

        else:
            _LOGGER.warning("Unhandled OCPP action: %s", action)
            response = {}

        await self._send_call_result(unique_id, response)

    async def _parse_meter_values(self, payload: dict) -> None:
        """Extract meter values from MeterValues payload."""
        # Sync transaction_id from MeterValues payload if we missed StartTransaction
        tx_id = payload.get("transactionId")
        if tx_id and self.state.transaction_id is None:
            self.state.transaction_id = int(tx_id)
            self.state.cable_connected = True
            _LOGGER.info("[OCPP] cable_connected=True inferred from MeterValues txId=%s", tx_id)
        for mv in payload.get("meterValue", []):
            for sv in mv.get("sampledValue", []):
                measurand = sv.get("measurand", "Energy.Active.Import.Register")
                value = float(sv.get("value", 0))
                unit = sv.get("unit", "")
                phase = sv.get("phase", "")

                if measurand == "Power.Active.Import" and not phase:
                    self.state.power_w = value if unit != "kW" else value * 1000
                elif measurand == "Current.Import" and not phase:
                    self.state.current_a = value
                elif measurand == "Current.Import" and phase == "L1":
                    # Garo only sends per-phase current; will be aggregated below.
                    self._current_l1 = value
                elif measurand == "Current.Import" and phase == "L2":
                    self._current_l2 = value
                elif measurand == "Current.Import" and phase == "L3":
                    self._current_l3 = value
                elif measurand == "Voltage" and not phase:
                    self.state.voltage_v = value
                elif measurand == "Energy.Active.Import.Register":
                    kwh = value / 1000.0 if unit == "Wh" else value
                    self.state.total_energy_kwh = kwh
                    if self.state.session_energy_start is not None:
                        session_kwh = kwh - self.state.session_energy_start
                        if session_kwh >= 0:
                            self.state.energy_kwh = session_kwh
                elif measurand == "SoC":
                    # Sanity check: only accept values 0–100 and reject
                    # implausible jumps > 20 pp from the last known value.
                    if 0.0 <= value <= 100.0:
                        prev = self.state.soc_percent
                        if prev is None or abs(value - prev) <= 20:
                            self.state.soc_percent = value
                            # Bug 9: mark coordinator so _update_soc_from_ha() keeps this value
                            if self._hass:
                                from . import OCPPCoordinator
                                from .const import DOMAIN
                                for coord in self._hass.data.get(DOMAIN, {}).values():
                                    if isinstance(coord, OCPPCoordinator) and coord.ocpp is self:
                                        coord._soc_source = "ocpp"
                        else:
                            _LOGGER.warning(
                                "[OCPP] Ignoring implausible SoC from charger: "
                                "%.1f%% → %.1f%% (delta >20 pp)", prev, value
                            )

        # Aggregate per-phase current into current_a (Garo only sends L1/L2/L3).
        l1 = getattr(self, "_current_l1", None)
        l2 = getattr(self, "_current_l2", None)
        l3 = getattr(self, "_current_l3", None)
        phase_values = [v for v in (l1, l2, l3) if v is not None]
        if phase_values:
            self.state.current_a = round(sum(phase_values) / len(phase_values), 2)

        # Infer charging/cable state from meter data if StatusNotification was missed.
        # If we have active power or a transaction ID, we must be charging.
        connector_id = payload.get("connectorId", 0)
        if connector_id == 1:
            if self.state.power_w > 100 or self.state.transaction_id is not None:
                if not self.state.charging:
                    _LOGGER.debug("[OCPP] Inferred charging=True from MeterValues (power=%.0fW txId=%s)",
                        self.state.power_w, self.state.transaction_id)
                    # If StartTransaction was missed (e.g. HA restarted mid-session),
                    # session_energy_start is 0 but total meter is high.
                    # Recalibrate: set start to current meter so session energy = 0.
                    if self.state.session_energy_start is None and self.state.total_energy_kwh > 1.0:
                        self.state.session_energy_start = self.state.total_energy_kwh
                        self.state.energy_kwh = 0.0
                        if self.state.session_id is None:
                            self.state.session_id = "recovered-" + str(uuid.uuid4())[:8].upper()
                        if self.state.session_start is None:
                            self.state.session_start = datetime.now(timezone.utc)
                        _LOGGER.warning("[OCPP] StartTransaction missed – recalibrating session start to %.3f kWh session=%s",
                            self.state.session_energy_start, self.state.session_id)
                        # Enforce current limit since we missed StartTransaction
                        limit = self._pending_limit_a if self._pending_limit_a is not None else self._default_limit_a
                        if self._hass:
                            self._hass.async_create_task(self.set_charging_limit(limit))
                        else:
                            asyncio.ensure_future(self.set_charging_limit(limit))
                if not self.state.charging:
                    self.state._charging_start = datetime.now(timezone.utc)
                self.state.charging = True
                self.state.cable_connected = True
                if self.state.connector_status == "Unknown":
                    self.state.connector_status = "Charging"

        self._notify()

    def _handle_call_result(self, unique_id: str, payload: dict) -> None:
        """Resolve a pending call future."""
        future = self._pending_calls.pop(unique_id, None)
        if future and not future.done():
            future.set_result(payload)

    def _handle_call_error(
        self, unique_id: str, error_code: str, error_desc: str
    ) -> None:
        """Reject a pending call future."""
        future = self._pending_calls.pop(unique_id, None)
        if future and not future.done():
            future.set_exception(
                Exception(f"OCPP error {error_code}: {error_desc}")
            )

    # ------------------------------------------------------------------ #
    #  Outgoing commands                                                   #
    # ------------------------------------------------------------------ #

    async def _send_call(
        self, action: str, payload: dict, timeout: float = 10.0
    ) -> dict:
        """Send a CALL and wait for the CALLRESULT."""
        if not self._ws:
            raise ConnectionError("No charger connected")

        unique_id = str(uuid.uuid4())
        msg = json.dumps([CALL, unique_id, action, payload])
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending_calls[unique_id] = future

        async with self._call_lock:
            await self._ws.send(msg)
            _LOGGER.debug("[OCPP] → CALL  action=%s payload=%s", action, payload)

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending_calls.pop(unique_id, None)
            raise TimeoutError(f"OCPP call {action} timed out")

    async def _send_call_result(self, unique_id: str, payload: dict) -> None:
        """Send a CALLRESULT back to charger."""
        if not self._ws:
            return
        msg = json.dumps([CALLRESULT, unique_id, payload])
        await self._ws.send(msg)

    async def remote_start_transaction(
        self, id_tag: str = "HA_USER", connector_id: int = 1
    ) -> bool:
        """Send RemoteStartTransaction to the charger."""
        try:
            result = await self._send_call(
                ACTION_REMOTE_START,
                {"connectorId": connector_id, "idTag": id_tag},
            )
            status = result.get("status", "Rejected")
            _LOGGER.info("[OCPP] RemoteStartTransaction: result=%s", status)
            return status == "Accepted"
        except Exception as err:
            _LOGGER.error("RemoteStartTransaction failed: %s", err)
            return False

    async def remote_stop_transaction(self) -> bool:
        """Send RemoteStopTransaction to the charger."""
        if not self.state.transaction_id:
            _LOGGER.warning("No active transaction to stop")
            return False
        try:
            result = await self._send_call(
                ACTION_REMOTE_STOP,
                {"transactionId": self.state.transaction_id},
            )
            status = result.get("status", "Rejected")
            _LOGGER.info("[OCPP] RemoteStopTransaction: txId=%s result=%s", self.state.transaction_id, status)
            return status == "Accepted"
        except Exception as err:
            _LOGGER.error("RemoteStopTransaction failed: %s", err)
            return False

    async def _apply_charge_point_max_profile(self, max_current_a: float) -> bool:
        """Send a ChargePointMaxProfile (connectorId=0, stackLevel=0).

        Garo rejects TxProfile during an active transaction but DOES honour
        ChargePointMaxProfile, which acts as a hard cap for the whole charge point.
        This is the only profile type that reliably limits current on Garo laddbox.
        """
        payload = {
            "connectorId": 0,
            "csChargingProfiles": {
                "chargingProfileId": 1,
                "stackLevel": 0,
                "chargingProfilePurpose": "ChargePointMaxProfile",
                "chargingProfileKind": "Absolute",
                "chargingSchedule": {
                    "chargingRateUnit": "A",
                    "chargingSchedulePeriod": [
                        {"startPeriod": 0, "limit": max_current_a}
                    ],
                },
            },
        }
        try:
            result = await self._send_call(ACTION_SET_CHARGING_PROFILE, payload)
            status = result.get("status", "Rejected")
            _LOGGER.info("[OCPP] ChargePointMaxProfile applied: limit=%.0f A result=%s", max_current_a, status)
            return status == "Accepted"
        except Exception as err:
            _LOGGER.error("[OCPP] ChargePointMaxProfile failed: %s", err)
            return False

    async def set_charging_limit(
        self, max_current_a: float, _retries: int = 2
    ) -> bool:
        """Set charging current limit with retry.

        Strategy (in order of preference):
        1. GaroOwnerMaxCurrent via ChangeConfiguration – Accepted by Garo laddbox,
           works both before and during a transaction.
        2. ChargePointMaxProfile fallback – kept for non-Garo chargers.

        Retries up to *_retries* times with a 2 s delay on transient failures.
        """
        for attempt in range(1, _retries + 2):  # attempt 1 .. _retries+1
            # Try GaroOwnerMaxCurrent first
            try:
                result = await self._send_call(
                    ACTION_CHANGE_CONFIGURATION,
                    {"key": "GaroOwnerMaxCurrent", "value": str(int(max_current_a))},
                )
                status = result.get("status", "Rejected")
                _LOGGER.info(
                    "[OCPP] GaroOwnerMaxCurrent=%.0f A result=%s (attempt %d)",
                    max_current_a, status, attempt,
                )
                if status == "Accepted":
                    self.state.active_limit_a = max_current_a
                    self._pending_limit_a = max_current_a
                    return True
                if status == "RebootRequired":
                    self.state.active_limit_a = max_current_a
                    self._pending_limit_a = max_current_a
                    _LOGGER.warning("[OCPP] Charger requires reboot for GaroOwnerMaxCurrent change")
                    return True
                # "Rejected" or "NotSupported" → fall through to profile fallback
                break
            except (TimeoutError, ConnectionError) as err:
                _LOGGER.warning(
                    "[OCPP] GaroOwnerMaxCurrent attempt %d failed: %s", attempt, err
                )
                if attempt <= _retries:
                    await asyncio.sleep(2)
                    continue
                break
            except Exception as err:
                _LOGGER.debug("[OCPP] GaroOwnerMaxCurrent failed, falling back: %s", err)
                break

        # Fallback: ChargePointMaxProfile
        ok = await self._apply_charge_point_max_profile(max_current_a)
        if ok:
            self.state.active_limit_a = max_current_a
            self._pending_limit_a = max_current_a
        return ok

    async def trigger_status_notification(self) -> None:
        """Ask charger to resend StatusNotification so cable state is correct after HA restart."""
        if self._ws is None:
            _LOGGER.warning("[OCPP] TriggerMessage: ingen ansluten laddare")
            return
        try:
            _LOGGER.info("[OCPP] Skickar TriggerMessage StatusNotification")
            response = await self._send_call(
                ACTION_TRIGGER_MESSAGE,
                {"requestedMessage": "StatusNotification", "connectorId": 1},
            )
            _LOGGER.info("[OCPP] TriggerMessage svar: %s", response)
        except Exception as err:
            _LOGGER.error("[OCPP] TriggerMessage misslyckades: %s", err)

    async def trigger_meter_values(self) -> None:
        """Ask charger to send meter values now."""
        try:
            await self._send_call(
                ACTION_TRIGGER_MESSAGE,
                {"requestedMessage": "MeterValues", "connectorId": 1},
            )
        except Exception as err:
            _LOGGER.debug("TriggerMessage failed (non-critical): %s", err)

    async def change_configuration(self, key: str, value: str) -> dict:
        """Send ChangeConfiguration to the charger and return the result."""
        try:
            result = await self._send_call(
                ACTION_CHANGE_CONFIGURATION,
                {"key": key, "value": value},
            )
            status = result.get("status", "Unknown")
            _LOGGER.info("[OCPP] ChangeConfiguration key=%r value=%r result=%s", key, value, status)
            return {"key": key, "value": value, "status": status}
        except Exception as err:
            _LOGGER.error("[OCPP] ChangeConfiguration failed: %s", err)
            return {"key": key, "value": value, "status": "Error", "error": str(err)}

    async def get_configuration(self, key: str | None = None) -> dict:
        """Send GetConfiguration to the charger and return the result."""
        payload = {}
        if key:
            payload["key"] = [key]
        try:
            result = await self._send_call(ACTION_GET_CONFIGURATION, payload)
            config_key = result.get("configurationKey", [])
            unknown = result.get("unknownKey", [])
            _LOGGER.info("[OCPP] GetConfiguration key=%r → %d entries, unknown=%s", key, len(config_key), unknown)
            return {"configuration_key": config_key, "unknown_key": unknown}
        except Exception as err:
            _LOGGER.error("[OCPP] GetConfiguration failed: %s", err)
            return {"configuration_key": [], "unknown_key": [], "error": str(err)}

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _notify(self) -> None:
        """Call the state update callback."""
        try:
            self._state_callback(self.state)
        except Exception as err:
            _LOGGER.error("State callback error: %s", err)

