# -*- coding: utf-8 -*-
"""Разведка AppMetrica app 6299245 (LIME International, GCC) через Logs API.

Проверяет ДО стройки Фазы 2: (1) токен видит приложение; (2) country_iso_code валиден;
(3) есть sessions/installs/purchases; (4) распределение по странам и publisher.
Ничего не пишет. Запуск: LIME_GCC_MODE=app-probe (см. lime_gcc_report.main).

ENV: APPMETRICA_TOKEN, GCC_APP_ID (default 6299245), APPMETRICA_EVENT_NAME (default purchase).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone

from sync.appmetrica_logs import fetch_installations, fetch_purchase_events, fetch_sessions

_STAT_URL = "https://api.appmetrica.yandex.ru/stat/v1/data"


def _reporting_probe(token: str, frm: str, to: str) -> None:
    """Проверить Reporting API: sessions по дате×стране×источнику (агрегат, без джойна).

    Пробуем несколько имён метрик/измерений — API в 400 часто перечисляет валидные.
    """
    trials = [
        ("devices × date,country (BASE DAU)",
         {"metrics": "ym:s:devices", "dimensions": "ym:s:date,ym:s:regionCountry"}),
        # СПЛИТ DAU по атрибуции установки через ФИЛТР (разные префиксы допустимы в filters)
        ("DAU ORGANIC (filter i:publisher==Органика)",
         {"metrics": "ym:s:devices", "dimensions": "ym:s:date,ym:s:regionCountry",
          "filters": "ym:i:publisher=='Органика'"}),
        ("DAU PAID (filter i:publisher!=Органика)",
         {"metrics": "ym:s:devices", "dimensions": "ym:s:date,ym:s:regionCountry",
          "filters": "ym:i:publisher!='Органика'"}),
        # значения publisher (какие платные партнёры есть)
        ("i:installDevices × date,country,publisher",
         {"metrics": "ym:i:installDevices",
          "dimensions": "ym:i:date,ym:i:regionCountry,ym:i:publisher", "limit": "40"}),
    ]
    for label, extra in trials:
        params = {"id": APP_ID, "date1": frm, "date2": to, "accuracy": "1", "limit": "20", **extra}
        url = f"{_STAT_URL}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Authorization": f"OAuth {token}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read().decode("utf-8"))
            rows = data.get("data", [])
            print(f"[reporting {label}] OK строк={len(rows)}; totals={data.get('totals')}")
            for row in rows[:6]:
                dims = [d.get("name") or d.get("id") for d in row.get("dimensions", [])]
                print(f"    {dims} → {row.get('metrics')}")
        except urllib.error.HTTPError as e:
            print(f"[reporting {label}] HTTP {e.code}: {e.read().decode('utf-8')[:300]}")
        except Exception as e:  # noqa: BLE001
            print(f"[reporting {label}] ОШИБКА {type(e).__name__}: {e}")

APP_ID = os.environ.get("GCC_APP_ID") or "6299245"
EVENT = os.environ.get("APPMETRICA_EVENT_NAME") or "purchase"


def _win() -> tuple[str, str]:
    """Двухдневное зрелое окно (позавчера-2 … позавчера-1), данные точно готовы."""
    today = (datetime.now(timezone.utc) + timedelta(hours=3)).date()
    to = today - timedelta(days=3)
    frm = to - timedelta(days=1)
    return frm.isoformat(), to.isoformat()


def _top(counter: Counter, n: int = 12) -> str:
    return ", ".join(f"{k or '∅'}={v}" for k, v in counter.most_common(n))


def main() -> None:
    token = os.environ["APPMETRICA_TOKEN"]
    frm, to = _win()
    print(f"app={APP_ID}, окно {frm}..{to}, event={EVENT}")

    try:
        inst = fetch_installations(APP_ID, token, frm, to, country=True)
        print(f"\n[installations] всего {len(inst)}")
        if inst:
            print("  поля:", sorted(inst[0].keys()))
            print("  страны:", _top(Counter(r.get("country_iso_code") for r in inst)))
            print("  publisher:", _top(Counter(r.get("publisher_name") or "∅(organic)" for r in inst)))
    except Exception as e:  # noqa: BLE001
        print(f"[installations] ОШИБКА {type(e).__name__}: {e}")

    try:
        sess = fetch_sessions(APP_ID, token, frm, to, country=True)
        print(f"\n[sessions_starts] всего {len(sess)}")
        if sess:
            print("  поля:", sorted(sess[0].keys()))
            print("  страны:", _top(Counter(r.get("country_iso_code") for r in sess)))
    except Exception as e:  # noqa: BLE001
        print(f"[sessions_starts] ОШИБКА {type(e).__name__}: {e}")

    try:
        ev = fetch_purchase_events(APP_ID, token, frm, to, EVENT, country=True)
        print(f"\n[events {EVENT}] всего {len(ev)}")
        if ev:
            print("  поля:", sorted(ev[0].keys()))
            print("  страны:", _top(Counter(r.get("country_iso_code") for r in ev)))
            src = Counter()
            for r in ev:
                try:
                    src[(json.loads(r.get("event_json") or "{}").get("source") or "∅")] += 1
                except Exception:  # noqa: BLE001
                    src["parse_err"] += 1
            print("  event_json.source:", _top(src))
            ej = ev[0].get("event_json")
            try:
                print("  event_json пример:", {k: v for k, v in json.loads(ej).items()
                                               if k in ("value", "currency", "source", "referrer", "transaction_id")})
            except Exception:  # noqa: BLE001
                print("  event_json (raw):", str(ej)[:200])
    except Exception as e:  # noqa: BLE001
        print(f"[events] ОШИБКА {type(e).__name__}: {e}")

    # deeplinks / ре-энгейджмент: есть ли партнёр+устройство+время для last-touch.
    from sync.appmetrica_logs import fetch_export
    for fields in ("appmetrica_device_id,deeplink_datetime,publisher_name,tracker_name,city,country_iso_code",
                   "appmetrica_device_id,event_datetime,publisher_name,tracker_name",
                   "appmetrica_device_id,session_start_datetime,publisher_name"):
        try:
            dl = fetch_export("deeplinks", APP_ID, token, frm, to, fields)
            print(f"\n[deeplinks fields='{fields[:40]}...'] всего {len(dl)}")
            if dl:
                print("  поля:", sorted(dl[0].keys()))
                print("  publisher:", _top(Counter(r.get("publisher_name") or "∅" for r in dl)))
                print("  страны:", _top(Counter(r.get("country_iso_code") for r in dl)))
            break
        except Exception as e:  # noqa: BLE001
            print(f"[deeplinks fields='{fields[:40]}...'] {type(e).__name__}: {str(e)[:160]}")

    print()
    _reporting_probe(token, frm, to)


def reporting_only() -> None:
    """Только Reporting API (stat/v1/data) — синхронно, мгновенно, не трогает Logs (нет 429)."""
    token = os.environ["APPMETRICA_TOKEN"]
    frm, to = _win()
    print(f"app={APP_ID}, reporting-only окно {frm}..{to}")
    _reporting_probe(token, frm, to)


if __name__ == "__main__":
    main()
