"""Fristående tester för logging_setup.py (Feature 9).

Körs utan Home Assistant:
    python3 tests/test_logging_setup.py

logging_setup.py är ren stdlib (och const.py saknar imports), så modulkatalogen läggs på sys.path
och modulerna importeras direkt – paketets __init__.py drar in HA och ska INTE importeras.
Förväntade värden är handskrivna litteraler. Testerna delar processens loggerträd: varje test
städar med remove_logging() i en finally (via applied()) och återställer det den ändrat.
"""
import contextlib
import errno
import io
import logging
import logging.handlers
import os
import socket
import sys
import tempfile
import threading
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "ocpp_charger"
sys.path.insert(0, str(PKG))
import const  # noqa: E402
import logging_setup as ls  # noqa: E402

COMPONENT = "custom_components.ocpp_charger"

# Lever till processens slut, så att loggfilerna går att läsa efter att applied()-blocket lämnats.
_TMP = tempfile.TemporaryDirectory()


# ── Hjälpare ───────────────────────────────────────────────────────────────────


class Capture(logging.Handler):
    """Fångande handler på root-loggern – står i för HA:s root-handlers.

    Sparar bara komponentens egna poster, så orelaterade loggare inte stör räkningen.
    """

    def __init__(self):
        super().__init__(level=logging.NOTSET)
        self.records = []

    def emit(self, record):
        if record.name.startswith(COMPONENT):
            self.records.append(record)

    def levels(self):
        return [r.levelname for r in self.records]


@contextlib.contextmanager
def applied(**overrides):
    """apply_logging mot en tempkatalog med en fångande root-handler. Städar alltid efteråt.

    Ger (capture, loggfilens sökväg). Filen är läsbar först efter att blocket lämnats
    (remove_logging() tömmer kön och stänger den).
    """
    root = logging.getLogger()
    capture = Capture()
    root.addHandler(capture)
    path = str(Path(tempfile.mkdtemp(dir=_TMP.name)) / "ocpp_charger_debug.log")
    try:
        ls.apply_logging(ls.LoggingConfig(**overrides), path)
        yield capture, path
    finally:
        ls.remove_logging()
        root.removeHandler(capture)


def managed(logger):
    return [h for h in logger.handlers if getattr(h, "_ocpp_managed", False)]


def emit_all(name=COMPONENT + ".ocpp_client"):
    log = logging.getLogger(name)
    log.debug("d-msg")
    log.info("i-msg")
    log.warning("w-msg")
    log.error("e-msg")


def sinks():
    """Handlers som QueueListener-tråden skriver till (fil, ev. syslog)."""
    return ls._state.listener.handlers


@contextlib.contextmanager
def udp_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    srv.settimeout(2.0)
    try:
        yield srv, srv.getsockname()[1]
    finally:
        srv.close()


def make_record(msg, level=logging.INFO):
    return logging.LogRecord(COMPONENT + ".x", level, __file__, 1, msg, None, None)


# ── const.py ───────────────────────────────────────────────────────────────────


def test_const_values_match_the_spec():
    assert const.CONF_LOG_VERBOSE_HA == "log_verbose_ha"
    assert const.CONF_SYSLOG_HOST == "syslog_host"
    assert const.CONF_SYSLOG_PORT == "syslog_port"
    assert const.CONF_SYSLOG_LEVEL == "syslog_level"
    assert const.DEFAULT_SYSLOG_PORT == 1514
    assert const.DEFAULT_SYSLOG_LEVEL == "DEBUG"
    assert const.LOG_FILE_NAME == "ocpp_charger_debug.log"
    assert const.LOG_BACKUP_DAYS == 14


# ── 1. config_from_entry_data ─────────────────────────────────────────────────


def test_config_empty_dict_gives_defaults():
    cfg = ls.config_from_entry_data({})
    assert cfg == ls.LoggingConfig(
        verbose_ha=False, syslog_host="", syslog_port=1514, syslog_level="DEBUG"
    )


def test_config_invalid_port_falls_back_to_1514():
    for bad in (0, -1, 65536, 70000, "abc", "", None, True, [], float("nan"), float("inf")):
        cfg = ls.config_from_entry_data({"syslog_port": bad})
        assert cfg.syslog_port == 1514, repr(bad)


def test_config_valid_port_is_kept():
    for good, expected in ((1, 1), (514, 514), (65535, 65535), ("2514", 2514), (2514.0, 2514)):
        assert ls.config_from_entry_data({"syslog_port": good}).syslog_port == expected, good


