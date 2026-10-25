#!/usr/bin/env python3
"""Синк LIME «APP · AppMetrica»: недельные установки по партнёрам + когорты покупателей.

Окно и поведение — через env (см. sync/lime_appmetrica.py):
  APP_COHORT_MONTHS (7), APP_MAX_LIFE (6), APPMETRICA_EVENT_NAME (purchase),
  APPMETRICA_APP_ID (4415407), APP_KEEP_REATTR / APP_KEEP_REINSTALL (выкл).
Сырьё — appm_* (sync.appmetrica_raw); APPM_SOURCE=api — старый путь через Logs API. Запуск: python sync_lime_appmetrica.py
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    if not os.environ.get("DATABASE_URL"):
        print("ОШИБКА: нет DATABASE_URL")
        sys.exit(1)
    # Токен нужен только старому пути (APPM_SOURCE=api): с Y1 витрина читает сырьё из базы.
    if (os.environ.get("APPM_SOURCE") or "db").strip().lower() == "api" and not os.environ.get("APPMETRICA_TOKEN"):
        print("lime-appmetrica: пропуск (нет APPMETRICA_TOKEN)")
        return

    from sync.lime_appmetrica import sync_lime_appmetrica

    sync_lime_appmetrica()
    print("=== lime appmetrica sync DONE ===")


if __name__ == "__main__":
    main()
