"""Tester för Feature 9-kopplingen i __init__.py, config_flow.py och översättningarna.

Körs utan Home Assistant:
    python3 tests/test_feature9_wiring.py

__init__.py och config_flow.py importerar HA/voluptuous och kan inte köras här, så kopplingen
granskas via ast (samma grepp som tests/test_feature8.py använder för handler-kontrakten).
Options-flow-klassen körs mot fejkade beroenden: klassen plockas ur config_flow.py via ast och
exec:as med en minimal voluptuous-ersättning. Förväntade värden är handskrivna litteraler.
"""
import __future__
import ast
import asyncio
import copy
import json
import sys
import types
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "ocpp_charger"
sys.path.insert(0, str(PKG))
import const  # noqa: E402  (const.py saknar imports)
import logging_setup  # noqa: E402  (ren stdlib)


def _tree(name):
    return ast.parse((PKG / name).read_text(encoding="utf-8"), filename=name)


def _module_function(name, func_name):
    for node in _tree(name).body:
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == func_name:
            return node
    raise AssertionError(f"{func_name} finns inte i {name}")


def _const_names():
    names = set()
    for node in _tree("const.py").body:
        if isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def _names_imported_from_const(name):
    """Alla namn modulen importerar ur const – både `from .const` och standalone-grenen `from const`."""
    found = set()
    for node in ast.walk(_tree(name)):
        if isinstance(node, ast.ImportFrom) and node.module == "const" and node.level in (0, 1):
            found.update(a.name for a in node.names)
    return found


def _executor_jobs(func):
    """{'logging_setup.apply_logging': rad, ...} för varje hass.async_add_executor_job(<modul>.<funk>, ...)."""
    jobs = {}
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "async_add_executor_job"
            and node.args
            and isinstance(node.args[0], ast.Attribute)
            and isinstance(node.args[0].value, ast.Name)
        ):
            jobs[f"{node.args[0].value.id}.{node.args[0].attr}"] = node.lineno
    return jobs


def _awaited_executor_jobs(func):
    """{'logging_setup.apply_logging': ['log_cfg', ...], ...}: modul.funk -> källtext för övriga argument,
    för varje `await hass.async_add_executor_job(<modul>.<funk>, ...)` i funktionen (bara awaitade anrop)."""
    jobs = {}
    for node in ast.walk(func):
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            call = node.value
            if (
                isinstance(call.func, ast.Attribute)
                and call.func.attr == "async_add_executor_job"
                and call.args
                and isinstance(call.args[0], ast.Attribute)
                and isinstance(call.args[0].value, ast.Name)
            ):
                jobs[f"{call.args[0].value.id}.{call.args[0].attr}"] = [ast.unparse(a) for a in call.args[1:]]
    return jobs


def _first_call_line(func, predicate):
    lines = [n.lineno for n in ast.walk(func) if isinstance(n, ast.Call) and predicate(n)]
    assert lines, "anropet hittades inte"
    return min(lines)


# ── __init__.py ────────────────────────────────────────────────────────────────


def test_every_name_imported_from_const_is_defined():
    defined = _const_names()
    for path in sorted(PKG.glob("*.py")):
        missing = _names_imported_from_const(path.name) - defined
        assert not missing, f"{path.name} importerar okända namn ur const.py: {sorted(missing)}"


def test_init_imports_the_logging_setup_module():
    assert any(
        isinstance(n, ast.ImportFrom)
        and n.level == 1
        and n.module is None
        and any(a.name == "logging_setup" for a in n.names)
        for n in _tree("__init__.py").body
    )


def test_init_no_longer_builds_its_own_file_handler():
    src = (PKG / "__init__.py").read_text(encoding="utf-8")
    assert "RotatingFileHandler" not in src
    assert "/config/ocpp_charger_debug.log" not in src


def test_setup_applies_logging_via_executor_before_the_coordinator_exists():
    func = _module_function("__init__.py", "async_setup_entry")
    jobs = _executor_jobs(func)
    assert "logging_setup.apply_logging" in jobs
    coordinator_line = _first_call_line(
        func, lambda n: isinstance(n.func, ast.Name) and n.func.id == "OCPPCoordinator"
    )
    assert jobs["logging_setup.apply_logging"] < coordinator_line  # uppstartsloggar hamnar rätt
    body = ast.unparse(func)
    assert "logging_setup.config_from_entry_data(entry.data)" in body
    # Awaitat, och i rätt argumentordning (config först, sedan sökvägen) – annars körs det aldrig / fel.
    assert _awaited_executor_jobs(func)["logging_setup.apply_logging"] == [
        "log_cfg",
        "hass.config.path(LOG_FILE_NAME)",
    ]


