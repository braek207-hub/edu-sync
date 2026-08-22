# -*- coding: utf-8 -*-
"""
sync/agent/metrika.py — обогащение витрин автопилота данными Яндекс Метрики.

Закрывает две дыры, которые Директ не закрывает:

  1. ПОЧАСОВОЙ ПРОФИЛЬ. Reports API отвергает HourOfDay (probe 31781715471) —
     почасовой детализации расхода Директ не отдаёт вовсе. Метрика отдаёт визиты
     и цели по часам, и корректировка расписания считается по ним.

  2. РАННИЙ СИГНАЛ КАЧЕСТВА. Отказы, глубина и время на сайте видны в тот же день,
     а оплата созревает 30–90 дней. Для агента это единственная метрика качества
     трафика с суточной задержкой — прокси p_pay опирается на поведение, но сам
     по себе поведенческий срез нужен и как независимый детектор: рост отказов
     при неизменном CPA означает, что изменение испортило релевантность.

Идём через Reporting API (/stat/v1/data) с тем же OAuth YM_TOKEN и той же атрибуцией
lastsign, что и sync/edu_visits.py — иначе цифры разойдутся с существующими витринами.

Хранение: поведенческие показатели — суммами и счётчиками (visits, bounces, pageviews,
visit_seconds), а не готовыми процентами: среднее по среднему не складывается при
перегруппировке по неделям и направлениям.
"""

import os
import time
from typing import Any, Dict, List

import requests

METRICA_API_URL = "https://api-metrika.yandex.net/stat/v1/data"
GOALS_API_URL = "https://api-metrika.yandex.net/management/v1/counter/{counter}/goals"
ATTRIBUTION = "lastsign"
ROW_LIMIT = 10_000
# Счётчики EDU по проектам (vuz/vse/provuz) — те же, что в sync/edu_direct_settings.py.
EDU_COUNTERS = [98627983, 96526110, 95348914]


def _metrica_get(params: Dict[str, Any], token: str) -> Dict[str, Any]:
    """GET Reporting API с ретраями. Тот же путь, что sync/edu_visits.py::_metrica_get."""
    headers = {"Authorization": f"OAuth {token}"}
    backoff = 2
    for attempt in range(6):
        resp = requests.get(METRICA_API_URL, params=params, headers=headers, timeout=120)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in {429, 500, 502, 503, 504} and attempt < 5:
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        raise RuntimeError(f"Metrica API {resp.status_code}: {resp.text[:400]}")
    raise RuntimeError("Metrica API: max retries")


