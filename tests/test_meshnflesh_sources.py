"""MESHNFLESH_SOURCES: пилот Panda-BI запускает Директ и Метрику meshnflesh отдельными юнитами."""
import pytest

from sync import meshnflesh_metrika as m


@pytest.fixture
def calls(monkeypatch):
    seen: list[str] = []
    monkeypatch.setenv("DATABASE_URL", "postgresql:///test")
    monkeypatch.setenv("MESHNFLESH_YANDEX_TOKEN", "t")
    monkeypatch.setenv("MESHNFLESH_DIRECT_TOKEN", "t")
    monkeypatch.setattr(m, "fetch_direct_report", lambda *a: seen.append("direct") or [])
    monkeypatch.setattr(m, "_fetch_with_split", lambda fn, *a: seen.append(fn.__name__) or [])
    return seen


def test_empty_runs_all_sources_as_workflow(calls, monkeypatch):
    monkeypatch.delenv("MESHNFLESH_SOURCES", raising=False)
    assert m.main() == 0
    assert calls == ["direct", "fetch_metrica_sources", "fetch_metrica_purchases"]


def test_metrica_only_skips_direct(calls, monkeypatch):
    monkeypatch.setenv("MESHNFLESH_SOURCES", "metrica")
    assert m.main() == 0
    assert calls == ["fetch_metrica_sources", "fetch_metrica_purchases"]


def test_direct_failure_does_not_fail_metrica_unit(calls, monkeypatch):
    def boom(*a):
        raise RuntimeError("Direct API 204")

    monkeypatch.setattr(m, "fetch_direct_report", boom)
    monkeypatch.setenv("MESHNFLESH_SOURCES", "metrica")
    assert m.main() == 0
    monkeypatch.setenv("MESHNFLESH_SOURCES", "direct")
    assert m.main() == 1


def test_unknown_source_rejected(calls, monkeypatch):
    monkeypatch.setenv("MESHNFLESH_SOURCES", "metrika")
    assert m.main() == 1
    assert calls == []
