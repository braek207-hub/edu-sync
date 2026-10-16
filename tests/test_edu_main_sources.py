"""EDU_SOURCES: пилот Panda-BI запускает источники синка EDU (main.py) отдельными юнитами."""
import types

import pytest

import main


@pytest.fixture
def calls(monkeypatch):
    seen: list[str] = []
    fakes = {}
    for _, module, func in main.STEPS:
        mod = fakes.setdefault(module, types.SimpleNamespace())
        setattr(mod, func, lambda f=func: seen.append(f))
    monkeypatch.setattr(main.importlib, "import_module", lambda name: fakes[name])
    monkeypatch.setattr(main, "_resolve_env", lambda: None)
    for k, v in {"DATABASE_URL": "postgresql:///test", "DIRECT_TOKEN": "t", "DIRECT_CLIENTS_JSON": "[]",
                 "GOOGLE_SHEETS_ID": "s", "GOOGLE_APPLICATION_CREDENTIALS": "sa.json"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setitem(__import__("sys").modules, "sync.validate_sheets",
                        types.SimpleNamespace(run_validation=lambda: seen.append("validate")))
    return seen


def test_empty_runs_all_steps_as_workflow(calls, monkeypatch):
    monkeypatch.delenv("EDU_SOURCES", raising=False)
    assert main.main() == 0
    assert calls == ["sync_direct_all", "sync_crm_leads", "sync_crm_payments", "sync_metrika_offline",
                     "sync_edu_visits", "sync_lime_appmetrica", "validate"]


def test_direct_only_needs_no_google(calls, monkeypatch):
    monkeypatch.setenv("EDU_SOURCES", "direct")
    monkeypatch.delenv("GOOGLE_SHEETS_ID")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS")
    assert main.main() == 0
    assert calls == ["sync_direct_all"]


def test_crm_without_sheet_fails_before_any_step(calls, monkeypatch):
    monkeypatch.setenv("EDU_SOURCES", "crm,visits")
    monkeypatch.delenv("GOOGLE_SHEETS_ID")
    assert main.main() == 1
    assert calls == []


def test_failed_source_fails_only_its_unit(calls, monkeypatch):
    def boom():
        raise RuntimeError("Direct API 204")

    real = main.importlib.import_module
    monkeypatch.setattr(main.importlib, "import_module",
                        lambda name: types.SimpleNamespace(sync_direct_all=boom) if name == "sync.direct" else real(name))
    monkeypatch.setenv("EDU_SOURCES", "visits")
    assert main.main() == 0
    monkeypatch.setenv("EDU_SOURCES", "direct")
    assert main.main() == 1


def test_unknown_source_rejected(calls, monkeypatch):
    monkeypatch.setenv("EDU_SOURCES", "metrika")
    assert main.main() == 1
    assert calls == []