def test_config_invalid_level_falls_back_to_debug():
    for bad in ("VERBOSE", "info", "", None, 10, ["INFO"]):
        assert ls.config_from_entry_data({"syslog_level": bad}).syslog_level == "DEBUG", repr(bad)
    for good in ("DEBUG", "INFO", "WARNING", "ERROR"):
        assert ls.config_from_entry_data({"syslog_level": good}).syslog_level == good


def test_config_host_is_trimmed_and_non_strings_become_empty():
    assert ls.config_from_entry_data({"syslog_host": "  graylog.lan \t"}).syslog_host == "graylog.lan"
    assert ls.config_from_entry_data({"syslog_host": "   "}).syslog_host == ""
    for bad in (None, 5, ["x"]):
        assert ls.config_from_entry_data({"syslog_host": bad}).syslog_host == "", repr(bad)


def test_config_verbose_needs_a_real_true():
    assert ls.config_from_entry_data({"log_verbose_ha": True}).verbose_ha is True
    for other in (False, None, "true", "false", 1, "on"):
        assert ls.config_from_entry_data({"log_verbose_ha": other}).verbose_ha is False, repr(other)


# ── 2. propagate / nivå ───────────────────────────────────────────────────────


def test_apply_sets_propagate_false_and_debug_and_remove_restores():
    logger = logging.getLogger(COMPONENT)
    logger.setLevel(logging.WARNING)
    logger.propagate = True
    try:
        with applied():
            assert logger.propagate is False
            assert logger.level == logging.DEBUG
            assert len(managed(logger)) == 2
        assert logger.propagate is True
        assert logger.level == logging.WARNING
        assert managed(logger) == []
    finally:
        logger.setLevel(logging.NOTSET)
        logger.propagate = True


class FakeHassLogger(logging.Logger):
    """Modell av HA:s HassLogger (components/logger/__init__.py): setLevel() är en no-op för loggers med en
    override (logger: i configuration.yaml, logger.set_level, UI-knappen "Aktivera felsökning");
    orig_setLevel() går förbi den."""

    overrides = set()

    def setLevel(self, level):
        if self.name in self.overrides:
            return
        super().setLevel(level)

    def orig_setLevel(self, level):
        super().setLevel(level)


def test_debug_wins_over_an_ha_logger_override_and_the_level_is_restored():
    logger = logging.getLogger(COMPONENT)
    old_class, old_level, old_propagate = logger.__class__, logger.level, logger.propagate
    logger.__class__ = FakeHassLogger
    FakeHassLogger.overrides = {COMPONENT}
    logger.orig_setLevel(logging.WARNING)  # nivån som HA:s override satte
    try:
        with applied():
            assert logger.level == logging.DEBUG  # apply_logging kom förbi overriden
            assert logger.propagate is False
        assert logger.level == logging.WARNING  # remove_logging återställde nivån, också förbi overriden
        assert logger.propagate is True
    finally:
        FakeHassLogger.overrides = set()
        logger.__class__ = old_class
        logger.setLevel(old_level)
        logger.propagate = old_propagate


def test_remove_leaves_foreign_handlers_alone():
    logger = logging.getLogger(COMPONENT)
    foreign = logging.NullHandler()
    logger.addHandler(foreign)
    try:
        with applied():
            assert foreign in logger.handlers
        assert foreign in logger.handlers
    finally:
        logger.removeHandler(foreign)


# ── 3. idempotens ─────────────────────────────────────────────────────────────


def test_apply_twice_leaves_exactly_one_set_of_handlers():
    logger = logging.getLogger(COMPONENT)
    with applied() as (_, path):
        ls.apply_logging(ls.LoggingConfig(), path)  # andra gången
        ms = managed(logger)
        assert sorted(type(h).__name__ for h in ms) == ["HaForwardHandler", "QueueHandler"]
    assert managed(logger) == []


def test_remove_is_safe_repeatedly_and_without_apply():
    ls.remove_logging()
    ls.remove_logging()
    with applied():
        pass
    ls.remove_logging()


def test_remove_stops_the_listener_thread():
    before = threading.active_count()
    with applied():
        assert threading.active_count() == before + 1
        ls.apply_logging(ls.LoggingConfig(), str(Path(_TMP.name) / "x.log"))  # ersätter, ingen läcka
        assert threading.active_count() == before + 1
    assert threading.active_count() == before


