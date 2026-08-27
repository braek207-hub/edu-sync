# -*- coding: utf-8 -*-
"""
tests/test_agent_e0_ideas.py — реестр идей в отчёте прогона.

Реестр (sync/agent/ideas/registry.py) научился помнить идею дольше такта.
Здесь проверяются два его выхода наружу, и оба ломаются молча:

  * СЕКЦИЯ ideas В ОТЧЁТЕ Э0 печатается всегда, в том числе пустой. Пустая
    секция и отсутствующая — разные новости: первая говорит «генераторы
    отработали, находок нет», вторая читается как «генератор не запускался»,
    и различить их задним числом по логу нечем.

  * ИДЕЯ КЛАССА 3 (предложение) не попадает в план записи НИКОГДА. У неё нет
    рычага (writer/tier.py), и её место — экран человеку, а не кабинет.
    Защита стоит НА ВХОДЕ такта записи, а не только в отборе полос
    (lanes.select): отбор судит действие, а до него идея успела бы стать
    строкой плана, занять слот объекта и попасть в журнал.

БД подменяется двойником, DATABASE_URL тесты не требуют — конвенция
tests/test_agent_ideas_registry.py.
"""

import json as _json

import sync.agent_e0 as agent_e0
import sync.agent_e1 as agent_e1
from sync.agent import rejects
from sync.agent.ideas import registry
from sync.agent.writer import lanes, tier

from tests.test_agent_e0 import _patch_e0_run
from tests.test_agent_e1 import _patch_run, _reports, _setting


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


def _action(campaign_id="111", kind="bidmodifier.add"):
    """Готовое действие рычага — то, что идея приносит с собой в такт записи.

    Полезная нагрузка едет ПРИ идее, а не внутри subject: subject — адрес
    объекта, и изменчивое число в нём меняло бы idea_id каждым прогоном
    (докстринг registry.py). Поэтому у идеи есть отдельное поле action, в
    реестр оно не пишется и на идентичность не влияет.
    """
    return {
        "action_kind": kind,
        "object_level": "campaign",
        "object_id": str(campaign_id),
        "direct_type": "DEVICE_MULTIPLIER",
        "key": "MOBILE",
        "payload": {"CampaignId": int(campaign_id), "Type": "DEVICE_MULTIPLIER",
                    "key": "MOBILE", "BidModifier": 10},
        "idempotency_key": f"idea-{campaign_id}-{kind}",
    }


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


# ------------------------------------- идеи класса 3 и план записи Э1


def test_proposals_never_reach_the_write_plan():
    # Шаг 2 задачи 11 дословно: предложение не порождает действия ни при
    # какой ступени полосы и ни при каком остатке риск-бюджета.
    actions, refused = agent_e1.actions_from_ideas(
        [_idea("p", tier_value=tier.TIER_PROPOSAL, action=_action())])

    assert actions == []
    assert [r["reason"] for r in refused] == [rejects.PROPOSAL]


def test_idea_without_a_tier_is_treated_as_a_proposal():
    # Пустой класс НЕЛЬЗЯ читать нулём: ноль — это арифметика, которая
    # применяется всегда и риском не платит. Умолчание обязано быть самым
    # строгим концом шкалы, иначе забытое поле открывает кабинет.
    actions, refused = agent_e1.actions_from_ideas(
        [_idea("x", tier_value=None, action=_action())])

    assert actions == []
    assert [r["reason"] for r in refused] == [rejects.PROPOSAL]


def test_idea_without_a_lever_never_reaches_the_write_plan():
    # Идея без рычага — повод, а не действие: применять нечем. Такие живут в
    # реестре для человека и в план не едут.
    actions, refused = agent_e1.actions_from_ideas([_idea("no-lever")])

    assert actions == []
    assert [r["reason"] for r in refused] == [rejects.PROPOSAL]


def test_idea_whose_kind_has_no_lane_is_refused_not_crashed():
    # Вид действия вне карты полос — дефект генератора. Он обязан стать
    # названным отказом, а не исключением: одна кривая идея не имеет права
    # уронить такт записи целиком.
    actions, refused = agent_e1.actions_from_ideas(
        [_idea("weird", action=_action(kind="landing.rewrite"))])

    assert actions == []
    assert [r["reason"] for r in refused] == [rejects.PROPOSAL]


def test_closed_idea_does_not_reach_the_write_plan():
    # Закрытая идея — запись о случившемся. Отклонённая человеком приезжает
    # сюда именно в этом статусе (registry._silence), и повторно применить её
    # значило бы обойти его «нет».
    actions, refused = agent_e1.actions_from_ideas(
        [_idea("done", status=registry.STATUS_DROPPED, action=_action())])

    assert actions == []
    assert [r["reason"] for r in refused] == [rejects.CLOSED_KEY]


def test_measured_idea_with_a_lever_becomes_an_action():
    # Обратная сторона запрета: идея с рычагом и доказанным классом обязана
    # доехать. Без этого теста «не пускать класс 3» чинится тем, что не
    # пускается вообще ничего.
    actions, refused = agent_e1.actions_from_ideas(
        [_idea("ok", action=_action())])

    assert refused == []
    assert [a["action_kind"] for a in actions] == ["bidmodifier.add"]


