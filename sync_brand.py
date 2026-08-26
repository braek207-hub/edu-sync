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
# Регионы спроса Wordstat (ряды в lime_wordstat_demand[_daily] по колонке region).
WORDSTAT_REGIONS = ("ru", "kz", "gcc")
# Регионы органики Яндекса (Метрика; ряды в lime_yandex_organic). RU там не нужен —
# в RU органику даёт Вебмастер (клики выдачи, длинная история).
ORGANIC_REGIONS = ("kz", "gcc")
# Окно перезаписи органики: Метрика досчитывает визиты сутки-двое, 90 дней с запасом.
ORGANIC_WINDOW_DAYS = 90


def main() -> None:
    if not os.environ.get("DATABASE_URL"):
        print("ОШИБКА: нет DATABASE_URL")
        sys.exit(1)

    errors: list[str] = []

    # Wordstat спрос (Cloud Search API) — нужен только API-ключ (folderId опц.).
    # WORDSTAT_FROM=YYYY-MM-DD → бэкфилл с этой даты; иначе инкремент последних недель.
    # Регионы: ru (без гео), kz и gcc (гео-фильтр + локальные фразы) — свой ряд у каждого,
    # свой гард свежести; ошибка одного региона не мешает остальным.
    if os.environ.get("YANDEX_SEARCHAPI_KEY"):
        for region in WORDSTAT_REGIONS:
            try:
                from sync.wordstat import sync_wordstat_demand, demand_up_to_date

                # Крон ежедневный: пока прошлой закрытой недели нет — дёргаем API; появилась — пропуск.
                if not os.environ.get("WORDSTAT_FROM") and demand_up_to_date(
                    "lime_wordstat_demand", region
                ):
                    print(f"wordstat[{region}]: последняя закрытая неделя уже есть — пропуск")
                else:
                    frm = os.environ.get("WORDSTAT_FROM") or (
                        dt.date.today() - dt.timedelta(weeks=INCREMENTAL_WEEKS)
                    ).isoformat()
                    n = sync_wordstat_demand(frm, dt.date.today().isoformat(), region)
                    print(f"wordstat[{region}]: {n} недель (с {frm})")
            except Exception as e:
                print(f"ОШИБКА wordstat[{region}]: {e}")
                errors.append(f"wordstat[{region}]: {e}")

            # Дневной срез спроса (глубина Wordstat — 60 дней) — отдельный try:
            # ошибка дневного не мешает уже записанному недельному (и наоборот).
            try:
                from sync.wordstat import (
                    daily_demand_up_to_date,
                    daily_floor,
                    sync_wordstat_demand_daily,
                )

                # Лаг дневного Wordstat 1-3 дня: пока «вчера-1» нет — дёргаем API, появился — отдыхаем.
                # WORDSTAT_FROM (бэкфилл/смена методики) перезаписывает и дневное окно:
                # свежесть не гарантирует, что старые дни сняты той же методикой, что недельный ряд.
                if not os.environ.get("WORDSTAT_FROM") and daily_demand_up_to_date(
                    "lime_wordstat_demand_daily", region
                ):
                    print(f"wordstat-daily[{region}]: свежие дни уже есть — пропуск")
                else:
                    # Всё доступное окно (60 дней): upsert идемпотентен, поэтому первый запуск —
                    # это же и бэкфилл, отдельная команда не нужна.
                    frm = daily_floor()
                    n = sync_wordstat_demand_daily(frm, dt.date.today().isoformat(), region)
                    print(f"wordstat-daily[{region}]: {n} дней (с {frm})")
            except Exception as e:
                print(f"ОШИБКА wordstat-daily[{region}]: {e}")
                errors.append(f"wordstat-daily[{region}]: {e}")
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

        # Дневной SEO-срез — отдельный try: ошибка дневного не мешает уже
        # записанному недельному (и наоборот). Гоняем КАЖДЫЙ прогон, без
        # skip-гарда по свежести: сводка дозаливает дни до ~2 недель задним
        # числом, а прежний гард замораживал недозревшие дни (снят 2026-08-21
        # при переводе на TOTAL_CLICKS; синк — 4 GET, дёшево).
        try:
            from sync.webmaster import sync_brand_seo_daily

            n = sync_brand_seo_daily()
            print(f"webmaster-daily: {n} дней")
        except Exception as e:
            print(f"ОШИБКА webmaster-daily: {e}")
            errors.append(f"webmaster-daily: {e}")
    else:
        print("webmaster: пропуск (нет WORDSTAT_WEBMASTER_TOKEN)")

    # Google Search Console SEO (сервис-аккаунт как пользователь ресурсов).
    # GSC_FROM=YYYY-MM-DD → бэкфилл; иначе инкремент последних недель.
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or os.environ.get("GOOGLE_SERVICE_ACCOUNT"):
        # KZ и GCC — один источник (Search Console), разные ресурсы и страны.
        # GCC_GSC_FROM отдельно от GSC_FROM: история GCC начинается с 2025-10.
        for reg, from_env in (("kz", "GSC_FROM"), ("gcc", "GCC_GSC_FROM")):
            try:
                from sync.gsc import sync_gsc_seo

                frm = os.environ.get(from_env) or (
                    dt.date.today() - dt.timedelta(weeks=INCREMENTAL_WEEKS)
                ).isoformat()
                n = sync_gsc_seo(frm, dt.date.today().isoformat(), reg)
                print(f"gsc[{reg}]: {n} строк неделя×страна (с {frm})")
            except Exception as e:
                print(f"ОШИБКА gsc[{reg}]: {e}")
                errors.append(f"gsc[{reg}]: {e}")
    else:
        print("gsc: пропуск (нет GOOGLE_APPLICATION_CREDENTIALS / GOOGLE_SERVICE_ACCOUNT)")

    # Органика Яндекса по гео (KZ, Залив) — Метрика: у API Вебмастера гео-среза нет
    # (см. докстринг sync/yandex_organic.py). ORGANIC_FROM=YYYY-MM-DD → бэкфилл;
    # иначе перезаписываем окно последних 90 дней (upsert идемпотентен).
    if os.environ.get("LIME_METRIKA_TOKEN"):
        for region in ORGANIC_REGIONS:
            try:
                from sync.yandex_organic import sync_yandex_organic

                frm = os.environ.get("ORGANIC_FROM") or (
                    dt.date.today() - dt.timedelta(days=ORGANIC_WINDOW_DAYS)
                ).isoformat()
                n = sync_yandex_organic(frm, dt.date.today().isoformat(), region)
                print(f"yandex-organic[{region}]: {n} строк день×страна (с {frm})")
            except Exception as e:
                print(f"ОШИБКА yandex-organic[{region}]: {e}")
                errors.append(f"yandex-organic[{region}]: {e}")
    else:
        print("yandex-organic: пропуск (нет LIME_METRIKA_TOKEN)")

    if errors:
        print(f"Завершено с ошибками: {errors}")
        sys.exit(1)
    print("=== brand sync DONE ===")


if __name__ == "__main__":
    main()
