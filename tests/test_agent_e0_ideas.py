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
from sync.agent import demand as demand_mod, rejects
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
        "subject_key": registry.subject_key({"campaign_id": "111"}, "acc-1"),
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
    (докстринг registry.py). Поэтому у идеи есть отдельная колонка action —
    на идентичность она не влияет, но в базе живёт: такт записи читает идеи
    оттуда, и всё, чего в колонке нет, для него не существует.
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

    # Кабинетный цикл спрашивает СВОЙ кабинет; хвостовой вызов без фильтра —
    # прогонная сводка (счёт идей-предложений для Telegram, 03.09.2026), она
    # ничего не применяет и в кабинеты не едет.
    assert asked == ["acc-1", None]


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


# =========================================================================
# Идея, проверка которой уже идёт, не переигрывается каждый такт.
#
# registry.mark не вызывался никогда: применённая идея оставалась в статусе
# new, open_ideas отдавал её снова, и назавтра то же действие поехало бы
# вторым. Две половины одного механизма — отметка на выходе такта и отказ на
# входе следующего, — и обе обязаны проверяться порознь: отметка без отказа
# бесполезна, отказ без отметки никогда не сработает.
# =========================================================================


def test_running_idea_is_not_replayed_next_tact():
    # Идея, уже уехавшая в кабинет, второй раз не едет: горизонт её проверки
    # ещё не вышел, и повторное применение того же рычага не «ускорит»
    # результат, а испортит замер — исход нечем будет приписать.
    actions, refused = agent_e1.actions_from_ideas(
        [_idea("run", status=registry.STATUS_RUNNING, action=_action())])

    assert actions == []
    assert [r["reason"] for r in refused] == [rejects.IDEA_RUNNING]


def test_running_idea_is_refused_by_its_own_reason():
    # Своя причина, а не чужая с другим смыслом. closed_key означает «ключ
    # закрыт финальным статусом» и лечится ожиданием следующего окна;
    # proposal — «рычага нет вовсе» и не лечится ничем. Идея в работе — это
    # третье: рычаг есть, он УЖЕ применён, идёт замер.
    assert rejects.IDEA_RUNNING in rejects.KNOWN_REASONS
    assert rejects.IDEA_RUNNING not in {rejects.CLOSED_KEY, rejects.PROPOSAL}


def test_queued_idea_travels_like_a_new_one():
    # queued — «человек сказал: в работу». Для такта записи это то же самое,
    # что new: рычаг ещё не применён, замер не начат. Отличай их такт — и
    # взятая человеком в работу идея встала бы навсегда.
    actions, refused = agent_e1.actions_from_ideas(
        [_idea("q", status=registry.STATUS_QUEUED, action=_action())])

    assert refused == []
    assert [a["idea_id"] for a in actions] == ["q"]


def _detail(action, result="applied"):
    return {"key": action["idempotency_key"], "result": result}


def test_applied_idea_is_marked_running(monkeypatch):
    # Единственная связь между тактом записи и жизненным циклом идеи. Без
    # неё реестр отдаёт применённую идею открытой каждое утро, и одно и то
    # же действие уезжает в кабинет каждый день.
    marks = []
    monkeypatch.setattr(agent_e1.ideas_registry, "mark",
                        lambda *a, **k: marks.append((a, k)) or {})
    monkeypatch.setattr(agent_e1.writer_db, "make_action_id",
                        lambda key: f"act:{key}")
    action = {**_action(), "idea_id": "ok"}

    outcome = agent_e1.mark_applied_ideas([action], [_detail(action)])

    assert outcome["running"] == ["ok"]
    assert marks == [(("ok", registry.STATUS_RUNNING),
                      {"action_id": f"act:{action['idempotency_key']}"})]