# ── 4. HA-vidarebefordran ─────────────────────────────────────────────────────


def test_ha_gets_only_warning_and_error_by_default_and_exactly_once():
    with applied() as (capture, _):
        emit_all()
    # En gång var: propagate=False, så posten når inte root en andra gång via uppåtpropagering.
    assert capture.levels() == ["WARNING", "ERROR"]


def test_ha_gets_everything_when_verbose():
    with applied(verbose_ha=True) as (capture, _):
        emit_all()
    assert capture.levels() == ["DEBUG", "INFO", "WARNING", "ERROR"]


def test_forward_bypasses_ha_root_level_on_purpose():
    root = logging.getLogger()
    old = root.level
    root.setLevel(logging.ERROR)  # som HA:s "logger: default: error"
    try:
        with applied() as (capture, _):
            emit_all()
        assert capture.levels() == ["WARNING", "ERROR"]
        with applied(verbose_ha=True) as (capture, _):
            emit_all()
        assert capture.levels() == ["DEBUG", "INFO", "WARNING", "ERROR"]
    finally:
        root.setLevel(old)


def test_verbose_is_a_plain_attribute_that_can_change_without_swapping_the_handler():
    root = logging.getLogger()
    capture = Capture()
    root.addHandler(capture)
    try:
        handler = ls.HaForwardHandler()
        handler.handle(make_record("quiet"))
        assert capture.records == []
        handler.verbose = True
        handler.handle(make_record("loud"))
        assert len(capture.records) == 1
    finally:
        root.removeHandler(capture)


# ── 5. fil ────────────────────────────────────────────────────────────────────


def test_file_gets_every_level_after_remove_and_keeps_non_ascii():
    with applied() as (_, path):
        emit_all()
        logging.getLogger(COMPONENT).info("Laddning klar – åäö")
    text = Path(path).read_text(encoding="utf-8")
    for level, msg in (("DEBUG", "d-msg"), ("INFO", "i-msg"), ("WARNING", "w-msg"), ("ERROR", "e-msg")):
        assert f"{level} {COMPONENT}.ocpp_client – {msg}" in text, level
    assert f"INFO {COMPONENT} – Laddning klar – åäö" in text


def test_file_handler_rotates_at_midnight_and_keeps_14():
    with applied() as (_, path):
        handlers = [h for h in sinks() if isinstance(h, logging.handlers.TimedRotatingFileHandler)]
        assert len(handlers) == 1
        fh = handlers[0]
        assert fh.when == "MIDNIGHT"
        assert fh.backupCount == 14
        assert fh.level == logging.DEBUG
        assert fh.encoding == "utf-8"
        assert fh.baseFilename == os.path.abspath(path)
        assert fh.delay is True


def test_traceback_reaches_the_file_and_ha():
    with applied() as (capture, path):
        try:
            1 / 0
        except ZeroDivisionError:
            logging.getLogger(COMPONENT).exception("boom")
    text = Path(path).read_text(encoding="utf-8")
    assert "boom" in text and "Traceback" in text and "ZeroDivisionError" in text
    assert capture.levels() == ["ERROR"] and capture.records[0].exc_info is not None


# ── 6. syslog av ──────────────────────────────────────────────────────────────


def test_no_syslog_handler_when_host_is_empty():
    with applied(syslog_host=""):
        assert not any(isinstance(h, logging.handlers.SysLogHandler) for h in sinks())


# ── 7. syslog på ──────────────────────────────────────────────────────────────


def test_syslog_packet_has_local0_pri_tag_and_message():
    with udp_server() as (srv, port):
        with applied(syslog_host="127.0.0.1", syslog_port=port, syslog_level="DEBUG"):
            emit_all()
            packets = [srv.recv(4096) for _ in range(4)]
    prefix = COMPONENT + ".ocpp_client – "
    # PRI = facility local0 (16) * 8 + severity: debug 7, info 6, warning 4, error 3
    assert packets == [
        f"<135>ocpp_charger: {prefix}d-msg".encode("utf-8"),
        f"<134>ocpp_charger: {prefix}i-msg".encode("utf-8"),
        f"<132>ocpp_charger: {prefix}w-msg".encode("utf-8"),
        f"<131>ocpp_charger: {prefix}e-msg".encode("utf-8"),
    ]
    # Varken avslutande NUL eller BOM (Pythons standard lägger en NUL i varje datagram).
    assert not any(p.endswith(b"\x00") or b"\xef\xbb\xbf" in p for p in packets)


