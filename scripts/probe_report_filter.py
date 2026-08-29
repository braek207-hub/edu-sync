# -*- coding: utf-8 -*-
"""
scripts/probe_report_filter.py — проверка формы условия Reports API, которым
кабинетный агрегат отсекает чужие кампании.

Зачем замер. Кампании вне зоны ответственности агента (sync/agent/scope.py)
из покампанийных срезов вырезаются по строкам — там есть CampaignId. У
КАБИНЕТНОГО агрегата (by_campaign=False) в ответе по строке на сегмент и
никакого CampaignId: расход чужих РК иначе оседает в знаменателе
конверсионности сегмента, то есть в кабинетных корректировках ставок.
Отсечь их может только сам Директ — условием запроса
Filter: [{"Field": "CampaignId", "Operator": "NOT_IN", "Values": [...]}].

Цена ошибки в форме запроса высокая: отвергнутое условие роняет НЕ один
отчёт, а весь такт расчёта — кабинетные срезы идут пачкой в одном пуле.
В этом репозитории гадание про формы запросов Директа уже стоило восьми
упавших прогонов (см. segments.fetch_account_goal_ids), поэтому форма
проверяется до боевого прогона, а не на нём.

Что делает. Для каждого кабинета, которому принадлежит хоть одна исключённая
кампания, запрашивает срез device за последние 7 дней ДВАЖДЫ — без условия и
с условием — и печатает обе суммы. Ожидание: второй запрос отработал (не
исключение) и расход в нём меньше. Равенство сумм при непустом списке значит,
что условие принято, но ничего не отсекло, — тоже находка, и её надо читать
глазами, а не считать успехом.

Ничего не пишет: ни в базу, ни в кабинет. Читает direct_stats и Reports API.

Запуск: воркфлоу probe-report-filter (DATABASE_URL, DIRECT_TOKEN,
DIRECT_CLIENTS_JSON живут только в секретах Actions).
"""

import json
from datetime import date, timedelta

from sync.agent import db as agent_db
from sync.agent import segments
from sync.agent_e0 import _direct_clients

WINDOW_DAYS = 7

# Глубина поиска исключённых кампаний по витрине источника. Шире замерного
# окна намеренно: кампания могла не тратить эти семь дней, а условие запроса
# всё равно обязано её пережить — проверяется форма, а не открутка.
LOOKUP_DAYS = 90


def main() -> int:
    date_to = (date.today() - timedelta(days=1)).isoformat()
    date_from = (date.today() - timedelta(days=WINDOW_DAYS)).isoformat()

    excluded = sorted(agent_db.load_excluded_campaign_ids(
        (date.today() - timedelta(days=LOOKUP_DAYS)).isoformat(), date_to))

    out = {"window": [date_from, date_to], "excluded_ids": excluded, "accounts": []}
    if not excluded:
        out["verdict"] = "NOTHING_TO_PROBE"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    for client in _direct_clients():
        login = client["login"]
        # Чья кампания — спрашивается у самого кабинета: Id, спрошенный у
        # чужого логина, отвечает молчанием, и это и есть ответ (тот же приём,
        # что у sync/agent/master.py::probe_accounts). Слать в условие Id
        # чужого кабинета нельзя: тогда отказ Директа означал бы «не твоя
        # кампания», а не «форма запроса негодна», и замер соврал бы.
        try:
            known = segments.fetch_campaigns_by_ids(login, excluded)
        except Exception as exc:  # noqa: BLE001
            out["accounts"].append({"login": login,
                                    "error": f"{type(exc).__name__}: {exc}"[:300]})
            continue
        mine = sorted(known)
        if not mine:
            continue

        row = {"login": login, "campaigns": mine}
        try:
            plain, _ = segments.fetch_segment_report(
                login, "device", date_from, date_to)
            row["rows_plain"] = len(plain)
            row["cost_plain"] = round(sum(float(r["cost"]) for r in plain), 2)
        except Exception as exc:  # noqa: BLE001
            row["error_plain"] = f"{type(exc).__name__}: {exc}"[:300]
        try:
            filtered, _ = segments.fetch_segment_report(
                login, "device", date_from, date_to, excluded_campaign_ids=mine)
            row["rows_filtered"] = len(filtered)
            row["cost_filtered"] = round(sum(float(r["cost"]) for r in filtered), 2)
        except Exception as exc:  # noqa: BLE001
            row["error_filtered"] = f"{type(exc).__name__}: {exc}"[:300]

        row["ok"] = ("cost_filtered" in row and "cost_plain" in row
                     and row["cost_filtered"] < row["cost_plain"])
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
