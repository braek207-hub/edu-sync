# -*- coding: utf-8 -*-
"""Roistat API v1 — аналитика проекта LIME KZ.

Контракт снят зондами 2026-07-19 и сверен с интерфейсом Роистата на полном июне
(расхождение 0.05–0.10%). План: docs/superpowers/plans/2026-07-19-lime-kz-roistat.md
в репозитории приложения.

Четыре особенности, каждая ломает наивную реализацию:

1. `to` в периоде ЭКСКЛЮЗИВЕН. Запрос с from==to возвращает нули, а не день.
2. Измерения по дате нет — дневная гранулярность только отдельным запросом на каждый день.
3. В имени метрики `revenue_сanceled` буква «с» КИРИЛЛИЧЕСКАЯ (U+0441). Набранное
   латиницей имя невалидно, и API отвечает request_data_validation_error.
4. Подписи приходят с неразрывным пробелом: 'Google\xa0Ads\xa01'. Без нормализации
   склейка по имени канала молча проваливается и весь платный трафик уходит в «Others».

Ошибки API приходят с HTTP 200 и телом {"status": "error"} — проверять код ответа мало.

ENV: ROISTAT_API_KEY, ROISTAT_PROJECT_ID (default 235593).
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

API_URL = "https://cloud.roistat.com/api/v1/project/analytics/data"

RETRIES = int(os.environ.get("ROISTAT_RETRIES") or "3")
RETRY_SLEEP = int(os.environ.get("ROISTAT_RETRY_SLEEP") or "5")

# Порядок не важен — разбор идёт по metric_name из ответа.
# revenue_сanceled: «с» кириллическая, см. пункт 3 докстринга модуля.
METRICS = (
    "visitCount",
    "leadCount",
    "paidLeadCount",
    "paidLeadsPrice",
    "progressLeadsPrice",
    "revenue_сanceled",
    "visitsCost",
    "paidClientCount",
    "canceledLeadCount",
)

FIELD_BY_METRIC = {
    "visitCount": "visits",
    "leadCount": "leads",
    "paidLeadCount": "paid_leads",
    "paidLeadsPrice": "paid_revenue",
    "progressLeadsPrice": "progress_revenue",
    "revenue_сanceled": "canceled_revenue",
    "visitsCost": "cost",
    "paidClientCount": "paid_clients",
    "canceledLeadCount": "canceled_leads",
}

# Кампания лежит на РАЗНЫХ уровнях: у Google/Директа — level_3 (level_2 это код типа:
# g / d / x / search / context), у Facebook — level_2, а level_3 это адсет. Тянем оба
# и выбираем по каналу в sync.roistat_channels.campaign_of.
DIMENSIONS = ("marker_level_1", "marker_level_2", "marker_level_3")


def denbsp(s: str) -> str:
    """Неразрывный пробел → обычный, схлопнуть края.

    Роистат отдаёт подписи как 'Google\xa0Ads\xa01'. Без нормализации это не равно
    'Google Ads 1', и склейка по имени проваливается молча.
    """
    return (s or "").replace("\xa0", " ").strip()


def day_period(day_iso: str) -> dict:
    """Период для ОДНОГО дня. `to` эксклюзивен, поэтому это следующая дата."""
    d = date.fromisoformat(day_iso)
    nxt = d + timedelta(days=1)
    return {"from": d.strftime("%d.%m.%Y"), "to": nxt.strftime("%d.%m.%Y")}


def parse_analytics(resp: dict) -> list[dict]:
    """Разобрать ответ в плоские строки «канал + уровни + метрики».

    Args:
        resp: полный ответ API.

    Returns:
        Список дектов: channel, level2_id/level2, level3_id/level3 и поля
        FIELD_BY_METRIC; отсутствующие метрики = 0.0.
    """
    out: list[dict] = []
    for group in resp.get("data") or []:
        for item in group.get("items") or []:
            dims = item.get("dimensions") or {}

            def level(name: str) -> tuple[str, str]:
                """(id, подпись): value — настоящий id, title — читаемое имя."""
                lvl = dims.get(name) or {}
                return (denbsp(lvl.get("value") or ""), denbsp(lvl.get("title") or ""))

            ch_id, ch_name = level("marker_level_1")
            l2_id, l2_name = level("marker_level_2")
            l3_id, l3_name = level("marker_level_3")

            # У «Прямых визитов» value пустой, читаемое имя всегда в title.
            row = {
                "channel": ch_name or ch_id,
                "level2_id": l2_id, "level2": l2_name,
                "level3_id": l3_id, "level3": l3_name,
            }
            for field in FIELD_BY_METRIC.values():
                row[field] = 0.0
            for m in item.get("metrics") or []:
                field = FIELD_BY_METRIC.get(m.get("metric_name"))
                if field:
                    row[field] = float(m.get("value") or 0)
            out.append(row)
    return out


def fetch_day(day_iso: str, project: str, key: str) -> list[dict]:
    """Строки за один день. Повторяет запрос на транзиентных ошибках.

    Raises:
        RuntimeError: если API вернул ошибку после всех попыток.
    """
    qs = urllib.parse.urlencode({"key": key, "project": project})
    body = json.dumps({
        "dimensions": list(DIMENSIONS),
        "metrics": list(METRICS),
        "period": day_period(day_iso),
    }).encode("utf-8")

    last = None
    for attempt in range(1, RETRIES + 1):
        req = urllib.request.Request(
            f"{API_URL}?{qs}", data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                resp = json.loads(r.read().decode("utf-8"))
            # Ошибки приходят с HTTP 200 и status=error — проверять тело обязательно.
            if resp.get("status") == "error":
                last = str(resp.get("description") or resp.get("error"))
            else:
                return parse_analytics(resp)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = f"{type(e).__name__}: {e}"
        if attempt < RETRIES:
            print(f"roistat_api: WARN {day_iso} — {last}, попытка {attempt} из {RETRIES}")
            time.sleep(RETRY_SLEEP * attempt)

    raise RuntimeError(f"roistat_api: {day_iso} не забран после {RETRIES} попыток: {last}")
