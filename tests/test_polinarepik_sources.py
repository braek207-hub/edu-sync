"""POLINAREPIK_SOURCES: пилот Panda-BI запускает Директ и Метрику polinarepik отдельными юнитами."""
import pytest

from sync import polinarepik


@pytest.fixture
def calls(monkeypatch):
    seen: list[str] = []
    monkeypatch.setenv("DATABASE_URL", "postgresql:///test")
    for name in ("sync_direct", "sync_metrica", "sync_metrica_sources", "sync_metrica_purchases"):
        monkeypatch.setattr(polinarepik, name, lambda days, n=name: seen.append(n) or 0)
    return seen


def test_empty_runs_all_sources_as_workflow(calls, monkeypatch):
    monkeypatch.delenv("POLINAREPIK_SOURCES", raising=False)
    assert polinarepik.main() == 0
    assert calls == ["sync_direct", "sync_metrica", "sync_metrica_sources", "sync_metrica_purchases"]


def test_metrica_only_skips_direct(calls, monkeypatch):
    monkeypatch.setenv("POLINAREPIK_SOURCES", "metrica")
    assert polinarepik.main() == 0
    assert calls == ["sync_metrica", "sync_metrica_sources", "sync_metrica_purchases"]


def test_direct_failure_does_not_fail_metrica_unit(calls, monkeypatch):
    def boom(days):
        raise RuntimeError("Direct API 204")

    monkeypatch.setattr(polinarepik, "sync_direct", boom)
    monkeypatch.setenv("POLINAREPIK_SOURCES", "metrica")
    assert polinarepik.main() == 0
    monkeypatch.setenv("POLINAREPIK_SOURCES", "direct")
    assert polinarepik.main() == 1


def test_unknown_source_rejected(calls, monkeypatch):
    monkeypatch.setenv("POLINAREPIK_SOURCES", "metrika")
    assert polinarepik.main() == 1
    assert calls == []