def test_unload_removes_logging_via_executor_after_the_coordinator_stopped():
    func = _module_function("__init__.py", "async_unload_entry")
    jobs = _executor_jobs(func)
    assert "logging_setup.remove_logging" in jobs
    stop_line = _first_call_line(
        func, lambda n: isinstance(n.func, ast.Attribute) and n.func.attr == "async_stop"
    )
    assert stop_line < jobs["logging_setup.remove_logging"]  # stoppmeddelandena hinner loggas
    assert _awaited_executor_jobs(func)["logging_setup.remove_logging"] == []  # awaitat, utan argument


def test_init_imports_log_file_name_from_const():
    # Saknas importen kraschar async_setup_entry med NameError och hela integrationen laddas inte.
    assert "LOG_FILE_NAME" in _names_imported_from_const("__init__.py")


# ── config_flow.py: options-flow-steget edit_logging ──────────────────────────


class _Marker:
    """voluptuous.Optional/Required-ersättning: nyckel, default och description."""

    def __init__(self, key, default=None, description=None):
        self.schema = key
        self.default = default
        self.description = description


class _FakeSchema:
    """voluptuous.Schema-ersättning. Anropet fyller i default för utelämnade nycklar precis som riktiga
    voluptuous. HA:s frontend utelämnar tomma fält, så det är så ett rensat fält ser ut för flödet."""

    def __init__(self, mapping):
        self.schema = mapping

    def __call__(self, data):
        result = dict(data)
        for marker in self.schema:
            if marker.schema not in result and marker.default is not None:
                result[marker.schema] = marker.default
        return result


FAKE_VOL = types.SimpleNamespace(
    Schema=_FakeSchema,
    Optional=_Marker,
    Required=_Marker,
    All=lambda *validators: ("All", validators),
    Coerce=lambda typ: ("Coerce", typ),
    Range=lambda **kwargs: ("Range", kwargs),
    In=lambda choices: ("In", choices if isinstance(choices, dict) else list(choices)),
)


class _FakeOptionsFlow:
    """config_entries.OptionsFlow-ersättning: formulär och entry blir vanliga dicts."""

    def async_show_form(self, **kwargs):
        return {"type": "form", **kwargs}

    def async_create_entry(self, **kwargs):
        return {"type": "create_entry", **kwargs}


def _make_flow(entry_data):
    """Bygg den riktiga OCPPChargerOptionsFlow (ur config_flow.py) mot fejkade HA-beroenden."""
    class_node = next(
        n
        for n in _tree("config_flow.py").body
        if isinstance(n, ast.ClassDef) and n.name == "OCPPChargerOptionsFlow"
    )
    # Bara namn som config_flow.py själv importerar ur const – saknas en import blir det NameError här.
    namespace = {name: getattr(const, name) for name in _names_imported_from_const("config_flow.py")}
    namespace.update(
        vol=FAKE_VOL,
        copy=copy,
        config_entries=types.SimpleNamespace(OptionsFlow=_FakeOptionsFlow),
    )
    code = compile(
        ast.Module(body=[class_node], type_ignores=[]),
        "config_flow.py",
        "exec",
        flags=__future__.annotations.compiler_flag,  # som filens `from __future__ import annotations`
    )
    exec(code, namespace)
    entry = types.SimpleNamespace(data=dict(entry_data), entry_id="entry-1")
    flow = namespace["OCPPChargerOptionsFlow"](entry)
    updates, tasks = [], []
    flow.hass = types.SimpleNamespace(
        config_entries=types.SimpleNamespace(
            async_update_entry=lambda config_entry, data: updates.append(data),
            async_reload=lambda entry_id: ("reload", entry_id),
        ),
        async_create_task=tasks.append,
    )
    return flow, updates, tasks


def _fields(form):
    """{fältnamn: (default, suggested_value, validator)} ur ett fejkat formulär."""
    return {
        m.schema: (m.default, (m.description or {}).get("suggested_value"), v)
        for m, v in form["data_schema"].schema.items()
    }


def test_menu_offers_logging_just_before_done():
    flow, _, _ = _make_flow({})
    form = asyncio.run(flow.async_step_init())
    menu = next(iter(form["data_schema"].schema.values()))[1]
    assert list(menu)[-2:] == ["logging", "done"]
    assert menu["logging"] == "📝 Edit logging settings"


def test_selecting_logging_opens_the_edit_logging_step():
    flow, _, _ = _make_flow({})
    form = asyncio.run(flow.async_step_init({"action": "logging"}))
    assert form["type"] == "form" and form["step_id"] == "edit_logging"


def test_edit_logging_form_prefills_the_current_values():
    flow, _, _ = _make_flow(
        {"log_verbose_ha": True, "syslog_host": "graylog.lan", "syslog_port": 2514, "syslog_level": "WARNING"}
    )
    form = asyncio.run(flow.async_step_edit_logging())
    assert form["step_id"] == "edit_logging"
    assert _fields(form) == {
        "log_verbose_ha": (True, None, bool),
        "syslog_host": (None, "graylog.lan", str),  # förifylld men utan default (se testet längre ner)
        "syslog_port": (2514, None, ("All", (("Coerce", int), ("Range", {"min": 1, "max": 65535})))),
        "syslog_level": ("WARNING", None, ("In", ["DEBUG", "INFO", "WARNING", "ERROR"])),
    }


