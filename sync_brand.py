#!/usr/bin/env python3
"""Инкрементальный синк брендового трафика LIME (Wordstat спрос + Вебмастер SEO).

Wordstat пропускается, если нет Cloud-кредов (YANDEX_SEARCHAPI_KEY + YANDEX_CLOUD_FOLDER_ID) —
Вебмастер синкается независимо. Запуск: python sync_brand.py
"""
import datetime as dt
import os
import sys

from dotenv import load_dotenv

load_dotenv()

INCREMENTAL_WEEKS = 8


def main() -> None:
    if not os.environ.get("DATABASE_URL"):
        print("ОШИБКА: нет DATABASE_URL")
        sys.exit(1)

    errors: list[str] = []

    # Wordstat спрос (Cloud Search API) — нужен только API-ключ (folderId опц.).
    # WORDSTAT_FROM=YYYY-MM-DD → бэкфилл с этой даты; иначе инкремент последних недель.
    if os.environ.get("YANDEX_SEARCHAPI_KEY"):
        try:
            from sync.wordstat import sync_wordstat_demand

            frm = os.environ.get("WORDSTAT_FROM") or (
                dt.date.today() - dt.timedelta(weeks=INCREMENTAL_WEEKS)
            ).isoformat()
            n = sync_wordstat_demand(frm, dt.date.today().isoformat())
            print(f"wordstat: {n} недель (с {frm})")
        except Exception as e:
            print(f"ОШИБКА wordstat: {e}")
            errors.append(f"wordstat: {e}")
    else:
        print("wordstat: пропуск (нет YANDEX_SEARCHAPI_KEY)")

    # Вебмастер SEO
    if os.environ.get("WORDSTAT_WEBMASTER_TOKEN"):
        try:
            from sync.webmaster import sync_brand_seo

            n = sync_brand_seo()
            print(f"webmaster: {n} недель")
        except Exception as e:
            print(f"ОШИБКА webmaster: {e}")
            errors.append(f"webmaster: {e}")
    else:
        print("webmaster: пропуск (нет WORDSTAT_WEBMASTER_TOKEN)")

    # Google Search Console SEO (сервис-аккаунт как пользователь ресурсов).
    # GSC_FROM=YYYY-MM-DD → бэкфилл; иначе инкремент последних недель.
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or os.environ.get("GOOGLE_SERVICE_ACCOUNT"):
        try:
            from sync.gsc import sync_gsc_seo

            frm = os.environ.get("GSC_FROM") or (
                dt.date.today() - dt.timedelta(weeks=INCREMENTAL_WEEKS)
            ).isoformat()
            n = sync_gsc_seo(frm, dt.date.today().isoformat())
            print(f"gsc: {n} недель (с {frm})")
        except Exception as e:
            print(f"ОШИБКА gsc: {e}")
            errors.append(f"gsc: {e}")
    else:
        print("gsc: пропуск (нет GOOGLE_APPLICATION_CREDENTIALS / GOOGLE_SERVICE_ACCOUNT)")

    if errors:
        print(f"Завершено с ошибками: {errors}")
        sys.exit(1)
    print("=== brand sync DONE ===")


if __name__ == "__main__":
    main()
