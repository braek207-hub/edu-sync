# -*- coding: utf-8 -*-
"""
scripts/probe_report_filter.py — проверка формы условия Reports API, которым
кабинетный агрегат отсекает чужие кампании.

Зачем замер. Кампании вне зоны ответственности агента (sync/agent/scope.py)
из покампанийных срезов вырезаются по строкам — там есть CampaignId. У
КАБИНЕТНОГО агрегата (by_campaign=False) в ответе по строке на сегмент и
никакого CampaignId: расход чужих РК иначе оседает в знаменателе
конверсионности сегмента, то есть в кабинетных корректировках ставок.
Отсечь их может только сам Директ — условием запроса.

Первая редакция замера спрашивала NOT_IN с Id чужих кампаний. Директ ответил
ошибкой 4001 (run 33274646184): «для поля CampaignId, указанного в
Filter.Field, допустимы только операторы EQUALS, IN» для типа отчёта
CUSTOM_REPORT. Оператора NOT_IN не существует, и «всё кроме этих семи»
выражается единственным способом — перечислением всего остального. Отсюда
нынешняя форма:
Filter: [{"Field": "CampaignId", "Operator": "IN", "Values": [свои Id]}].

Цена ошибки в форме запроса высокая: отвергнутое условие роняет НЕ один
отчёт, а весь такт расчёта — кабинетные срезы идут пачкой в одном пуле.
В этом репозитории гадание про формы запросов Директа уже стоило восьми
упавших прогонов (см. segments.fetch_account_goal_ids), поэтому форма
проверяется до боевого прогона, а не на нём.

Что делает. Для каждого кабинета, которому принадлежит хоть одна исключённая
кампания, запрашивает срез device за последние 7 дней ДВАЖДЫ — без условия и
с условием — и печатает обе суммы. Спрашивается ровно тот список, что уйдёт
в бою: свои кампании ЭТОГО кабинета, как их отдаёт campaigns.get.

Ожидание: второй запрос отработал (не исключение), строки в нём есть и
расход меньше. Пустой ответ при принятом условии — отказ, а не успех: значит
перечисление Id отсекло всё подряд. Равенство сумм значит, что условие
принято и ничего не отсекло, — тоже находка, читать глазами.

Отдельно замер показывает цену формы: сколько кампаний уходит в Values
(предел длины Values в документации не назван) и на сколько агрегат теряет
кампании Мастера, которых campaigns.get не отдаёт вовсе (sync/agent/master.py)
и которые условие «IN свои» выбрасывает заодно с чужими.

Ничего не пишет: ни в базу, ни в кабинет. Читает только Direct API.

Запуск: воркфлоу probe-report-filter (DIRECT_TOKEN, DIRECT_CLIENTS_JSON
живут только в секретах Actions).
"""

import json
from datetime import date, timedelta

from sync.agent import segments
from sync.agent_e0 import _direct_clients

WINDOW_DAYS = 7


def main() -> int:
    date_to = (date.today() - timedelta(days=1)).isoformat()
    date_from = (date.today() - timedelta(days=WINDOW_DAYS)).isoformat()

    out = {"window": [date_from, date_to], "accounts": []}

    for client in _direct_clients():
        login = client["login"]
        # Состав кабинета спрашивается у самого кабинета — тем же вызовом, что
        # и в бою. Чужие кампании тут не «список на исключение», а признак:
        # сужается только кабинет, которому есть что исключать.
        try:
            own, foreign = segments.fetch_campaign_ids_by_scope(login)
        except Exception as exc:  # noqa: BLE001
            out["accounts"].append({"login": login,
                                    "error": f"{type(exc).__name__}: {exc}"[:300]})
            continue
        if not foreign:
            continue

        row = {"login": login, "own_campaigns": len(own),
               "foreign_campaigns": sorted(foreign)}
        try:
            plain, _ = segments.fetch_segment_report(
                login, "device", date_from, date_to)
            row["rows_plain"] = len(plain)
            row["cost_plain"] = round(sum(float(r["cost"]) for r in plain), 2)
        except Exception as exc:  # noqa: BLE001
            row["error_plain"] = f"{type(exc).__name__}: {exc}"[:300]
        try:
            filtered, _ = segments.fetch_segment_report(
                login, "device", date_from, date_to, own_campaign_ids=own)
            row["rows_filtered"] = len(filtered)
            row["cost_filtered"] = round(sum(float(r["cost"]) for r in filtered), 2)
        except Exception as exc:  # noqa: BLE001
            row["error_filtered"] = f"{type(exc).__name__}: {exc}"[:300]

        row["ok"] = (row.get("rows_filtered", 0) > 0
                     and "cost_plain" in row
                     and row["cost_filtered"] < row["cost_plain"])
        # Цена формы: этого расхода агрегат лишился сверх чужих кампаний —
        # кампании Мастера в Values не попадают, потому что campaigns.get их
        # не отдаёт. Число читать глазами: оно не делает замер красным, но
        # именно оно решает, стоит ли сужение своей цены.
        if "cost_plain" in row and "cost_filtered" in row:
            row["cost_dropped"] = round(row["cost_plain"] - row["cost_filtered"], 2)
        out["accounts"].append(row)

    probed = [a for a in out["accounts"] if "ok" in a]
    # Зелёным считается только полный успех по всем опрошенным кабинетам.
    # Пустой список опрошенных — не успех: значит спросить было не у кого, и
    # форма запроса так и осталась непроверенной.
    out["verdict"] = ("GREEN" if probed and all(a["ok"] for a in probed)
                      else "CHECK_BY_HAND")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
