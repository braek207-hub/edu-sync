# -*- coding: utf-8 -*-
"""Перезапись плана одного месяца в листе plan_monthly (книга EDU).

Источник истины плана — Google Sheet: sync/plan.py каждую ночь делает
TRUNCATE monthly_plans и заливает лист заново, поэтому запись напрямую в
базу не живёт дольше суток. Меняем лист, затем (если задан DATABASE_URL)
сразу прогоняем штатный sync_plan_monthly — база обновляется тем же кодом,
что и ночью, без второй реализации.

Правка минимально-инвазивная: строки целевого месяца очищаются по одной
(values.clear), новые дописываются в конец (values.append). Остальные
строки листа не переписываются — их форматирование и содержимое не
затрагиваются. Пустые строки синк пропускает как «нет month».

Запуск (workflow plan-set-month.yml):
    python scripts/plan_set_month.py --month 2026-09 --rows rows.json

rows.json — список объектов с ключами
    project, direction, budget, leads, connections, deals, payments, revenue, notes
в терминах листа: budget = plan_budget_vat (с НДС, как есть), payments = plan_sales.

ENV: GOOGLE_SHEETS_ID (или SHEET_ID_EDU), GOOGLE_SERVICE_ACCOUNT — сервис-аккаунт
EDU; скрипту нужен write-scope, поэтому клиент строится здесь, а не берётся из
sync/sheets.py (тот намеренно readonly).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from sync.plan import PLAN_SHEET, STANDARD_PLAN_HEADERS, normalize_month_key

WRITE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _service():
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        creds = Credentials.from_service_account_file(
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"], scopes=WRITE_SCOPES
        )
    else:
        creds = Credentials.from_service_account_info(
            json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT"]), scopes=WRITE_SCOPES
        )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", required=True, help="YYYY-MM")
    ap.add_argument("--rows", required=True, help="путь к JSON со строками месяца")
    args = ap.parse_args()

    rows = json.loads(Path(args.rows).read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        raise SystemExit("rows: нужен непустой JSON-список")
    for r in rows:
        missing = {"project", "direction", "budget", "leads"} - set(r)
        if missing:
            raise SystemExit(f"rows: не хватает полей {missing} в {r}")

    sheet_id = os.environ.get("GOOGLE_SHEETS_ID") or os.environ["SHEET_ID_EDU"]
    # sync_plan_monthly ниже читает строго GOOGLE_SHEETS_ID
    os.environ.setdefault("GOOGLE_SHEETS_ID", sheet_id)
    svc = _service()

    values = (
        svc.spreadsheets()
        .values()
        .get(
            spreadsheetId=sheet_id,
            range=PLAN_SHEET,
            valueRenderOption="UNFORMATTED_VALUE",
        )
        .execute(num_retries=4)
        .get("values", [])
    )
    if not values:
        raise SystemExit(f"Лист {PLAN_SHEET} пуст — нечего править")

    # Строки целевого месяца (1-базные номера строк листа; заголовок — строка 1).
    target = [
        i + 2
        for i, r in enumerate(values[1:])
        if normalize_month_key(r[0] if r else "") == args.month
    ]
    print(f"{PLAN_SHEET}: строк всего {len(values)}, месяц {args.month}: {len(target)} строк {target}")

    for rownum in target:
        svc.spreadsheets().values().clear(
            spreadsheetId=sheet_id,
            range=f"{PLAN_SHEET}!A{rownum}:J{rownum}",
            body={},
        ).execute(num_retries=4)

    payload = [
        [
            f"{args.month}-01",
            r["project"],
            r["direction"],
            r["budget"],
            r["leads"],
            r.get("connections", 0),
            r.get("deals", 0),
            r.get("payments", 0),
            r.get("revenue", 0),
            r.get("notes", ""),
        ]
        for r in rows
    ]
    svc.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=PLAN_SHEET,
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": payload},
    ).execute(num_retries=4)
    print(f"Дописано {len(payload)} строк месяца {args.month}")

    # Проверка у получателя: перечитать лист и убедиться, что месяц собрался.
    check = (
        svc.spreadsheets()
        .values()
        .get(
            spreadsheetId=sheet_id,
            range=PLAN_SHEET,
            valueRenderOption="UNFORMATTED_VALUE",
        )
        .execute(num_retries=4)
        .get("values", [])
    )
    got = [r for r in check[1:] if normalize_month_key(r[0] if r else "") == args.month]
    print(f"Проверка: в листе {len(got)} строк месяца {args.month}: {got}")
    if len(got) != len(rows):
        raise SystemExit("Число строк после записи не совпало с ожидаемым")

    if os.environ.get("DATABASE_URL"):
        from sync.plan import sync_plan_monthly

        n = sync_plan_monthly()
        print(f"База обновлена штатным синком: {n} строк")
    else:
        print("DATABASE_URL не задан — база обновится ночным синком")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