def test_idea_blocked_before_the_cabinet_is_not_marked(monkeypatch):
    # Отметка идёт по факту ПРИМЕНЕНИЯ, а не планирования. Планирование
    # может кончиться рельсой (заповедник, кулдаун, бюджет), и действие до
    # кабинета не доедет — отметь такую идею running, и она застряла бы в
    # реестре навсегда: закрывать нечего, применять нельзя.
    marks = []
    monkeypatch.setattr(agent_e1.ideas_registry, "mark",
                        lambda *a, **k: marks.append(a) or {})
    action = {**_action(), "idea_id": "blocked"}

    outcome = agent_e1.mark_applied_ideas([action], [])

    assert outcome["running"] == []
    assert marks == []


def test_rehearsal_does_not_move_the_idea(monkeypatch):
    # Репетиция (--dry-run) в кабинет не ходит. Двинь она статус — реестр
    # считал бы идею проверяемой, а в кабинете не изменилось бы ничего, и
    # горизонт истёк бы впустую.
    marks = []
    monkeypatch.setattr(agent_e1.ideas_registry, "mark",
                        lambda *a, **k: marks.append(a) or {})
    action = {**_action(), "idea_id": "rehearsed"}

    outcome = agent_e1.mark_applied_ideas(
        [action], [_detail(action, result="dry_run")])

    assert outcome["running"] == []
    assert marks == []


def test_actions_without_an_idea_are_not_marked(monkeypatch):
    # В план едут действия всех рычагов, а не только идейные. Действие без
    # idea_id реестру не принадлежит, и отметка по нему упала бы «идеи нет».
    marks = []
    monkeypatch.setattr(agent_e1.ideas_registry, "mark",
                        lambda *a, **k: marks.append(a) or {})
    action = dict(_action())

    assert agent_e1.mark_applied_ideas([action], [_detail(action)])["running"] == []
    assert marks == []


def test_a_refused_mark_does_not_break_the_run(monkeypatch):
    # Человек мог отклонить идею между чтением реестра и отправкой: строка
    # закрыта, и mark законно отказывает. Действие при этом УЖЕ применено в
    # кабинете — падение здесь оставило бы прогон без отчёта о том, что он
    # только что сделал. Отказ становится видимой строкой, а не трассой.
    def _refuse(*a, **k):
        raise registry.InvalidIdea("идея закрыта")

    monkeypatch.setattr(agent_e1.ideas_registry, "mark", _refuse)
    monkeypatch.setattr(agent_e1.writer_db, "make_action_id", lambda key: "act")
    action = {**_action(), "idea_id": "gone"}

    outcome = agent_e1.mark_applied_ideas([action], [_detail(action)])

    assert outcome["running"] == []
    assert [f["idea_id"] for f in outcome["failed"]] == ["gone"]


def test_applying_an_idea_marks_it_running_in_the_run(monkeypatch, capsys):
    # Врезка: чистая функция может судить верно, а прогон — не звать её
    # вовсе. Ровно этим и был мёртв путь идей до сих пор.
    _patch_run(monkeypatch, {"acc-1": []}, {"acc-1": [111]}, {"111": 1000.0})
    monkeypatch.setattr(
        agent_e1.ideas_registry, "open_ideas",
        lambda *a, **k: [_idea("ok", action=_action())])
    marks = []
    monkeypatch.setattr(agent_e1.ideas_registry, "mark",
                        lambda *a, **k: marks.append(a) or {})
    # Настоящее применение в тестах не ходит в кабинет (двойник клиента
    # отвечает репетицией), а проверяется здесь именно ветка боевого исхода.
    monkeypatch.setattr(
        agent_e1, "apply_actions",
        lambda client, prepared, db, **k: {
            "applied": len(prepared), "skipped": 0, "failed": 0, "rejected": 0,
            "dry_run": 0, "unknown_outcome": 0, "conflicted": 0, "deferred": 0,
            "units_low": 0, "rejects": [],
            "details": [{"key": a["idempotency_key"], "result": "applied"}
                        for a in prepared]})

    assert agent_e1.main() == 0
    report = _reports(capsys)[0]

    assert marks == [("ok", registry.STATUS_RUNNING)]
    assert report["ideas"]["running"] == 1


