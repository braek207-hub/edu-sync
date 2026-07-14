"""Валидация live-контрактов Wordstat + Вебмастер (Task 1).

Захватывает реальные ответы нашим токеном → фикстуры для TDD Task 3/4.
Ничего не пишет в БД. Запуск: python scripts/probe_brand_apis.py

Подтверждённые контракты (заполнить по факту прогона):
  Wordstat: POST https://api.wordstat.yandex.net/v1/dynamics
    body {phrase, period:"weekly", fromDate, toDate, regions:[225]}
    -> {"dynamics":[{"date","count","share"}]}
  Вебмастер per-query: <path>  (query-analytics/list POST | search-queries/popular GET)
"""
import json
import os
import pathlib
import urllib.error
import urllib.request

from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ["WORDSTAT_WEBMASTER_TOKEN"]
FIX = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures"
FIX.mkdir(parents=True, exist_ok=True)


def post(url, body, headers):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def get(url, headers):
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


# 1. Wordstat Dynamics (weekly, Russia=225)
try:
    ws = post(
        "https://api.wordstat.yandex.net/v1/dynamics",
        {"phrase": "лайм одежда", "period": "weekly",
         "fromDate": "2025-01-01", "toDate": "2025-03-31", "regions": [225]},
        {"Authorization": f"Bearer {TOKEN}"})
    (FIX / "wordstat_dynamics.json").write_text(
        json.dumps(ws, ensure_ascii=False, indent=2), encoding="utf-8")
    sample = (ws.get("dynamics") or [None])[0]
    print("WORDSTAT OK keys:", list(ws.keys()), "| n:", len(ws.get("dynamics", [])), "| sample:", sample)
except urllib.error.HTTPError as e:
    print("WORDSTAT HTTP", e.code, e.read().decode("utf-8", "replace")[:500])
except Exception as e:
    print("WORDSTAT ERR", type(e).__name__, str(e)[:300])


# 2. Вебмастер per-query. Кандидат A: query-analytics/list (POST).
UID = "1343007866"
HOST = "https:limestore.com:443"
wm_done = False
try:
    data = post(
        f"https://api.webmaster.yandex.net/v4/user/{UID}/hosts/{HOST}/query-analytics/list",
        {"offset": 0, "limit": 100, "device_type_indicator": "ALL",
         "text_indicator": "QUERY", "region_ids": []},
        {"Authorization": f"OAuth {TOKEN}"})
    (FIX / "webmaster_query_analytics.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print("WEBMASTER query-analytics OK keys:", list(data.keys())[:8])
    wm_done = True
except urllib.error.HTTPError as e:
    print("WEBMASTER query-analytics HTTP", e.code, e.read().decode("utf-8", "replace")[:400])
except Exception as e:
    print("WEBMASTER query-analytics ERR", type(e).__name__, str(e)[:300])

# Кандидат B (fallback): search-queries/popular (GET) — TOP по кликам.
if not wm_done:
    try:
        url = (f"https://api.webmaster.yandex.net/v4/user/{UID}/hosts/{HOST}"
               f"/search-queries/popular?order_by=TOTAL_CLICKS&query_indicator=TOTAL_CLICKS"
               f"&query_indicator=TOTAL_SHOWS&limit=100")
        data = get(url, {"Authorization": f"OAuth {TOKEN}"})
        (FIX / "webmaster_popular.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print("WEBMASTER popular OK keys:", list(data.keys())[:8])
    except urllib.error.HTTPError as e:
        print("WEBMASTER popular HTTP", e.code, e.read().decode("utf-8", "replace")[:400])
    except Exception as e:
        print("WEBMASTER popular ERR", type(e).__name__, str(e)[:300])
