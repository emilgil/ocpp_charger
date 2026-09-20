"""Loggkonfiguration för ocpp_charger (Feature 9).

Ren stdlib – ingen HA-import, så modulen testas fristående (tests/test_logging_setup.py).

    custom_components.ocpp_charger  (level=DEBUG, propagate=False)
      ├─ HaForwardHandler ─────────────► HA:s root-logger (bara WARNING+, allt vid verbose_ha)
      └─ QueueHandler ─► SimpleQueue ─► QueueListener (egen tråd)
                                          ├─ TimedRotatingFileHandler  (DEBUG, midnatt, 14 dygn)
                                          └─ SysLogHandler UDP         (vald nivå, bara om värd satt)

Fil-I/O och nätverk körs i listener-tråden, aldrig i HA:s event-loop. apply_logging() och
remove_logging() blockerar (getaddrinfo, filöppning, listener.stop() joinar tråden) och ska
anropas via hass.async_add_executor_job().
"""
from __future__ import annotations

import logging
import logging.handlers
import queue
import socket
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

try:  # in-package import
    from .const import (
        CONF_LOG_VERBOSE_HA,
        CONF_SYSLOG_HOST,
        CONF_SYSLOG_LEVEL,
        CONF_SYSLOG_PORT,
        DEFAULT_SYSLOG_LEVEL,
        DEFAULT_SYSLOG_PORT,
        LOG_BACKUP_DAYS,
    )
except ImportError:  # standalone (tests put the module dir on sys.path)
    from const import (  # type: ignore[no-redef]
        CONF_LOG_VERBOSE_HA,
        CONF_SYSLOG_HOST,
        CONF_SYSLOG_LEVEL,
        CONF_SYSLOG_PORT,
        DEFAULT_SYSLOG_LEVEL,
        DEFAULT_SYSLOG_PORT,
        LOG_BACKUP_DAYS,
    )

COMPONENT_LOGGER_NAME = "custom_components.ocpp_charger"
SYSLOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
SYSLOG_IDENT = "ocpp_charger: "
FILE_FORMAT = "%(asctime)s %(levelname)s %(name)s – %(message)s"
SYSLOG_FORMAT = "%(name)s – %(message)s"

# Hårdkodat namn i stället för __name__: loggern ligger under komponentloggern även när modulen
# importeras fristående (tester), så modulens egna varningar tar samma väg som i HA
# (HA-vidarebefordran + fil).
_LOGGER = logging.getLogger(COMPONENT_LOGGER_NAME + ".logging_setup")


# ── Konfiguration ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LoggingConfig:
    verbose_ha: bool = False
    syslog_host: str = ""
    syslog_port: int = DEFAULT_SYSLOG_PORT
    syslog_level: str = DEFAULT_SYSLOG_LEVEL


def config_from_entry_data(data: Mapping[str, Any]) -> LoggingConfig:
    """Läs loggvalen ur entry.data. Saknade/ogiltiga värden → standard. Kastar aldrig."""
    host = data.get(CONF_SYSLOG_HOST, "")
    host = host.strip() if isinstance(host, str) else ""

    port = DEFAULT_SYSLOG_PORT
    raw_port = data.get(CONF_SYSLOG_PORT, DEFAULT_SYSLOG_PORT)
    if not isinstance(raw_port, bool):  # True vore annars port 1
        try:
            candidate = int(raw_port)
        except (TypeError, ValueError, OverflowError):
            candidate = 0
        if 1 <= candidate <= 65535:
            port = candidate

    level = data.get(CONF_SYSLOG_LEVEL, DEFAULT_SYSLOG_LEVEL)
    if level not in SYSLOG_LEVELS:
        level = DEFAULT_SYSLOG_LEVEL

    return LoggingConfig(
        # `is True`: bara en riktig bool slår på det (strängen "false" är sann i Python).
        verbose_ha=data.get(CONF_LOG_VERBOSE_HA, False) is True,
        syslog_host=host,
        syslog_port=port,
        syslog_level=level,
    )


# ── Handlers ──────────────────────────────────────────────────────────────────


class HaForwardHandler(logging.Handler):
    """Skickar komponentens poster vidare till HA:s root-logger.

    Kringgår med avsikt HA:s per-logger-nivåer (Logger.handle() nivåkollar inte): filtret ligger
    här – bara WARNING och ERROR, eller allt om `verbose`. `verbose` är en vanlig attribut som
    kan ändras utan att handlern byts.
    """

    def __init__(self, verbose: bool = False) -> None:
        super().__init__(level=logging.DEBUG)
        self.verbose = verbose

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if not self.verbose and record.levelno < logging.WARNING:
                return
            logging.getLogger().handle(record)
        except Exception:  # noqa: BLE001 – loggningen får aldrig fälla anroparen
            self.handleError(record)