def test_action_from_an_idea_carries_its_idea_id():
    # Связь «действие ← идея» нужна замеру: без неё исход применения нечем
    # вернуть в реестр, и идея навсегда останется в статусе new.
    actions, _ = agent_e1.actions_from_ideas([_idea("ok", action=_action())])

    assert actions[0]["idea_id"] == "ok"


def test_idea_class_can_only_tighten_the_action():
    # Класс идеи вправе ужесточить приговор действию, но не смягчить его:
    # иначе генератор выписывал бы своему действию освобождение от риска
    # (то же правило, что в writer/tier.tier_of).
    actions, _ = agent_e1.actions_from_ideas(
        [_idea("bet", tier_value=tier.TIER_BET, action=_action())])

    assert tier.tier_of(actions[0]) == tier.TIER_BET


def test_one_bad_idea_does_not_swallow_the_good_ones():
    # Порция идей не принимается «целиком или никак»: в отличие от записи в
    # реестр, здесь отказ одной идеи — штатное состояние такта, и остановка
    # всей порции означала бы, что одна кривая находка глушит кабинет.
    actions, refused = agent_e1.actions_from_ideas([
        _idea("p", tier_value=tier.TIER_PROPOSAL, action=_action()),
        _idea("ok", action=_action(campaign_id="222")),
    ])

    assert [a["idea_id"] for a in actions] == ["ok"]
    assert [r["idea_id"] for r in refused] == ["p"]


# ------------------------------------------ врезка: сквозной прогон Э1


def test_proposal_from_the_registry_does_not_become_a_planned_row(
        monkeypatch, capsys):
    # Чистая функция может судить верно, а такт записи — не спрашивать её
    # вовсе. Поэтому проверка идёт по боевому пути, и кабинету нарочно
    # дана своя работа: план у прогона есть, и видно, что предложение
    # в него не вошло, а не что план пуст сам по себе.
    _patch_run(monkeypatch,
               {"acc-1": [_setting("bid_modifier:device", "DESKTOP", 30)]},
               {"acc-1": [111]}, {"111": 1000.0})
    monkeypatch.setattr(
        agent_e1.ideas_registry, "open_ideas",
        lambda *a, **k: [_idea("p", tier_value=tier.TIER_PROPOSAL,
                               action=_action())])

    assert agent_e1.main() == 0
    report = _reports(capsys)[0]

    assert report["planned"] == 1                     # только корректировка
    assert report["ideas"]["actions"] == 0
    assert report["ideas"]["refused_by_reason"] == {rejects.PROPOSAL: 1}


def test_idea_with_a_lever_reaches_the_planned_rows(monkeypatch, capsys):
    # Та же врезка с обратным знаком: идея класса 1 с рычагом доезжает до
    # плана. Иначе тест выше был бы зелёным и на коде, который реестр не
    # читает вовсе.
    _patch_run(monkeypatch, {"acc-1": []}, {"acc-1": [111]}, {"111": 1000.0})
    monkeypatch.setattr(
        agent_e1.ideas_registry, "open_ideas",
        lambda *a, **k: [_idea("ok", action=_action())])

    assert agent_e1.main() == 0
    report = _reports(capsys)[0]

    # Кабинет без вычисленных настроек раньше выходил из прогона сразу
    # («нет значимых корректировок») — и именно такой кабинет (свежий,
    # без истории) нуждается в идеях больше всех. Идея — самостоятельный
    # повод действовать, и ранний выход обязан её учитывать.
    assert report["planned"] == 1
    assert report["ideas"]["actions"] == 1


def test_idea_outside_the_run_scope_is_not_planned(monkeypatch, capsys):
    # Первый боевой прогон запускается по одной кампании (--max-campaigns=1),
    # и рычаг, пришедший из реестра, не вправе обойти это ограничение. Тот же
    # фильтр закрывает идею на кампанию, которой в кабинете нет вовсе:
    # обращение к чужому Id — гарантированная ошибка и потраченные баллы.
    _patch_run(monkeypatch,
               {"acc-1": [_setting("bid_modifier:device", "DESKTOP", 30)]},
               {"acc-1": [111]}, {"111": 1000.0})
    monkeypatch.setattr(
        agent_e1.ideas_registry, "open_ideas",
        lambda *a, **k: [_idea("alien", action=_action(campaign_id="999"))])

    assert agent_e1.main() == 0
    report = _reports(capsys)[0]

    assert report["ideas"]["actions"] == 0
    assert report["ideas"]["out_of_scope"] == 1
    # Отказом это не считается: рамки запуска — не приговор идее, и назавтра
    # без ограничителя она уедет без единой правки.
    assert report["ideas"]["refused_by_reason"] == {}


def test_ideas_are_asked_for_this_cabinet_only(monkeypatch, capsys):
    # Реестр общий на все кабинеты, а идея принадлежит одному: спроси его без
    # фильтра — и чужая идея уедет в чужой кабинет, где её объекта нет.
    _patch_run(monkeypatch, {"acc-1": []}, {"acc-1": [111]}, {"111": 1000.0})
    asked = []
    monkeypatch.setattr(
        agent_e1.ideas_registry, "open_ideas",
        lambda account=None, *a, **k: asked.append(account) or [])

    assert agent_e1.main() == 0
    capsys.readouterr()

    assert asked == ["acc-1"]


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
