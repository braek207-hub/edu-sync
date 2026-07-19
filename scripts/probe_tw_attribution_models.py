# -*- coding: utf-8 -*-
"""Зонд: можно ли расщепить TW `organic_and_social` на органику и соцсети по факту.

Вопрос: `order_source()` берёт первый непустой source из lastPlatformClick → lastClick →
fullLastClick и для органических заказов получает единое "organic_and_social". Если другая
модель атрибуции несёт домен-реферер (google.com / instagram.com), деление станет
фактическим, а не пропорциональной моделью.

Печатает ТОЛЬКО агрегаты и имена полей — ни ключа, ни персональных данных заказов.
Запуск: python -m scripts.probe_tw_attribution_models [дней_назад]
"""
import io
import json
import os
import sys
from collections import Counter
from datetime import date, timedelta

from dotenv import load_dotenv

# Консоль Windows по умолчанию cp1251 — кириллица и стрелки роняют скрипт на print.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sync.gcc_triplewhale import TW_ORDERS_URL, _tw_post  # noqa: E402

MODELS = ("lastPlatformClick", "lastClick", "fullLastClick", "firstClick",
          "fullFirstClick", "linear", "linearAll")


def main() -> None:
    days_back = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    key = os.environ["GCC_TRIPLEWHALE_API_KEY"]
    shop = os.environ["GCC_TW_SHOP_DOMAIN"]

    to = date.today() - timedelta(days=1)
    frm = to - timedelta(days=days_back - 1)
    print(f"[зонд] период {frm}…{to}, магазин задан из env")

    data = _tw_post(
        TW_ORDERS_URL,
        {"x-api-key": key, "content-type": "application/json"},
        {"shop": shop, "startDate": frm.isoformat(), "endDate": to.isoformat(),
         "excludeJourneyData": True},
        timeout=180,
    )
    orders = data.get("ordersWithJourneys") or []
    print(f"[зонд] заказов: {len(orders)} (totalForRange={data.get('totalForRange')})")
    if not orders:
        return

    # 1. Какие вообще модели присутствуют и какие поля у тачпоинта.
    models_seen: Counter = Counter()
    touchpoint_fields: Counter = Counter()
    for o in orders:
        attribution = o.get("attribution") or {}
        for model, touchpoints in attribution.items():
            if touchpoints:
                models_seen[model] += 1
                for tp in touchpoints:
                    if isinstance(tp, dict):
                        for field in tp.keys():
                            touchpoint_fields[field] += 1

    print("\n[1] Модели с непустыми тачпоинтами:")
    for model, n in models_seen.most_common():
        print(f"    {model:<20} {n} заказов")
    print("\n[2] Поля тачпоинта (по всем моделям):")
    for field, n in touchpoint_fields.most_common():
        print(f"    {field:<24} {n}")

    # 2. Главный вопрос: что каждая модель говорит про заказы, которые сейчас
    #    схлопываются в organic_and_social.
    def first_source(order: dict, model: str):
        tps = (order.get("attribution") or {}).get(model) or []
        return tps[0].get("source") if tps and isinstance(tps[0], dict) else None

    def current_source(order: dict):
        for model in ("lastPlatformClick", "lastClick", "fullLastClick"):
            src = first_source(order, model)
            if src:
                return src
        return None

    organic = [o for o in orders if (current_source(o) or "") == "organic_and_social"]
    print(f"\n[3] Заказов с source='organic_and_social': {len(organic)} из {len(orders)}")

    if organic:
        for model in MODELS:
            sources = Counter((first_source(o, model) or "—") for o in organic)
            distinct = [s for s in sources if s not in ("—", "organic_and_social")]
            verdict = "РАЗЛИЧАЕТ" if distinct else "то же самое"
            print(f"\n    {model} → {verdict}")
            for src, n in sources.most_common(8):
                print(f"        {src!r:<40} {n}")

        # Находка зонда: у organic_and_social в campaignId лежит ДОМЕН-РЕФЕРЕР
        # (yandex.ru, lime-shop.com), а не id кампании. Значит деление
        # органика/соцсети делается по факту. Собираем словарь доменов.
        referrers: Counter = Counter()
        for o in organic:
            for model in ("lastPlatformClick", "fullLastClick", "fullFirstClick"):
                tps = (o.get("attribution") or {}).get(model) or []
                if tps and isinstance(tps[0], dict) and tps[0].get("source") == "organic_and_social":
                    referrers[tps[0].get("campaignId") or "(пусто)"] += 1
                    break
        print("\n[5] Домены-рефереры у organic_and_social (campaignId):")
        for dom, n in referrers.most_common(40):
            print(f"        {dom!r:<40} {n}")

        sample = organic[0]
        print("\n[4] Полная структура attribution одного органического заказа:")
        for model, tps in (sample.get("attribution") or {}).items():
            if not tps:
                continue
            print(f"    {model}:")
            print("        " + json.dumps(tps[0], ensure_ascii=False)[:400])


if __name__ == "__main__":
    main()
