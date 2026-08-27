# -*- coding: utf-8 -*-
"""
tests/test_agent_e0_ideas.py — реестр идей в отчёте прогона.

Реестр (sync/agent/ideas/registry.py) научился помнить идею дольше такта.
Здесь проверяется его первый выход наружу — секция ideas в отчёте Э0.

Она печатается всегда, в том числе пустой. Пустая секция и отсутствующая —
разные новости: первая говорит «генераторы отработали, находок нет», вторая
читается как «генератор не запускался», и различить их задним числом
по логу нечем: реестр к тому времени выглядит пустым в обоих случаях.

БД подменяется двойником, DATABASE_URL тесты не требуют — конвенция
tests/test_agent_ideas_registry.py.
"""

import json as _json

import sync.agent_e0 as agent_e0
from sync.agent.ideas import registry
from sync.agent.writer import lanes, tier

from tests.test_agent_e0 import _patch_e0_run


def _idea(idea_id="i-1", tier_value=tier.TIER_MEASURED, **over):
    """Строка реестра, как её отдаёт open_ideas: всё обязательное на месте."""
    idea = {
        "idea_id": idea_id,
        "source": "proven",
        "account": "acc-1",
        "subject": {"campaign_id": "111"},
        "subject_key": registry.subject_key({"campaign_id": "111"}),
        "tier": tier_value,
        "lane": lanes.LANE_ALLOCATION,
        "expected_rub": 100_000.0,
        "test_cost_rub": 10_000.0,
        "horizon_days": 14,
        "success_rule": {"metric": "eff_cpl", "op": "<=", "value": 900.0},
        "status": registry.STATUS_NEW,
    }
    idea.update(over)
    return idea


# ---------------------------------------------- секция ideas в отчёте Э0


def test_ideas_section_is_always_present(monkeypatch, capsys):
    # Главное утверждение задачи: секция есть в отчёте даже тогда, когда
    # реестр пуст. Отсутствие ключа читается как «генератор не запускался» —
    # и это единственное состояние, которое нельзя восстановить постфактум.
    _patch_e0_run(monkeypatch)

    assert agent_e0.main() == 0
    report = _json.loads(capsys.readouterr().out)

    assert "ideas" in report


def test_empty_ideas_section_still_names_its_counters():
    # Пустая секция обязана быть РАЗВЁРНУТОЙ: {} неотличимо от «секцию
    # собрать не смогли», а ноль по каждому счётчику — утверждение.
    section = agent_e0.ideas_section([])

    assert section["open"] == 0
    assert section["queue"] == []
    assert section["proposals"]["count"] == 0
    assert section["by_status"] == {}


def test_ideas_section_counts_registry_by_status_source_and_tier():
    section = agent_e0.ideas_section([
        _idea("a", source="proven"),
        _idea("b", source="abtest", status=registry.STATUS_RUNNING),
        _idea("c", source="abtest", tier_value=tier.TIER_BET),
    ])

    assert section["open"] == 3
    assert section["by_source"] == {"abtest": 2, "proven": 1}
    assert section["by_status"] == {registry.STATUS_NEW: 2,
                                    registry.STATUS_RUNNING: 1}
    assert section["by_tier"] == {"1": 2, "2": 1}


def test_proposals_are_counted_apart_from_the_queue():
    # Класс 3 не применяется никогда, и в очереди записи ему не место. Но и
    # прятать его нельзя: это и есть тот экран, ради которого реестр заведён.
    section = agent_e0.ideas_section([
        _idea("a"),
        _idea("p", tier_value=tier.TIER_PROPOSAL, lane=lanes.LANE_PROPOSAL),
    ])

    assert [i["idea_id"] for i in section["queue"]] == ["a"]
    assert section["proposals"]["count"] == 1
    assert [i["idea_id"] for i in section["proposals"]["sample"]] == ["p"]


def test_ideas_section_keeps_the_registry_order():
    # Порядок очереди задаёт реестр (registry.rank — ценность на рубль
    # проверки). Пересортируй отчёт по-своему — и человек увидит один порядок,
    # а такт записи возьмёт другой.
    given = [_idea("cheap"), _idea("dear", test_cost_rub=100_000.0)]

    section = agent_e0.ideas_section(given)

    assert [i["idea_id"] for i in section["queue"]] == ["cheap", "dear"]


def test_ideas_section_sums_the_money_at_stake():
    # Две суммы — обещание реестра и цена его проверки. Без них счётчик идей
    # не говорит ничего: три идеи по сто рублей и три по миллиону выглядят
    # одинаково.
    section = agent_e0.ideas_section([_idea("a"), _idea("b")])

    assert section["expected_rub"] == 200_000.0
    assert section["test_cost_rub"] == 20_000.0


# ------------------------------------------------ чтение открытых идей


def test_open_ideas_asks_only_for_open_statuses():
    # Закрытые идеи в очередь не входят: реестр помнит их ради истории, а не
    # ради повторного предложения. Условие проверяется по тексту запроса —
    # двойник таблицы его не исполняет.
    assert "status = ANY" in registry.SELECT_OPEN_SQL
    assert "account" in registry.SELECT_OPEN_SQL


def test_open_ideas_returns_them_in_queue_order(monkeypatch):
    # Порядок задаёт rank, а не база: SQL-сортировка по идентификатору
    # детерминирована, но к ценности отношения не имеет.
    rows = [_idea("dear", test_cost_rub=100_000.0), _idea("cheap")]
    monkeypatch.setattr(registry, "_read_open", lambda account=None: list(rows))

    assert [i["idea_id"] for i in registry.open_ideas()] == ["cheap", "dear"]