def test_edit_logging_form_defaults_for_an_install_without_the_keys():
    flow, _, _ = _make_flow({})
    fields = _fields(asyncio.run(flow.async_step_edit_logging()))
    assert {key: default for key, (default, _, _) in fields.items()} == {
        "log_verbose_ha": False,
        "syslog_host": None,  # ingen default på värden
        "syslog_port": 1514,
        "syslog_level": "DEBUG",
    }
    assert fields["syslog_host"][1] == ""  # men fältet förifylls tomt


def test_clearing_the_syslog_host_turns_syslog_off():
    # HA:s frontend skickar inte tomma fält. Med default= fyllde voluptuous i den gamla värden igen och
    # "tomt = av" gick inte att nå; utan default utelämnas nyckeln och .get(..., "") ger "".
    flow, updates, _ = _make_flow({"syslog_host": "graylog.lan", "syslog_port": 2514})
    form = asyncio.run(flow.async_step_edit_logging())
    submitted = form["data_schema"]({"log_verbose_ha": False, "syslog_port": 2514, "syslog_level": "DEBUG"})
    assert "syslog_host" not in submitted  # värden rensades i UI:t → inget att fylla i
    asyncio.run(flow.async_step_edit_logging(submitted))
    asyncio.run(flow.async_step_init({"action": "done"}))
    assert updates[0]["syslog_host"] == ""
    assert logging_setup.config_from_entry_data(updates[0]).syslog_host == ""  # → syslog av


def test_submit_trims_the_host_returns_to_the_menu_and_save_merges_it():
    flow, updates, tasks = _make_flow({"charger_id": "GaroCS-1", "notify_enabled": True})
    result = asyncio.run(
        flow.async_step_edit_logging(
            {"log_verbose_ha": True, "syslog_host": "  graylog.lan  ", "syslog_port": 1514, "syslog_level": "INFO"}
        )
    )
    assert result["type"] == "form" and result["step_id"] == "init"  # tillbaka i menyn
    done = asyncio.run(flow.async_step_init({"action": "done"}))
    assert done["type"] == "create_entry"
    assert updates == [
        {
            "charger_id": "GaroCS-1",  # övriga nycklar orörda
            "notify_enabled": True,
            "vehicles": [],
            "log_verbose_ha": True,
            "syslog_host": "graylog.lan",  # trimmad
            "syslog_port": 1514,
            "syslog_level": "INFO",
        }
    ]
    assert tasks == [("reload", "entry-1")]  # ändrade loggvärden tillämpas via omladdning


def test_save_without_visiting_the_logging_step_adds_no_logging_keys():
    flow, updates, _ = _make_flow({"charger_id": "GaroCS-1"})
    asyncio.run(flow.async_step_init({"action": "done"}))
    assert updates == [{"charger_id": "GaroCS-1", "vehicles": []}]


# ── strings.json / sv.json / translations/sv.json ─────────────────────────────

LOGGING_FIELDS = {"log_verbose_ha", "syslog_host", "syslog_port", "syslog_level"}


def _logging_step(rel):
    return json.loads((PKG / rel).read_text(encoding="utf-8"))["options"]["step"]["edit_logging"]


def test_strings_json_has_the_spec_texts():
    step = _logging_step("strings.json")
    assert step["title"] == "Logging Settings"
    assert step["description"] == (
        "Full log always goes to ocpp_charger_debug.log (daily rotation, 14 days). "
        "Choose what else to send."
    )
    assert step["data"] == {
        "log_verbose_ha": "Send everything to the Home Assistant log (default: warnings and errors only)",
        "syslog_host": "Syslog host (IP or hostname, empty = off)",
        "syslog_port": "Syslog UDP port",
        "syslog_level": "Minimum level sent to syslog",
    }


def test_both_sv_json_files_have_swedish_texts_for_every_field():
    english = _logging_step("strings.json")
    for rel in ("sv.json", "translations/sv.json"):
        step = _logging_step(rel)
        assert set(step["data"]) == LOGGING_FIELDS, rel
        assert step["title"] and step["description"], rel
        assert step["title"] != english["title"], rel  # översatt
        assert all(step["data"][k] and step["data"][k] != english["data"][k] for k in LOGGING_FIELDS), rel


def test_translated_fields_match_the_form_fields():
    flow, _, _ = _make_flow({})
    form = asyncio.run(flow.async_step_edit_logging())
    assert set(_fields(form)) == LOGGING_FIELDS


def test_existing_steps_survive_in_every_translation_file():
    for rel in ("strings.json", "sv.json", "translations/sv.json"):
        steps = json.loads((PKG / rel).read_text(encoding="utf-8"))["options"]["step"]
        for name in ("init", "edit_notify", "edit_planner", "edit_logging"):
            assert name in steps, f"{rel}: {name}"


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
