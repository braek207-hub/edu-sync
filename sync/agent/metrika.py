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
from typing import Any, Dict, List, Tuple

import requests

METRICA_API_URL = "https://api-metrika.yandex.net/stat/v1/data"
GOALS_API_URL = "https://api-metrika.yandex.net/management/v1/counter/{counter}/goals"
ATTRIBUTION = "lastsign"
# Профиль накладывается на показы Директа — считать его надо по рекламному
# трафику, а не по всему сайту (тот же фильтр у fetch_campaign_behavior).
AD_FILTER = "ym:s:lastTrafficSource=='ad'"
ROW_LIMIT = 10_000
# Метрика берёт не больше 20 метрик на запрос (код 4015). Одну занимают визиты.
MAX_GOALS_PER_REQUEST = 19
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


def parse_hourly_by_goal(data: Dict[str, Any],
                         goal_ids: List[int]) -> Tuple[Dict[str, float],
                                                       Dict[int, Dict[str, float]]]:
    """Ответ Метрики → визиты по часам и достижения по часам ОТДЕЛЬНО на цель.

    Колонки metrics идут в том же порядке, в каком их запросили: нулевая —
    визиты, дальше по одной на цель в порядке hourly_metrics (по возрастанию
    идентификатора). Разделение по целям обязательно: складывать колонки
    нельзя, см. pick_lead_goal.

    Час извлекается из измерения ym:s:hour (значение вида '13' или '13:00').
    """
    ordered = sorted({int(g) for g in goal_ids})
    visits: Dict[str, float] = {}
    reaches: Dict[int, Dict[str, float]] = {g: {} for g in ordered}
    for row in data.get("data") or []:
        dims = row.get("dimensions") or []
        metrics = row.get("metrics") or []
        if not dims or not metrics:
            continue
        raw_hour = str(dims[0].get("name") or dims[0].get("id") or "").strip()
        hour = raw_hour.split(":")[0].lstrip("0") or "0"
        visits[hour] = float(metrics[0] or 0.0)
        for i, goal in enumerate(ordered, start=1):
            reaches[goal][hour] = float(metrics[i] or 0.0) if i < len(metrics) else 0.0
    return visits, reaches


def pick_lead_goal(reaches: Dict[int, Dict[str, float]]) -> Any:
    """Одна цель-числитель из набора целей Директа. None — достижений нет вовсе.

    Почему НЕ сумма колонок. Цели Директа дублируют друг друга, и это измерено
    здесь дважды. В отчёте Директа 330070378 и 369313502 дали одинаковые
    2753/2753 — одно действие под двумя идентификаторами. Проба 32579085232
    подтвердила то же в Метрике за 90 дней: у счётчика 96526110 «Страница
    "Спасибо" VseKolledzhi» и «Страницы спасибо» дали ПОБУКВЕННО совпадающие
    векторы 24 часов (37 252 = 37 252), а рядом «Автоцель: отправил контактные
    данные» — тот же поступок третьим способом (корреляция 0.9995). У счётчика
    98627983 к ним добавлена ступенчатая «CRM: Заказ оплачен» — подмножество
    заявки. Сумма считает одну заявку дважды и подмешивает оплаты.

    Берётся цель с наибольшим числом достижений: заявка всегда объёмнее своей
    ступени (оплаты), а из дублей выбор безразличен — векторы совпадают.
    Выбор не молчаливый: вызывающий возвращает его в отчёт прогона, потому что
    «самая массовая колонка» без имени рядом — это тот же приём, каким сюда
    однажды пролезла микроцель прокрутки.
    """
    totals = {g: sum(hours.values()) for g, hours in reaches.items()}
    best = max(totals.values()) if totals else 0.0
    if best <= 0:
        return None
    # Порядок по идентификатору — чтобы выбор был воспроизводим на дублях.
    return min(g for g, t in totals.items() if t == best)


