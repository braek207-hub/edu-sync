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
from collections import Counter
from datetime import datetime, timedelta, timezone

from sync.appmetrica_logs import fetch_installations, fetch_purchase_events, fetch_sessions

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
            ej = ev[0].get("event_json")
            try:
                print("  event_json ключи:", sorted(json.loads(ej).keys()) if ej else "пусто")
            except Exception:  # noqa: BLE001
                print("  event_json (raw):", str(ej)[:200])
    except Exception as e:  # noqa: BLE001
        print(f"[events] ОШИБКА {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