# =========================================================================
# Генераторы включаются в расчётный такт (задача 16а).
#
# До этого пять генераторов Ф13 были мёртвым кодом: чистые функции есть,
# связки не собирает никто, registry.upsert в бою не вызывается ниоткуда.
# Реестр оставался пустым, секция ideas честно печатала нули — и путь идей
# выглядел работающим ровно до первого вопроса «почему предложений нет».
#
# Проверяется здесь то, что ломается молча: находки доезжают до реестра;
# отбракованные связки попадают в отчёт числами по причинам; кабинеты не
# затирают друг друга в общей порции.
# =========================================================================

from datetime import date as _date

from sync.agent.ideas import bundles as ideas_bundles

_POOL = {"paid": 120.0, "deals": 200.0, "connected": 400.0,
         "eff": 900.0, "leads": 1800.0, "clicks": 90_000.0}


def _rows_of(store, source):
    return [row for row in store.table.values() if row["source"] == source]


def _ladder_section(campaigns=("111",), directions=("vuz",)):
    return {
        "window_from": "2026-05-01",
        "window_to": "2026-07-29",
        "by_object": {c: {"step": "eff", "events_by_step": dict(_POOL)}
                      for c in campaigns},
        "counts": {"by_direction": {d: dict(_POOL) for d in directions},
                   "account": dict(_POOL)},
        "avg_check": {c: 60_000.0 for c in campaigns},
    }


def _portfolio_section(moves):
    """moves: {кабинет: {кампания: направление}}."""
    return {"accounts": {
        login: {"lambda": 1.0, "moves": {
            campaign: {"direction": direction, "value_per_lead": 1_500.0,
                       "cost_28d": 300_000.0, "leads_28d": 200,
                       "limit_binding": True}
            for campaign, direction in campaigns.items()}}
        for login, campaigns in moves.items()}}


def _run_facts(campaigns=(("111", "vuz"),)):
    return [{"fact_date": "2026-07-01", "campaign_id": campaign,
             "direction": direction, "cost": 10_000.0, "eff_leads": 8}
            for campaign, direction in campaigns]


def _slices(campaign="111"):
    """Срез, на котором мобильные окупаются заметно лучше остального объёма."""
    return [
        {"campaign_id": campaign, "slice_kind": "device", "slice_key": "MOBILE",
         "clicks": 6_000.0, "conversions": 300.0, "cost": 90_000.0},
        {"campaign_id": campaign, "slice_kind": "device", "slice_key": "DESKTOP",
         "clicks": 6_000.0, "conversions": 40.0, "cost": 180_000.0},
    ]


def _collect(**over):
    kwargs = {
        "facts": _run_facts(),
        "ladder_section": _ladder_section(),
        "portfolio_section": _portfolio_section({"acc-1": {"111": "vuz"}}),
        "sliced_rows": _slices(),
        "query_rows": [],
        "expansion": [],
        "demand": {},
        # Витрина снята и корректировок в ней нет: связка вправе утверждать,
        # что add применим.
        "settings_by_campaign": {"111": {"bidModifiers": {"total": 0,
                                                          "items": []}}},
        "login_by_campaign": {"111": "acc-1"},
        "direction_by_campaign": {"111": "vuz"},
        "holdout_ids": [],
        "learning_reset": {},
        "learning_reset_read": True,
        "quality_drift": {},
        "config": {},
        "slice_window_days": 90,
        "query_window_days": 30,
        "today": _date(2026, 8, 27),
    }
    kwargs.update(over)
    return agent_e0.collect_ideas(**kwargs)


def test_generators_findings_reach_the_registry(store):
    # Шаг 2 плана беты дословно: расчётный такт пишет находки генераторов в
    # реестр. Двойник — та же таблица в памяти, что у тестов реестра.
    summary = _collect()

    assert summary["by_source"]["proven"]["ideas"] == 1
    assert summary["by_source"]["proven"]["upserted"] == 1
    assert len(_rows_of(store, "proven")) == 1


