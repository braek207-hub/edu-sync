# -*- coding: utf-8 -*-
"""
probe_conversions.py — почему Conversions приходит нулём при переданных целях.

Прогон 32469160289: цели кабинета найдены (4, 2, 5, 2 из стратегий кампаний),
в запрос переданы, а все сегментные срезы всё равно отказали с «нет ни одной
конверсии». Значит вопрос не в наличии целей, и гадать о нём нельзя — здесь
печатается СЫРОЙ ответ Reports API: заголовок TSV и первые строки.

Проверяются варианты, которыми ответ может отличаться:
  · разные модели атрибуции — цель могла достигаться по той, которую мы не
    спрашиваем (агент просит только LSC);
  · срез Device против отчёта по кабинету целиком — есть ли конверсии ВООБЩЕ;
  · запрос без целей — контрольный, показывает базовый состав колонок.

Только чтение. Запуск: python probe_conversions.py
ENV: DIRECT_TOKEN, DIRECT_CLIENTS_JSON
"""

import json
import os
import time
from datetime import date, timedelta

import requests

REPORTS_URL = "https://api.direct.yandex.com/json/v5/reports"

TODAY = date.today()
# Верхняя граница отодвинута: свежие дни у Директа ещё пересчитываются, и
# нули в них ничего не доказывали бы.
DATE_TO = (TODAY - timedelta(days=2)).isoformat()
DATE_FROM = (TODAY - timedelta(days=16)).isoformat()


def _logins():
    raw = (os.environ.get("DIRECT_CLIENTS_JSON") or "").strip()
    out = []
    if raw:
        for item in json.loads(raw):
            login = item.get("login") if isinstance(item, dict) else item
            goals = (item.get("goal_ids") or []) if isinstance(item, dict) else []
            if login:
                out.append((str(login), [str(g) for g in goals]))
    return out


def _post(login, payload):
    headers = {
        "Authorization": f"Bearer {os.environ['DIRECT_TOKEN']}",
        "Client-Login": login,
        "Accept-Language": "ru",
        "processingMode": "auto",
        "returnMoneyInMicros": "false",
        "skipReportHeader": "true",
        "skipReportSummary": "true",
        "Content-Type": "application/json; charset=utf-8",
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for _ in range(60):
        r = requests.post(REPORTS_URL, data=body, headers=headers, timeout=120)
        if r.status_code == 200:
            return r.text
        if r.status_code in (201, 202):
            time.sleep(10)
            continue
        return f"HTTP {r.status_code}: {r.text[:500]}"
    return "TIMEOUT"


def _goals_of_account(login):
    """Цели из стратегий кампаний — тем же путём, каким их берёт агент."""
    from sync.agent.segments import fetch_account_goal_ids

    return [str(g) for g in fetch_account_goal_ids(login)]


def _report(login, fields, goals, attribution, name):
    params = {
        "SelectionCriteria": {"DateFrom": DATE_FROM, "DateTo": DATE_TO},
        "FieldNames": fields,
        "ReportName": name,
        "ReportType": "CUSTOM_REPORT",
        "DateRangeType": "CUSTOM_DATE",
        "Format": "TSV",
        "IncludeVAT": "YES",
        "IncludeDiscount": "NO",
    }
    if goals:
        params["Goals"] = goals
        params["AttributionModels"] = attribution
    return _post(login, {"params": params})


def main() -> int:
    for login, secret_goals in _logins():
        print(f"\n{'=' * 70}\nКАБИНЕТ {login}")
        goals = secret_goals or _goals_of_account(login)
        source = "секрета" if secret_goals else "стратегий кампаний"
        print(f"цели: {goals} (из {source})")
        print(f"период: {DATE_FROM}..{DATE_TO}")

        cases = [
            ("аккаунт целиком, LSC", ["Clicks", "Cost", "Conversions"], goals, ["LSC"]),
            ("аккаунт целиком, LC", ["Clicks", "Cost", "Conversions"], goals, ["LC"]),
            ("аккаунт целиком, FC", ["Clicks", "Cost", "Conversions"], goals, ["FC"]),
            ("срез Device, LSC", ["Device", "Clicks", "Cost", "Conversions"], goals, ["LSC"]),
            ("по одной цели, LSC", ["Clicks", "Cost", "Conversions"], goals[:1], ["LSC"]),
            ("контроль: без целей", ["Clicks", "Cost"], [], []),
        ]
        for title, fields, g, attribution in cases:
            if not g and title != "контроль: без целей":
                print(f"\n--- {title}: пропущено, целей нет")
                continue
            text = _report(login, fields, g, attribution, f"probe-{title}-{DATE_FROM}")
            head = "\n".join(text.splitlines()[:4]) if text else "(пусто)"
            print(f"\n--- {title}\n{head}")
        break  # одного кабинета достаточно, чтобы понять форму ответа
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
