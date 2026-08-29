# -*- coding: utf-8 -*-
"""Переоценка журнала под дельта-модель: цена действия — его дельта, не объект."""

from datetime import date

from sync.agent.writer import reprice

# Расчёт Э0 того дня, из которого восстанавливается доля сегмента. Блока
# exposure в журнале нет, поэтому долю негде взять, кроме как пересчитать
# её тем же plan.segment_shares по строкам того же дня.
#
# 22.08 сегмент AGE_55 брал 4 % кликов возрастного среза, 26.08 — 31 %:
# именно так и выглядит след применённой корректировки, поэтому доля обязана
# читаться на дату действия, а не сегодняшняя.
_COMPUTED_BY_DAY = {
    "2026-08-22": [
        {"calc_date": date(2026, 8, 22), "setting_kind": "bid_modifier:age",
         "setting_key": "AGE_55", "value": -43.0, "support_n": 4},
        {"calc_date": date(2026, 8, 22), "setting_kind": "bid_modifier:age",
         "setting_key": "AGE_25_34", "value": 12.0, "support_n": 96},
    ],
    "2026-08-26": [
        {"calc_date": date(2026, 8, 26), "setting_kind": "bid_modifier:age",
         "setting_key": "AGE_55", "value": -20.0, "support_n": 31},
        {"calc_date": date(2026, 8, 26), "setting_kind": "bid_modifier:age",
         "setting_key": "AGE_25_34", "value": 9.0, "support_n": 69},
    ],
}


def _computed_on(day):
    """Строки edu_agent_computed_settings, посчитанные в этот день."""
    return [dict(row) for row in _COMPUTED_BY_DAY[day]]


ACTION = {
    "action_id": "a1", "action_kind": "bidmodifier.add",
    "object_level": "campaign", "object_id": "114057545",
    "risk_rub": 38876.0, "risk_basis": None,
    "direct_type": "AGE", "setting_key": "AGE_55",
    "payload": {"BidModifier": -43},
}
SHARE = 0.04
DAILY = {"114057545": 5553.71}
# Второй аргумент plan — расчёт того дня, а не готовый словарь долей: долю
# определяет share_for и только он.
SHARES = _computed_on("2026-08-22")


def test_pre_delta_row_is_repriced_to_its_delta():
    action = {"action_id": "a1", "action_kind": "bidmodifier.add",
              "object_level": "campaign", "object_id": "114057545",
              "risk_rub": 38876.0, "risk_basis": None,
              "direct_type": "AGE", "setting_key": "AGE_55",
              "payload": {"BidModifier": -43}}
    share = reprice.share_for(action, _computed_on("2026-08-22"))   # 0.04
    new = reprice.recompute(action, share, {"114057545": 5553.71})
    assert new is not None
    assert new < 5000.0, f"дельта сегмента 4% не может стоить {new}"


def test_share_comes_from_the_calc_of_the_action_date():
    action = {"direct_type": "AGE", "setting_key": "AGE_55",
              "applied_at": "2026-08-22T08:40:00+00:00"}
    assert reprice.share_for(action, _computed_on("2026-08-22")) == 0.04
    assert reprice.share_for(action, _computed_on("2026-08-26")) == 0.31


def test_repricing_twice_changes_nothing():
    first = reprice.recompute(ACTION, SHARE, DAILY)
    assert reprice.recompute({**ACTION, "risk_rub": first}, SHARE, DAILY) == first


def test_row_without_a_share_is_not_repriced_and_is_named():
    action = {"action_id": "a2", "action_kind": "schedule.set",
              "object_id": "999", "risk_rub": 12000.0,
              "direct_type": None, "setting_key": None, "payload": {}}
    repriced, untouched = reprice.plan([action], {}, {"999": 1000.0})
    assert repriced == []
    assert untouched[0]["reason"], "строка исчезла из отчёта молча"


def test_row_priced_by_todays_model_is_skipped():
    # Свежесть цены — совпадение ОСНОВАНИЯ с сегодняшним, а не непустая
    # колонка: основание перечисляет множители модели словами и служит её
    # отпечатком.
    basis = reprice.basis_for(ACTION, SHARE, DAILY)
    repriced, untouched = reprice.plan([{**ACTION, "risk_basis": basis}],
                                       SHARES, DAILY)
    assert repriced == []
    assert untouched[0]["reason"] == reprice.REASON_ALREADY_DELTA


def test_row_priced_by_the_previous_model_is_repriced():
    # Прежнее правило «risk_basis заполнен — не трогаем» сработало ровно один
    # раз, на переходе к дельта-модели. Поправка 29.08.2026 (ошибка раскладки)
    # законсервировала бы по нему весь журнал в старых ценах — ровно то, ради
    # чего модуль и написан.
    action = {**ACTION, "risk_basis": "сегмент 4.0% объекта × сдвиг 86%"}
    repriced, _ = reprice.plan([action], SHARES, DAILY)
    assert [r["new"] for r in repriced] == [reprice.recompute(ACTION, SHARE, DAILY)]


# --------------------------------------------------------------- края