def test_syslog_resolves_the_host_once_and_uses_the_ip():
    with udp_server() as (srv, port):
        with applied(syslog_host="localhost", syslog_port=port):
            sysh = [h for h in sinks() if isinstance(h, logging.handlers.SysLogHandler)]
            assert len(sysh) == 1
            host, p = sysh[0].address
            assert host in ("127.0.0.1", "::1") and p == port  # en IP, inte "localhost"


# ── 8. syslog-nivå ────────────────────────────────────────────────────────────


def test_syslog_level_filters_lower_levels_but_file_still_gets_them():
    with udp_server() as (srv, port):
        with applied(syslog_host="127.0.0.1", syslog_port=port, syslog_level="WARNING") as (_, path):
            emit_all()
            first = srv.recv(4096)
            second = srv.recv(4096)
        srv.settimeout(0.3)
        try:
            extra = srv.recv(4096)
        except socket.timeout:
            extra = None
    assert first.startswith(b"<132>ocpp_charger: ") and first.endswith(b"w-msg")
    assert second.startswith(b"<131>ocpp_charger: ") and second.endswith(b"e-msg")
    assert extra is None
    assert "i-msg" in Path(path).read_text(encoding="utf-8")


# ── 9. ogiltig värd ───────────────────────────────────────────────────────────


def test_unresolvable_syslog_host_does_not_raise_and_warns_once():
    # "nonexistent.invalid" → gaierror (OSError); "a..b" → UnicodeError (inte OSError).
    for bad in ("nonexistent.invalid", "a..b"):
        with applied(syslog_host=bad) as (capture, path):
            assert not any(isinstance(h, logging.handlers.SysLogHandler) for h in sinks()), bad
            emit_all()
        syslog_warnings = [
            r for r in capture.records if r.levelno == logging.WARNING and "Syslog" in r.getMessage()
        ]
        assert len(syslog_warnings) == 1, bad
        # HA-vidarebefordran och filen fungerar som vanligt.
        assert capture.levels() == ["WARNING", "WARNING", "ERROR"], bad  # syslog-varning, w-msg, e-msg
        text = Path(path).read_text(encoding="utf-8")
        assert "d-msg" in text and "e-msg" in text and "Syslog UDP är inte aktiv" in text, bad


# ── 10. handleError-rate-limit ────────────────────────────────────────────────


class FlakySocket:
    """Ersätter SysLogHandler.socket: sendto kastar OSError så länge .fail är sant."""

    def __init__(self):
        self.fail = True
        self.sent = []

    def sendto(self, data, address):
        if self.fail:
            raise OSError(errno.ECONNREFUSED, "Connection refused")
        self.sent.append(data)

    def close(self):
        pass


def test_syslog_handle_error_is_rate_limited_and_reports_recovery():
    root = logging.getLogger()
    capture = Capture()
    root.addHandler(capture)
    component = logging.getLogger(COMPONENT)
    old_level = component.level
    # DEBUG som apply_logging sätter i drift – annars filtrerar root-nivån (WARNING) bort info-raden.
    component.setLevel(logging.DEBUG)
    handler = ls.RateLimitedSysLogHandler(
        address=("127.0.0.1", 9),
        facility=logging.handlers.SysLogHandler.LOG_LOCAL0,
        socktype=socket.SOCK_DGRAM,
    )
    handler.socket.close()
    flaky = FlakySocket()
    handler.socket = flaky
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr):
            handler.handle(make_record("one"))
            handler.handle(make_record("two"))
            assert capture.levels() == ["WARNING"]  # två misslyckade sändningar → en varning
            flaky.fail = False
            handler.handle(make_record("three"))
            assert capture.levels() == ["WARNING", "INFO"]  # återställd
            handler.handle(make_record("four"))
            assert capture.levels() == ["WARNING", "INFO"]  # inget mer så länge det fungerar
            flaky.fail = True
            handler.handle(make_record("five"))
            assert capture.levels() == ["WARNING", "INFO", "WARNING"]  # nytt fel → ny varning
        assert stderr.getvalue() == ""  # ingen "--- Logging error ---" på stderr
        assert len(flaky.sent) == 2  # "three" och "four"
    finally:
        component.setLevel(old_level)
        root.removeHandler(capture)
        handler.close()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as exc:  # AssertionError = fel resultat, annat = kraschade
            failed += 1
            print(f"FAIL  {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