def profile_rows(visits: Dict[str, float],
                 reaches: Dict[str, float]) -> List[Dict[str, Any]]:
    """Визиты и достижения по часам → строки среза slice_kind='hour'."""
    out: List[Dict[str, Any]] = []
    for hour, v in visits.items():
        goals = float(reaches.get(hour) or 0.0)
        out.append({
            "segment_kind": "hour",
            "segment_key": hour,
            # Для corrections важна конверсионность: визиты играют роль кликов,
            # достижения цели — роль ожидаемых оплат.
            "clicks": int(v),
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


def goal_batches(goal_ids: List[int], size: int = MAX_GOALS_PER_REQUEST) -> List[List[int]]:
    """Цели порциями под лимит Метрики в 20 метрик на запрос.

    Опыт 32577516946: счётчик 98627983 имеет 90 целей, и запрос всех сразу
    отвергается целиком — «Exceeded number of metrics in request, value: 90,
    limit: 20, code 4015». Молча срезать хвост нельзя: это тот же класс дефекта,
    что профиль по «любой цели» — считается не то, а выглядит посчитанным.
    """
    ordered = sorted({int(g) for g in goal_ids})
    return [ordered[i:i + size] for i in range(0, len(ordered), size)]


def fetch_hourly_profile(counter_id: int, date_from: str, date_to: str,
                         goal_ids: List[int]) -> Tuple[List[Dict[str, Any]],
                                                       Dict[str, Any]]:
    """Визиты и достижения ЦЕЛЕВОЙ цели по часам суток + чем именно считали.

    goal_ids обязателен и обязан быть непустым. Раньше здесь стояла
    `ym:s:sumGoalReachesAny` — сумма достижений ЛЮБОЙ цели счётчика, и это
    оказалось не про лиды: на замере 22.08.2026 (счётчик 98627983, окно 90
    дней) 0.38 достижения на визит складывались на ~70 % из микроцелей
    «2 минуты на сайте», «глубина прокрутки», «прокрутка: середина блока»,
    а заявки давали 2 %. Микроцели и заявки расходятся по суткам ПРОТИВОПОЛОЖНО:
    прокрутка ночью 1.17–1.49 к дню, «CRM: новая заявка» — 0.45, «новая сделка» —
    0.41. Расписание, посчитанное по Any, поднимало ставку на 3:00–7:00 (+13,
    +27, +21 %) — то есть на часы, где заявок вдвое меньше.

    ФИЛЬТР РЕКЛАМНОГО ТРАФИКА. Знаменатель и числитель считаются по визитам из
    рекламы, а не по всему сайту: результат накладывается на показы Директа.
    Без фильтра час получал бы коэффициент состава источников — доля SEO и
    прямых заходов ночью выше, потому что реклама в это время откручивается
    меньше. Замер 32579085232: доля рекламы в визитах ночью 0.956–0.984 против
    0.976–0.983 днём, то есть примесь мала, но она есть и смещена в одну
    сторону — ровно как у соседней fetch_campaign_behavior, где фильтр стоял
    с самого начала.

    Числитель — ОДНА цель (pick_lead_goal), а не сумма колонок: цели Директа
    дублируют друг друга, и сумма считает одну заявку дважды.

    Пересечение с целями счётчика делает вызывающий: цель принадлежит одному
    счётчику, и чужой идентификатор в metrics отвергается Метрикой целиком.
    """
    if not goal_ids:
        raise ValueError(
            "fetch_hourly_profile без целей: считать расписание не по чему. "
            "Профиль по «любой цели» — это профиль прокрутки, а не лидов."
        )
    token = os.environ["YM_TOKEN"]
    visits: Dict[str, float] = {}
    reaches: Dict[int, Dict[str, float]] = {}
    for batch in goal_batches(goal_ids):
        part_visits, part_reaches = parse_hourly_by_goal(_metrica_get({
            "ids": counter_id,
            "metrics": hourly_metrics(batch),
            "dimensions": "ym:s:hour",
            "filters": AD_FILTER,
            "date1": date_from,
            "date2": date_to,
            "attribution": ATTRIBUTION,
            "limit": ROW_LIMIT,
            "accuracy": "full",
        }, token), batch)
        # Визиты в каждой порции ОДНИ И ТЕ ЖЕ (это метрика часа, а не цели):
        # сложить их значило бы раздуть знаменатель во столько раз, сколько
        # было порций.
        visits.update(part_visits)
        reaches.update(part_reaches)

    goal_id = pick_lead_goal(reaches)
    if goal_id is None:
        return [], {"goal_id": None, "reaches": 0,
                    "reason": "ни одна цель Директа не достигнута в окне"}
    return profile_rows(visits, reaches[goal_id]), {
        "goal_id": goal_id,
        "reaches": int(sum(reaches[goal_id].values())),
        "goals_offered": len(reaches),
    }


MIN_COUNTER_ACCOUNT_SHARE = 0.5


def resolve_counter_account(campaign_names: Any,
                            login_by_campaign_name: Dict[str, str],
                            min_share: float = MIN_COUNTER_ACCOUNT_SHARE) -> Any:
    """Кабинет Директа, которому принадлежит счётчик. None — привязки нет.

    Зачем. Почасовой профиль накладывается на TimeTargeting КАМПАНИИ, а
    кампания ведёт на ОДИН сайт. Раньше профили трёх счётчиков складывались в
    один и раскатывались на все кабинеты — а счётчики о сутках не согласны:
    проба 32579085232 дала у 98627983 часы 02-05 на уровне 130, у 96526110 те
    же часы на уровне 90. Слитый профиль решал спор объёмом, и кампаниям
    меньшего счётчика доставалось расписание чужого сайта.

    Проба 32579931952 показала, что связь счётчик↔кабинет однозначная:
    98627983 → 93 % имён кампаний одного кабинета, 96526110 → 90 % другого,
    95348914 → 75 % третьего, а четвёртый кабинет (5 кампаний) не принадлежит
    ни одному счётчику. Поэтому привязка выводится из данных каждый прогон, а
    не задаётся константой: кампании переезжают, кабинеты добавляются.

    Порог доли обязателен. Без него кабинет, поймавший одно случайное совпадение
    имени, получил бы чужое расписание — а это ровно тот отказ, который надо
    видеть, а не сглаживать.
    """
    names = [str(n).strip() for n in campaign_names if str(n).strip()]
    if not names:
        return None
    hits: Dict[str, int] = {}
    for name in names:
        login = login_by_campaign_name.get(name)
        if login:
            hits[login] = hits.get(login, 0) + 1
    if not hits:
        return None
    best = max(hits, key=lambda k: (hits[k], k))
    return best if hits[best] / len(names) >= min_share else None


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