def test_the_tact_sweeps_stale_rows_of_the_live_registry(store):
    # Пределы применялись только в момент записи и доставали лишь то, что
    # генератор находит сегодня заново. Строка, которую он перестал находить,
    # висела в очереди навсегда: замер 29.08.2026 (прод) — две идеи proven с
    # ожиданием 32 ₽ при цене замера 933 ₽ и 129 ₽ при 829 ₽ пережили порог
    # именно так. Проход стоит В ТАКТЕ, а не отдельной командой: очередь
    # читается каждым прогоном, и чинить её надо там же, где читают.
    stale = _idea(idea_id="stale-1", expected_rub=32.0, test_cost_rub=933.0)
    store.table["stale-1"] = stale

    summary = _collect()

    assert summary["swept"]["closed"] == 1
    assert store.table["stale-1"]["status"] == registry.STATUS_DROPPED
    assert sum(summary["swept"]["by_reason"].values()) == 1


def test_the_sweep_does_not_take_down_the_tact(store, monkeypatch):
    # Расчётный такт считает деньги, и падать из-за экрана предложений ему
    # нельзя: недоступный реестр становится строкой отчёта, как и остальные
    # его чтения (closure.unavailable).
    def _boom(*a, **k):
        raise RuntimeError("реестр недоступен")

    monkeypatch.setattr(agent_e0.ideas_registry, "sweep_open", _boom)

    summary = _collect()

    assert summary["swept"]["closed"] == 0
    assert "реестр недоступен" in summary["swept"]["unavailable"]


def test_refused_bundles_are_counted_by_reason(store):
    # Шаг 3: отбракованные связки попадают в отчёт числами по причинам.
    # «Поводов не нашлось» и «поводы были, но у всех не хватило рычага» ведут
    # к разным следующим шагам, и по одному нулю их не различить.
    summary = _collect(settings_by_campaign={})

    reasons = summary["by_source"]["proven"]["skipped_by_reason"]
    assert summary["by_source"]["proven"]["ideas"] == 0
    assert sum(reasons.values()) == 2
    assert any("неизвестно" in reason for reason in reasons)


def test_a_slice_of_an_unknown_campaign_is_counted_as_a_refused_bundle(store):
    # Отбраковка самой СБОРКИ считается отдельно от отбраковки генераторов:
    # «связку не собрали» лечится данными такта, «генератор отказал» — нет.
    summary = _collect(sliced_rows=_slices("999"))

    assert summary["bundles"]["skipped_by_reason"] == {
        ideas_bundles.REASON_UNKNOWN_CAMPAIGN: 2}
    assert summary["bundles"]["segments"] == 0


def test_the_generator_without_an_input_is_named_in_the_report(store):
    # Ноль находок генератора аудиторий значит «спрашивать было не о чем», а
    # не «поводов не нашлось». По пустому счётчику эти два состояния
    # неразличимы, и дыру во входе никто бы не заметил.
    summary = _collect()

    assert "audience" in summary["sources_without_input"]


def test_two_cabinets_with_the_same_direction_do_not_overwrite_each_other(store):
    # Адрес идеи у половины генераторов — НАПРАВЛЕНИЕ, а «vuz» есть сразу в
    # нескольких кабинетах. Без кабинета в идентификаторе находка второго
    # кабинета молча затирала бы находку первого в той же порции — и отказ
    # человека в одном кабинете глушил бы идею во всех остальных.
    summary = _collect(
        facts=_run_facts((("111", "vuz"), ("222", "vuz"))),
        ladder_section=_ladder_section(("111", "222")),
        portfolio_section=_portfolio_section({"acc-1": {"111": "vuz"},
                                              "acc-2": {"222": "vuz"}}),
        sliced_rows=[],
        expansion=[{"query": "колледж заочно", "campaigns": ["111", "222"]}],
        demand={"vuz": {"regime": "подъём", "sigma": 2.4, "frequency": 12_000,
                        "baseline_median": 9_000, "last_week": "2026-08-17"}},
        login_by_campaign={"111": "acc-1", "222": "acc-2"},
        direction_by_campaign={"111": "vuz", "222": "vuz"},
        settings_by_campaign={})

    assert summary["by_source"]["market"]["ideas"] == 2
    assert summary["by_source"]["market"]["upserted"] == 2
    written = _rows_of(store, "market")
    assert {row["account"] for row in written} == {"acc-1", "acc-2"}
    assert len(written) == 2


