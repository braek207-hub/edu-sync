# -*- coding: utf-8 -*-
"""Зонд П2-c: SQL-эндпоинт Triple Whale — есть ли расход по странам и кампаниям.

История вопроса: summary-page отдаёт расход одной цифрой на магазин, и 13 форм
параметра фильтра по стране он молча игнорирует. Но в UI фильтр по стране есть,
значит данные существуют. Документация (triplewhale.readme.io/llms.txt) показала
эндпоинт «Execute Custom SQL Query» — прямой SQL по хранилищу Orcabase.

POST https://api.triplewhale.com/api/v2/orcabase/api/sql
Authorization: Bearer <ключ>   ← НЕ x-api-key, в отличие от остальных эндпоинтов

Только SELECT. Запуск: python -m scripts.probe_tw_sql
"""
import io
import json
import os
import sys
import time

import requests
from dotenv import load_dotenv

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
load_dotenv()

URL = "https://api.triplewhale.com/api/v2/orcabase/api/sql"
PERIOD = {"start": "2026-06-01", "end": "2026-06-30"}


def run(query: str, label: str, quiet: bool = False):
    body = {"query": query, "period": PERIOD}
    for attempt in range(1, 4):
        try:
            r = requests.post(
                URL,
                headers={"Authorization": f"Bearer {os.environ['GCC_TRIPLEWHALE_API_KEY']}",
                         "Content-Type": "application/json"},
                json=body, timeout=180,
            )
        except Exception as exc:
            time.sleep(5 * attempt)
            if attempt == 3:
                print(f"[{label}] сеть: {type(exc).__name__}")
                return None
            continue
        break

    if r.status_code != 200:
        print(f"[{label}] HTTP {r.status_code}: {(r.text or '')[:300]}")
        return None
    try:
        data = r.json()
    except Exception:
        print(f"[{label}] 200, но не JSON: {(r.text or '')[:200]}")
        return None
    if not quiet:
        preview = json.dumps(data, ensure_ascii=False)[:700]
        print(f"[{label}] OK: {preview}")
    return data


def main():
    print("=== 1. Доступ ===")
    if run("SELECT 1 AS ok", "смоук") is None:
        print("SQL-эндпоинт недоступен этим ключом — дальше смысла нет")
        return

    print("\n=== 2. Какие таблицы видны ===")
    for q, label in [
        ("SELECT table_name FROM INFORMATION_SCHEMA.TABLES LIMIT 200", "information_schema"),
        ("SHOW TABLES", "show tables"),
    ]:
        if run(q, label):
            break


if __name__ == "__main__":
    main()