def parse_hourly(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Ответ Метрики → почасовой профиль в формате среза slice_kind='hour'.

    Час извлекается из измерения ym:s:hour (значение вида '13' или '13:00').
    """
    out: List[Dict[str, Any]] = []
    for row in data.get("data") or []:
        dims = row.get("dimensions") or []
        metrics = row.get("metrics") or []
        if not dims or not metrics:
            continue
        raw_hour = str(dims[0].get("name") or dims[0].get("id") or "").strip()
        hour = raw_hour.split(":")[0].lstrip("0") or "0"
        visits = float(metrics[0] or 0.0)
        # Целей в запросе несколько (по одной колонке на цель) — складываем ВСЕ,
        # а не берём первую: расписание считается по сумме лидов часа.
        goals = sum(float(m or 0.0) for m in metrics[1:])
        out.append({
            "segment_kind": "hour",
            "segment_key": hour,
            # Для corrections важна конверсионность: визиты играют роль кликов,
            # достижения цели — роль ожидаемых оплат.
            "clicks": int(visits),
            "leads": int(goals),
            "sum_p_pay": goals,
        })
    return sorted(out, key=lambda r: int(r["segment_key"]))


def parse_campaign_behavior(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Ответ Метрики → поведение по кампаниям Директа.

    ВАЖНО: Метрика отдаёт ИМЯ кампании, а не её Id — и ym:s:lastDirectClickOrder,
    и ...OrderName возвращают одну и ту же строку с названием (probe 31788247020).
    Поэтому здесь возвращается campaign_name, а привязка к Id делается снаружи
    по справочнику имён из фактов.

    Отдаём суммы и счётчики: visits, bounces, pageviews, visit_seconds.
    Проценты и средние считает потребитель — иначе перегруппировка врёт.
    """
    out: List[Dict[str, Any]] = []
    for row in data.get("data") or []:
        dims = row.get("dimensions") or []
        metrics = row.get("metrics") or []
        if not dims or len(metrics) < 4:
            continue
        campaign_name = str(dims[0].get("name") or "").strip()
        if not campaign_name or campaign_name.lower() in {"не задано", "(not set)"}:
            continue
        visits = float(metrics[0] or 0.0)
        bounce_rate = float(metrics[1] or 0.0)      # проценты
        page_depth = float(metrics[2] or 0.0)       # страниц за визит
        avg_seconds = float(metrics[3] or 0.0)      # секунд за визит
        out.append({
            "campaign_name": campaign_name,
            "visits": int(visits),
            "bounces": int(round(visits * bounce_rate / 100.0)),
            "pageviews": int(round(visits * page_depth)),
            "visit_seconds": int(round(visits * avg_seconds)),
        })
    return sorted(out, key=lambda r: r["campaign_name"])


def fetch_counter_goal_ids(counter_id: int) -> List[int]:
    """Идентификаторы целей, заведённых на счётчике.

    Нужны, чтобы спрашивать почасовые достижения только по тем целям, которые
    у счётчика есть: `ym:s:goal<чужой id>reaches` Метрика отвергает целиком, и
    один лишний идентификатор обнулил бы профиль всего счётчика.
    """
    headers = {"Authorization": f"OAuth {os.environ['YM_TOKEN']}"}
    resp = requests.get(GOALS_API_URL.format(counter=counter_id),
                        headers=headers, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f"Metrica goals {resp.status_code}: {resp.text[:300]}")
    return [int(g["id"]) for g in (resp.json().get("goals") or []) if g.get("id")]


def hourly_metrics(goal_ids: List[int]) -> str:
    """Строка metrics: визиты плюс отдельная колонка на каждую цель."""
    ordered = sorted({int(g) for g in goal_ids})
    return ",".join(["ym:s:visits"] + [f"ym:s:goal{g}reaches" for g in ordered])


def fetch_hourly_profile(counter_id: int, date_from: str, date_to: str,
                         goal_ids: List[int]) -> List[Dict[str, Any]]:
    """Визиты и достижения ЦЕЛЕВЫХ целей по часам суток.

    goal_ids обязателен и обязан быть непустым. Раньше здесь стояла
    `ym:s:sumGoalReachesAny` — сумма достижений ЛЮБОЙ цели счётчика, и это
    оказалось не про лиды: на замере 22.08.2026 (счётчик 98627983, окно 90
    дней) 0.38 достижения на визит складывались на ~70 % из микроцелей
    «2 минуты на сайте», «глубина прокрутки», «прокрутка: середина блока»,
    а заявки давали 2 %. Микроцели и заявки расходятся по суткам ПРОТИВОПОЛОЖНО:
    прокрутка ночью 1.17–1.49 к дню, «CRM: новая заявка» — 0.45, «новая сделка» —
    0.41. Расписание, посчитанное по Any, поднимало ставку на 3:00–7:00 (+13,
    +27, +21 %) — то есть на часы, где заявок вдвое меньше.

    Пересечение с целями счётчика делает вызывающий: цель принадлежит одному
    счётчику, и чужой идентификатор в metrics отвергается Метрикой целиком.
    """
    if not goal_ids:
        raise ValueError(
            "fetch_hourly_profile без целей: считать расписание не по чему. "
            "Профиль по «любой цели» — это профиль прокрутки, а не лидов."
        )
    params = {
        "ids": counter_id,
        "metrics": hourly_metrics(goal_ids),
        "dimensions": "ym:s:hour",
        "date1": date_from,
        "date2": date_to,
        "attribution": ATTRIBUTION,
        "limit": ROW_LIMIT,
        "accuracy": "full",
    }
    return parse_hourly(_metrica_get(params, os.environ["YM_TOKEN"]))


def merge_hourly(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Почасовые строки НЕСКОЛЬКИХ счётчиков → один профиль, сложенный по часу.

    Дефект, который это закрывает (сухой прогон 32568178620): строки трёх
    счётчиков EDU просто складывались в один список, и для каждого часа
    оказывалось три отдельные строки. Конверсионность у счётчиков разная —
    0.385, 0.340 и 0.285 на замере 22.08.2026, — а база считается по сумме
    ВСЕХ строк. В итоге у счётчика с конверсией ниже общей ВСЕ 24 часа
    уходили вниз (0.285 / 0.36 ≈ 0.79), у счётчика выше — все вверх, а при
    записи по ключу (кабинет, вид, час) последний перетирал остальные.
    Расписание получалось «22 часа из 24 опустить» — то есть профилем
    счётчика, а не профилем суток.

    Правильная величина — конверсионность ЧАСА ПО ВСЕМУ EDU: визиты и
    достижения целей складываются, и только потом считается отношение.
    """
    merged: Dict[str, Dict[str, Any]] = {}
    for row in rows or []:
        key = str(row.get("segment_key"))
        slot = merged.setdefault(key, {
            "segment_kind": "hour", "segment_key": key,
            "clicks": 0, "leads": 0, "sum_p_pay": 0.0,
        })
        slot["clicks"] += int(row.get("clicks") or 0)
        slot["leads"] += int(row.get("leads") or 0)
        slot["sum_p_pay"] += float(row.get("sum_p_pay") or 0.0)
    return sorted(merged.values(), key=lambda r: int(r["segment_key"]))


def fetch_campaign_behavior(counter_id: int, date_from: str, date_to: str) -> List[Dict[str, Any]]:
    """Поведение по кампаниям Директа: отказы, глубина, время."""
    params = {
        "ids": counter_id,
        "metrics": "ym:s:visits,ym:s:bounceRate,ym:s:pageDepth,ym:s:avgVisitDurationSeconds",
        "dimensions": "ym:s:lastDirectClickOrderName",
        "filters": "ym:s:lastTrafficSource=='ad'",
        "date1": date_from,
        "date2": date_to,
        "attribution": ATTRIBUTION,
        "limit": ROW_LIMIT,
        "accuracy": "full",
    }
    return parse_campaign_behavior(_metrica_get(params, os.environ["YM_TOKEN"]))