def test_ideas_of_a_cabinet_are_judged_by_its_own_threshold(store):
    # Порог λ у каждого кабинета свой. Посуди связку одного порогом другого —
    # и приговор вынесен по чужой мерке.
    portfolio = _portfolio_section({"acc-1": {"111": "vuz"}})
    portfolio["accounts"]["acc-1"]["lambda"] = 1_000.0

    summary = _collect(portfolio_section=portfolio)

    assert summary["by_source"]["proven"]["ideas"] == 0


def test_a_rejected_portion_does_not_kill_the_run(monkeypatch, store):
    # Расчётный такт считает деньги. Падение из-за экрана предложений
    # оставило бы прогон без отчёта о том, что он только что посчитал, —
    # поэтому отказ реестра становится строкой отчёта, а не трассой.
    def _refuse(rows):
        raise registry.InvalidIdea("порция негодна")

    monkeypatch.setattr(agent_e0.ideas_registry, "upsert", _refuse)

    summary = _collect()

    assert "proven" in summary["failed"]
    assert summary["by_source"]["proven"]["ideas"] == 1
    assert summary["by_source"]["proven"]["upserted"] == 0


def test_each_source_is_written_in_its_own_portion(monkeypatch, store):
    # Реестр принимает порцию целиком или никак. Одна кривая находка обязана
    # уронить находки СВОЕГО генератора, а не всего такта: общего кода и общей
    # причины ошибиться у пяти генераторов нет.
    portions = []
    real = agent_e0.ideas_registry.upsert

    def _spy(rows):
        portions.append({row["source"] for row in rows})
        return real(rows)

    monkeypatch.setattr(agent_e0.ideas_registry, "upsert", _spy)

    _collect(expansion=[{"query": "колледж заочно", "campaigns": ["111"]}],
             demand={"vuz": {"regime": "подъём", "sigma": 2.4,
                             "frequency": 12_000, "baseline_median": 9_000,
                             "last_week": "2026-08-17"}})

    assert portions and all(len(p) == 1 for p in portions)
    assert {"proven"} in portions and {"market"} in portions


def test_the_run_prints_what_the_generators_found_this_tact(monkeypatch, capsys):
    # Врезка: чистая функция может считать честно, а прогон — не звать её
    # вовсе. Ровно этим и был мёртв путь идей до сих пор. Счётчик реестра
    # такое не ловит: реестр помнит и вчерашние находки, и по нему прогон,
    # не позвавший ни одного генератора, неотличим от прогона без находок.
    _patch_e0_run(monkeypatch)

    assert agent_e0.main() == 0
    report = _json.loads(capsys.readouterr().out)

    generated = report["ideas"]["generated"]
    assert "by_source" in generated and "bundles" in generated
    assert "audience" in generated["sources_without_input"]


def test_master_campaign_cards_reach_the_registry(store):
    """Шестой генератор позван тактом, а не остался чистой функцией.

    Ровно этой болезнью пять первых генераторов болели до задачи: код
    существовал, звать его было некому, реестр оставался пустым, и отчёт при
    этом выглядел работающим. Проверяем сквозняк: карточка Мастера кампаний
    из sync/agent/master.py доезжает до строки реестра — с полосой 7, классом
    3 и без нагрузки рычага.
    """
    card = {
        "account": "acc-1",
        "campaign_id": "705571231",
        "campaign_name": "vsekolledzhi_postupi  / Общее / МК / МСК",
        "direction": "other",
        "cost_rub": 1_256_175.0,
        "clicks": 21_105.0,
        "leads": 691.0,
        "eff_leads": 623.0,
        "revenue_rub": 532_000.0,
        "window_days": 28,
        "account_cost_rub": 12_776_230.0,
        "share_of_account": 0.0983,
        "base_cpl_rub": 1_500.0,
        "api": None,
    }

    summary = _collect(master_rows=[card])

    assert summary["by_source"]["master_campaign"]["upserted"] == 1
    rows = _rows_of(store, "master_campaign")
    assert len(rows) == 1
    assert rows[0]["lane"] == "proposal"
    assert rows[0]["tier"] == 3
    assert rows[0]["action"] is None


