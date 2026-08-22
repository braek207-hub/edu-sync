# -*- coding: utf-8 -*-
"""probe_counter_to_account.py — какому кабинету принадлежит каждый счётчик.

Почасовое расписание сейчас считается по ТРЁМ счётчикам Метрики сразу
(metrika.merge_hourly), а применяется к каждой кампании каждого кабинета.
Проба probe_schedule_numerator показала, что счётчики о ночи не согласны:
98627983 поднимает часы 02-05 до 130, 96526110 те же часы опускает до 90.
Слитый профиль решает спор в пользу счётчика с бОльшим объёмом, и кампаниям
меньшего достаётся расписание чужого сайта.

Вопрос, который закрывает эта проба: счётчик соответствует кабинету
один-к-одному или нет. Если да — профиль надо считать по счётчику кабинета,
и ключ хранения (логин) менять не придётся.

Метод: у каждого счётчика спрашиваем ИМЕНА кампаний Директа, которые он
видел, у каждого кабинета — имена его кампаний, и печатаем пересечение.

Только чтение. ENV: YM_TOKEN, DIRECT_TOKEN, DIRECT_CLIENTS_JSON.
"""

import json
import os
from datetime import date, timedelta

from sync.agent.metrika import EDU_COUNTERS, fetch_campaign_behavior
from sync.agent.writer.client import WriteClient


def account_campaign_names(login: str):
    client = WriteClient(login, sandbox=False, dry_run=True)
    out = {}
    limit, offset = 10_000, 0
    while True:
        resp = client.get("campaigns", {
            "SelectionCriteria": {},
            "FieldNames": ["Id", "Name", "Status"],
            "Page": {"Limit": limit, "Offset": offset},
        })
        items = (resp.get("result") or {}).get("Campaigns") or []
        for c in items:
            out[str(c["Name"]).strip()] = str(c["Id"])
        if len(items) < limit:
            break
        offset += limit
    return out


def main() -> int:
    d2 = (date.today() - timedelta(days=1)).isoformat()
    d1 = (date.today() - timedelta(days=30)).isoformat()
    clients = json.loads(os.environ["DIRECT_CLIENTS_JSON"])

    by_account = {}
    for c in clients:
        login = c["login"]
        try:
            by_account[login] = account_campaign_names(login)
        except Exception as exc:
            by_account[login] = {}
            print("кабинет %s не отдал кампании: %s: %s" % (login, type(exc).__name__, exc))

    by_counter = {}
    for counter in EDU_COUNTERS:
        try:
            rows = fetch_campaign_behavior(counter, d1, d2)
            by_counter[counter] = {str(r["campaign_name"]).strip() for r in rows}
        except Exception as exc:
            by_counter[counter] = set()
            print("счётчик %s не отдал кампании: %s: %s" % (counter, type(exc).__name__, exc))

    # Логины в выводе не печатаем целиком: они часть секрета DIRECT_CLIENTS_JSON
    # и GitHub всё равно их замаскирует. Порядковый номер и размер достаточны.
    print("\nматрица «счётчик × кабинет» — сколько имён кампаний совпало:\n")
    header = "счётчик".ljust(12) + "".join(
        ("каб%d(%d)" % (i + 1, len(v))).rjust(14) for i, v in enumerate(by_account.values()))
    print(header)
    for counter, names in by_counter.items():
        row = str(counter).ljust(12)
        for camps in by_account.values():
            hit = len(names & set(camps))
            share = (hit / len(names) * 100) if names else 0
            row += ("%d/%d (%.0f%%)" % (hit, len(names), share)).rjust(14)
        print(row)

    print("\nразмер множеств: " + ", ".join(
        "счётчик %s: %d имён" % (c, len(v)) for c, v in by_counter.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
