#!/usr/bin/env python3
"""Синк витрин вкладки «Категории» LIME: разделы, каналы, товары, кросс, кампании.

Ежедневно пересчитывает последние LIME_SECTIONS_DAYS полных дней (по умолчанию 3 —
поздние данные Метрики/AppMetrica доезжают за сутки-двое) и перезаписывает их
идемпотентно. Бэкфилл: LIME_SECTIONS_FROM/LIME_SECTIONS_TO (включительно).

Env:
  DATABASE_URL          — Postgres
  LIME_METRIKA_TOKEN    — сайт (Метрика 23504302); нет → веб пропускается
  APPMETRICA_TOKEN      — приложение (4415407); нет → app пропускается
  LIME_SECTIONS_CHECK=1 — не писать, сверить посчитанное с базой (proof-режим)
  LIME_SECTIONS_DUMP=dir — не трогать базу вовсе, сложить посчитанные строки в
                           JSON (отладка/сверка там, где Postgres недоступен)
  LIME_SECTIONS_PLATFORMS=web,app — какие площадки гнать
  LIME_SECTIONS_COHORT_ONLY=1 — писать только когорту (стейт+матрица), витрины
                           не трогать: бэкфилл когорты по сверенной истории

Запуск: python sync_lime_sections.py
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

MSK = timezone(timedelta(hours=3))


def _window():
    frm, to = os.environ.get("LIME_SECTIONS_FROM"), os.environ.get("LIME_SECTIONS_TO")
    if frm and to:
        return frm, to
    days = int(os.environ.get("LIME_SECTIONS_DAYS", "3"))
    yesterday = datetime.now(MSK).date() - timedelta(days=1)
    return (yesterday - timedelta(days=days - 1)).isoformat(), yesterday.isoformat()


def _days(frm: str, to: str):
    d, end = date.fromisoformat(frm), date.fromisoformat(to)
    while d <= end:
        yield d.isoformat()
        d += timedelta(days=1)


def main() -> None:
    if not os.environ.get("DATABASE_URL"):
        print("ОШИБКА: нет DATABASE_URL")
        sys.exit(1)
    frm, to = _window()
    platforms = [p.strip() for p in os.environ.get("LIME_SECTIONS_PLATFORMS", "web,app").split(",") if p.strip()]
    check = os.environ.get("LIME_SECTIONS_CHECK") == "1"
    dump_dir = os.environ.get("LIME_SECTIONS_DUMP")
    print(f"=== lime sections sync: {frm}…{to}, платформы {platforms}, "
          f"режим {'дамп' if dump_dir else 'СВЕРКА' if check else 'запись'} ===")

    from sync.lime_feedmap import FeedMap, fetch_feed
    from sync.lime_sections_common import SectionResolver
    from sync.lime_sections_db import compare_day, write_day

    cohort_only = os.environ.get("LIME_SECTIONS_COHORT_ONLY") == "1"

    def emit(platform: str, day: str, sets: dict) -> None:
        # Когорта живёт отдельным шагом: свой стейт в БД, дни строго по порядку.
        cohort_input = sets.pop("cohort_input", None)
        if not cohort_only:
            if dump_dir:
                import json
                os.makedirs(dump_dir, exist_ok=True)
                with open(os.path.join(dump_dir, f"{platform}_{day}.json"), "w", encoding="utf-8") as f:
                    json.dump(sets, f, ensure_ascii=False)
            else:
                (compare_day if check else write_day)(platform, day, sets)
        if platform == "web" and cohort_input and not check and not dump_dir:
            from sync.lime_sections_cohort import sync_cohort_web
            sync_cohort_web(day, cohort_input)

    feed_path = fetch_feed(os.path.join(tempfile.gettempdir(), "lime_feed.xml"))
    resolver = SectionResolver(FeedMap(feed_path))
    print(f"фид: типов {len(set(resolver.fm.article2type.values()))}, "
          f"артикулов {len(resolver.fm.article2gender):,}")

    web_token = os.environ.get("LIME_METRIKA_TOKEN")
    if "web" in platforms and web_token:
        from sync.lime_sections_web import build_web_day
        for day in _days(frm, to):
            emit("web", day, build_web_day(day, web_token, resolver, with_hits=not cohort_only))
    elif "web" in platforms:
        print("web: пропуск (нет LIME_METRIKA_TOKEN)")

    app_token = os.environ.get("APPMETRICA_TOKEN")
    if cohort_only:
        app_token = None    # когорта пока только web — приложение не гоняем зря
    if "app" in platforms and app_token:
        from sync.lime_sections_app import Attribution, build_app_day
        attr = Attribution(app_token, date.fromisoformat(frm), date.fromisoformat(to))
        seen_tx: set = set()
        for day in _days(frm, to):
            emit("app", day, build_app_day(day, app_token, resolver, attr, seen_tx))
    elif "app" in platforms:
        print("app: пропуск (нет APPMETRICA_TOKEN)")

    print("=== lime sections sync DONE ===")


if __name__ == "__main__":
    main()