def test_master_cards_of_another_account_do_not_leak(store):
    """Карточка чужого кабинета в очередь этого не попадает.

    Порог доли, цена лида и заповедник у каждого кабинета свои, и идея,
    посчитанная по чужой мерке, — приговор не тому кабинету.
    """
    card = {
        "account": "acc-2",
        "campaign_id": "705571231",
        "cost_rub": 1_256_175.0,
        "eff_leads": 623.0,
        "window_days": 28,
        "share_of_account": 0.0983,
        "base_cpl_rub": 1_500.0,
        "api": None,
    }

    summary = _collect(master_rows=[card])

    assert summary["by_source"]["master_campaign"]["ideas"] == 0
    assert _rows_of(store, "master_campaign") == []

# ------------------------------- спрос без адресата (диагностика 28.08)


def test_rising_demand_nobody_serves_is_named_in_the_report(store):
    # Поводы спроса раздаются кабинету только по ЕГО живым направлениям, и
    # отбор идёт ДО генератора. Это был единственный отсев во всём такте,
    # который не оставлял в отчёте ничего. Замер 28.08: единственное растущее
    # направление (dpo, σ=2.26) живо не было ни в одном из пяти кабинетов —
    # генератор рынка честно отдал ноль, а прочитать это было неоткуда.
    summary = _collect(demand={"dpo": {"regime": demand_mod.REGIME_RISE}})

    reasons = summary["by_source"]["market"]["skipped_by_reason"]
    assert reasons[agent_e0.DEMAND_RISING_UNADDRESSED] == 1


def test_quiet_demand_nobody_serves_is_counted_apart_from_rising(store):
    # Направление без кампаний и без подъёма — не новость, а фон. Слей их в
    # один счётчик — и растущее направление утонуло бы в десятке спокойных.
    summary = _collect(demand={"dpo": {"regime": demand_mod.REGIME_RISE},
                               "mba": {"regime": demand_mod.REGIME_NORMAL}})

    reasons = summary["by_source"]["market"]["skipped_by_reason"]
    assert reasons[agent_e0.DEMAND_RISING_UNADDRESSED] == 1
    assert reasons[agent_e0.DEMAND_UNADDRESSED] == 1


def test_rising_demand_without_address_carries_its_numbers(store):
    # Счётчика причины мало: «одно направление без адреса» не называет ни
    # направления, ни силы подъёма, а решение принимается по ним. Идею с
    # кабинетом такт не заводит — выбор кабинета из данных не выводится.
    summary = _collect(demand={
        "dpo": {"regime": demand_mod.REGIME_RISE, "sigma": 2.26,
                "frequency": 41000, "baseline_median": 30000,
                "last_week": "2026-08-17"},
        "mba": {"regime": demand_mod.REGIME_NORMAL, "sigma": 0.1},
    })

    rising = summary["rising_unaddressed"]
    assert [r["direction"] for r in rising] == ["dpo"]
    assert rising[0]["sigma"] == 2.26
    assert rising[0]["frequency"] == 41000
    assert rising[0]["baseline_median"] == 30000


def test_a_direction_with_live_campaigns_is_not_counted_as_unaddressed(store):
    # Оно доехало до генератора, и его вердикт — уже его дело. Посчитай его и
    # здесь — отчёт врал бы про охват вдвое.
    summary = _collect(demand={"vuz": {"regime": demand_mod.REGIME_RISE}})

    reasons = summary["by_source"].get("market", {}).get("skipped_by_reason", {})
    assert agent_e0.DEMAND_RISING_UNADDRESSED not in reasons