def test_new_price_is_the_delta_arithmetic_not_just_something_smaller():
    # «Меньше 5000» проходит и у случайно заниженного числа. Цена обязана быть
    # ровно долей 4 % × сдвигом 86 % × ошибкой раскладки 43 % от расхода за
    # 7 дней замера: переложенные деньги не сгорают, теряется разница
    # эффективностей, которую заявляет сам коэффициент.
    new = reprice.recompute(ACTION, SHARE, DAILY)
    assert new == round(5553.71 * 0.04 * 0.86 * 0.43 * 7, 2)


def test_unknown_daily_cost_leaves_the_row_alone():
    # Справочник расхода пуст — object_daily_cost честно отдаёт +inf. Записать
    # такую цену в журнал значит выставить бесконечный счёт недельному
    # бюджету; строка обязана остаться в старой цене и попасть в отчёт.
    assert reprice.recompute(ACTION, SHARE, {}) is None
    repriced, untouched = reprice.plan([ACTION], SHARES, {})
    assert repriced == []
    assert "расход" in untouched[0]["reason"]


def test_median_fallback_is_not_used_to_invent_a_price():
    # Кампании действия в справочнике нет, но другие кампании есть: risk.py
    # берёт для неё медиану. Переоценке этого достаточно — она консервативна
    # и это та же оценка, по которой строку оценил бы сегодняшний прогон.
    new = reprice.recompute(ACTION, SHARE, {"111": 1000.0, "222": 3000.0})
    assert new == round(2000.0 * 0.04 * 0.86 * 0.43 * 7, 2)


def test_share_is_none_when_the_calc_of_that_day_has_no_such_segment():
    # Расчёт того дня не сохранился или сегмента в нём нет — доли неоткуда
    # взять. Подстановка соседней доли была бы догадкой.
    action = {"direct_type": "AGE", "setting_key": "AGE_18_24"}
    assert reprice.share_for(action, _computed_on("2026-08-22")) is None
    assert reprice.share_for(action, []) is None


def test_share_matches_by_the_real_direct_type_too():
    # В журнале лежит тип корректировки Директа, а в расчёте — вид настройки:
    # это разные алфавиты. Соединение обязано работать по обеим формам, иначе
    # переоценивались бы только строки одного из двух написаний.
    action = {"direct_type": "DEMOGRAPHICS_ADJUSTMENT", "setting_key": "age_55"}
    assert reprice.share_for(action, _computed_on("2026-08-22")) == 0.04


def test_row_whose_payload_has_no_bid_modifier_is_named_not_guessed():
    # Доля нашлась, но действие — не корректировка ставки: силы сдвига в нём
    # нет, и посчитать дельту не из чего.
    action = {"action_id": "a3", "action_kind": "campaign.suspend",
              "object_id": "114057545", "risk_rub": 9000.0,
              "direct_type": "AGE", "setting_key": "AGE_55",
              "payload": {"State": "SUSPENDED"}}
    repriced, untouched = reprice.plan([action], SHARES, DAILY)
    assert repriced == []
    assert untouched[0]["action_id"] == "a3"
    assert untouched[0]["reason"]


def test_plan_reports_what_changed_and_why():
    repriced, untouched = reprice.plan([ACTION], SHARES, DAILY)
    assert untouched == []
    assert repriced[0]["action_id"] == "a1"
    assert repriced[0]["old"] == 38876.0
    assert repriced[0]["new"] < 5000.0
    # Основание — не украшение отчёта: без него число в журнале не проверяемо.
    assert "4.0%" in repriced[0]["basis"]


def test_unchanged_price_is_reported_instead_of_a_silent_no_op_update():
    # Пересчёт дал ту же цену — обновлять нечего, но и промолчать нельзя:
    # «проверили, совпало» обязано отличаться от «строку не смотрели».
    priced = reprice.recompute(ACTION, SHARE, DAILY)
    repriced, untouched = reprice.plan(
        [{**ACTION, "risk_rub": priced}], SHARES, DAILY)
    assert repriced == []
    assert "не изменилась" in untouched[0]["reason"]


# --- скрипт переоценки: какие строки он вообще достаёт ---------------------

def _reprice_script():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "reprice_actions.py"
    spec = importlib.util.spec_from_file_location("reprice_actions", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_reprices_every_status_that_holds_the_weekly_budget():
    # Фильтр по LIVE_STATUSES был бы уже. Откатанная строка платит риском
    # наравне с применённой (writer/db.spent_risk: экспозиция уже случилась,
    # откат не возвращает потраченного) — оставь её в старой цене, и она
    # продолжит держать недельный лимит, а переоценка её не достанет.
    from sync.agent.writer import db as writer_db

    sql = _reprice_script().SELECT_CHARGED_SQL

    for status in writer_db.RISK_CHARGED_STATUSES:
        assert f"'{status}'" in sql, f"статус {status} держит бюджет, но не переоценивается"
    assert "'rolled_back'" in sql


def test_script_writes_the_basis_along_with_the_new_price():
    # Пустой risk_basis — признак цены до дельта-модели, по нему же
    # reprice.plan пропускает уже пересчитанные строки. Записать цену без
    # основания значило бы оставить строку неотличимой от непереоценённой.
    sql = _reprice_script().UPDATE_SQL

    assert "risk_rub" in sql and "risk_basis" in sql