class RateLimitedSysLogHandler(logging.handlers.SysLogHandler):
    """SysLogHandler som inte spammar stderr vid upprepade sändningsfel.

    Standardens handleError() skriver "Logging error" till stderr för varje misslyckad rad (upp
    till 20 000/dygn). Här loggas första felet som EN warning och därefter tystnad tills en
    sändning lyckas igen (då loggas en info). Allt sker i QueueListener-tråden under handlerns lås.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._failing = False
        self._emit_failed = False

    def emit(self, record: logging.LogRecord) -> None:
        self._emit_failed = False
        super().emit(record)  # anropar handleError() vid fel
        if self._failing and not self._emit_failed:
            self._failing = False
            _LOGGER.info("[Logging] Syslog UDP fungerar igen (%s:%s)", *self.address)

    def handleError(self, record: logging.LogRecord) -> None:
        self._emit_failed = True
        if not self._failing:
            self._failing = True
            _LOGGER.warning(
                "[Logging] Syslog UDP-sändning till %s:%s misslyckades: %s – tystar tills den lyckas igen "
                "(återställningen loggas som INFO i debugfilen)",
                *self.address,
                sys.exc_info()[1],
            )


def _resolve_syslog_address(host: str, port: int) -> tuple[str, int]:
    """Lös värdnamnet EN gång (blockerar) så att varje sendto() går mot en IP utan DNS-uppslagning."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
    if not infos:
        raise OSError(f"getaddrinfo gav inga adresser för {host}")
    return infos[0][4][0], port


def _build_syslog_handler(config: LoggingConfig) -> logging.Handler:
    handler = RateLimitedSysLogHandler(
        address=_resolve_syslog_address(config.syslog_host, config.syslog_port),
        facility=logging.handlers.SysLogHandler.LOG_LOCAL0,
        socktype=socket.SOCK_DGRAM,
    )
    handler.ident = SYSLOG_IDENT
    # Ingen avslutande NUL-byte: Pythons standard lägger en i varje datagram och nyare
    # syslog-mottagare släpper igenom den som en del av meddelandet.
    handler.append_nul = False
    handler.setLevel(getattr(logging, config.syslog_level))
    handler.setFormatter(logging.Formatter(SYSLOG_FORMAT))
    return handler


def _build_file_handler(log_path: str) -> logging.Handler:
    handler = logging.handlers.TimedRotatingFileHandler(
        log_path,
        when="midnight",
        backupCount=LOG_BACKUP_DAYS,
        encoding="utf-8",
        delay=True,
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(FILE_FORMAT))
    return handler


def _set_level(logger: logging.Logger, level: int) -> None:
    """Logger.setLevel som går förbi HA:s logger-integration.

    HA byter loggerklass (HassLogger) och gör setLevel() till en no-op för loggers som har en override
    (logger: i configuration.yaml, logger.set_level, UI-knappen "Aktivera felsökning"). Då fick
    debugfilen tyst inte DEBUG/INFO och nivån återställdes inte heller. HA:s egna hjälpare använder
    samma orig_setLevel.
    """
    getattr(logger, "orig_setLevel", logger.setLevel)(level)


# ── Tillstånd, apply / remove ─────────────────────────────────────────────────


@dataclass
class _State:
    listener: logging.handlers.QueueListener
    handlers: list[logging.Handler]  # alla managed handlers (logger + listener) – stängs vid remove
    prev_level: int
    prev_propagate: bool


_LOCK = threading.RLock()
_state: _State | None = None


def apply_logging(config: LoggingConfig, log_path: str) -> None:
    """Bygg och aktivera loggkedjan. Idempotent. Blockerar – anropa via executor."""
    global _state
    with _LOCK:
        remove_logging()

        sinks: list[logging.Handler] = [_build_file_handler(log_path)]
        syslog_error: Exception | None = None
        if config.syslog_host:
            try:
                sinks.append(_build_syslog_handler(config))
            except Exception as err:  # noqa: BLE001 – ett syslog-fel får aldrig fälla setup
                syslog_error = err  # t.ex. gaierror (OSError) eller UnicodeError ("a..b")

        # respect_handler_level=True: annars skickas allt till alla sinks och syslog-nivån ignoreras.
        log_queue: queue.SimpleQueue = queue.SimpleQueue()
        listener = logging.handlers.QueueListener(
            log_queue, *sinks, respect_handler_level=True
        )
        listener.start()

        forward = HaForwardHandler(verbose=config.verbose_ha)
        to_queue = logging.handlers.QueueHandler(log_queue)
        handlers: list[logging.Handler] = [forward, to_queue, *sinks]
        for handler in handlers:
            handler._ocpp_managed = True  # type: ignore[attr-defined]

        logger = logging.getLogger(COMPONENT_LOGGER_NAME)
        _state = _State(listener, handlers, logger.level, logger.propagate)
        logger.addHandler(forward)
        logger.addHandler(to_queue)
        _set_level(logger, logging.DEBUG)
        logger.propagate = False

    # Efter att kedjan är på plats så att varningen når både HA-loggen och filen.
    if syslog_error is not None:
        _LOGGER.warning(
            "[Logging] Syslog UDP är inte aktiv – %s:%s kunde inte användas: %s",
            config.syslog_host,
            config.syslog_port,
            syslog_error,
        )


def remove_logging() -> None:
    """Ta bort loggkedjan och återställ loggern. Säker att anropa flera gånger. Blockerar."""
    global _state
    with _LOCK:
        logger = logging.getLogger(COMPONENT_LOGGER_NAME)
        for handler in list(logger.handlers):
            if getattr(handler, "_ocpp_managed", False):
                logger.removeHandler(handler)

        state, _state = _state, None
        if state is None:
            return
        try:
            state.listener.stop()  # tömmer kön innan filen stängs
        finally:
            for handler in state.handlers:
                handler.close()
            _set_level(logger, state.prev_level)
            logger.propagate = state.prev_propagate