# ---------------------------------- наряд доезжает до очереди билдера (B3)


def _text_campaign(goal=360_811_375, counter=98_627_983):
    """Настройки донора, из которых новая кампания берёт счётчик, цель и гео.

    Формат — витрина edu_campaign_settings, как в бою (agent_e0 →
    db.load_campaign_settings_raw), а не сырой campaigns.get.
    """
    return {"bidModifiers": {"total": 0, "items": []},
            "meta": {"counterIds": [counter]},
            "strategy": {"search": {"goalIds": [goal],
                                    "biddingStrategyType": "AVERAGE_CPA"}},
            "targeting": {"regions": [1, 10_716]}}


def _with_launch(**over):
    """Такт, в котором вынос собирается в наряд билдеру.

    Два донора одного направления с прочитанными настройками — минимум, при
    котором идея выноса перестаёт быть предложением и несёт наряд.
    """
    kwargs = {
        "facts": _run_facts((("111", "vuz"), ("222", "vuz"))),
        "ladder_section": _ladder_section(campaigns=("111", "222")),
        "portfolio_section": _portfolio_section({"acc-1": {"111": "vuz",
                                                           "222": "vuz"}}),
        "sliced_rows": [],
        "query_rows": [
            {"query": "колледж заочно", "campaign_id": "111",
             "clicks": 4_000.0, "conversions": 200.0, "cost": 90_000.0},
            {"query": "поступить в колледж", "campaign_id": "222",
             "clicks": 4_000.0, "conversions": 200.0, "cost": 90_000.0}],
        "expansion": [{"query": "колледж заочно"},
                      {"query": "поступить в колледж"}],
        "settings_by_campaign": {"111": _text_campaign(),
                                 "222": _text_campaign()},
        "login_by_campaign": {"111": "acc-1", "222": "acc-1"},
        "direction_by_campaign": {"111": "vuz", "222": "vuz"},
    }
    kwargs.update(over)
    return _collect(**kwargs)


def test_a_launch_order_reaches_the_builders_queue(store, monkeypatch):
    # Наряд, оставшийся в колонке action, — предложение на экране, а не цикл:
    # билдер читает очередь, а не реестр идей, и без постановки вынос не
    # уезжает никуда.
    sent = []
    monkeypatch.setattr(agent_e0.build_queue, "enqueue", sent.append)

    summary = _with_launch()

    assert summary["by_source"]["consolidate"]["queued"] == 1
    assert sent and sent[0]["kind"] and sent[0]["donor_negatives"]


def test_an_order_the_builder_already_took_is_counted_not_rewritten(
        store, monkeypatch):
    # Тело взятого наряда заморожено: переписать его значило бы собирать
    # движущуюся цель. Отказ обязан быть виден числом — иначе «наряд не
    # обновился» и «наряда не было» выглядят одинаково.
    def frozen(order):
        raise agent_e0.build_queue.OrderFrozen("взят билдером")

    monkeypatch.setattr(agent_e0.build_queue, "enqueue", frozen)
    summary = _with_launch()

    slot = summary["by_source"]["consolidate"]
    assert slot["queued"] == 0
    assert slot["skipped_by_reason"][agent_e0.ORDER_FROZEN_REASON] == 1


def test_an_unavailable_queue_does_not_kill_the_run(store, monkeypatch):
    # Расчётный такт считает деньги. Падать из-за недоступной очереди
    # билдера ему нельзя — отказ становится строкой отчёта.
    def broken(order):
        raise RuntimeError("очередь недоступна")

    monkeypatch.setattr(agent_e0.build_queue, "enqueue", broken)
    summary = _with_launch()

    assert summary["by_source"]["consolidate"]["upserted"] == 1
    assert "consolidate:queue" in summary["failed"]
