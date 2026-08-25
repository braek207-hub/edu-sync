# -*- coding: utf-8 -*-
"""
sync/agent/headroom.py — недобор трафика кампании.

Объём трафика Директа (AvgTrafficVolume, 0–100) показывает, какую долю
доступного трафика позиции получает объявление. 100 — берём всё, что даёт
позиция; 45 — меньше половины. Кампания с низким объёмом НЕ насыщена
независимо от того, что говорит кривая насыщения: расход упирается не в
исчерпанный спрос, а в ставку.

Почему не «доля выкупа». Колонка edu_agent_facts.auction_win_share пуста, и
наполнить её нечем: боевой прогон probe_traffic_headroom (Actions run
32855656868, docs/AGENT-DATA-SOURCES.md) получил FIELD_UNKNOWN на все три
кандидата (ImpressionShare, SearchImpressionShare, AuctionWinShare) —
таких полей у Reports API нет. Исторические значения приходили выгрузкой
интерфейса эпохи GAS. Объём трафика — то, что API отдаёт сегодня; это другая
величина, и называть её долей выкупа нельзя.

Где вердикт вообще возможен. Тот же замер: 85 кампаний, 13,5 млн показов,
покрытие объёмом трафика 1.0 в КАЖДОМ разрезе — пустого поля в витрине нет.
Зато у трёх кампаний «только сети» объём вырожден: ровно 100.0 у всех. Это
не «выкуплен весь трафик», а «величина не измеряется, отдаётся константа».
Оставить их в расчёте значило бы навсегда записать сетям headroom_share = 0,
то есть выдать незнание за знание «расти некуда». Поэтому содержательный
вердикт выдаётся ТОЛЬКО кампаниям, показывающимся только на поиске; сетям и
кампаниям, которых нет в витрине настроек (15 из 85, 22 % показов — то же
пересечение со слепой зоной расхода, coverage.blind_spend), — «неопределённо».

Здесь считается ТОЛЬКО признак «есть куда расти». Решение «долить» принимает
портфель (portfolio.py) по экономике: недобор трафика — не причина тратить,
а причина не считать кампанию упершейся в потолок.
"""

from typing import Any, Dict, List, Optional

FULL_VOLUME = 100.0

# Порог наблюдаемости. Ниже — среднее по объёму трафика собрано с горстки
# аукционов и скачет сильнее, чем измеряемая величина. 5000 показов за окно —
# примерно 180 показов в день, ниже этого кампании в EDU живут дни-обрывки.
MIN_IMPRESSIONS = 5_000

# Доля показов окна, пришедшихся на дни с НЕНУЛЕВЫМ объёмом трафика.
# Страховка на случай, если поле начнёт приходить пустым: sync/direct.py:117
# (to_num_gas) превращает прочерк в ноль, и признак «поля не было» до витрины
# не доезжает вовсе — ноль неотличим от измеренного нуля. По замеру
# 2026-08-25 покрытие равно 1.0 во всех разрезах, включая сетевые кампании,
# так что сегодня этот порог не отсекает ничего; сети отсекаются не им, а
# правилом «вердикт только для поиска».
MIN_VOLUME_COVERAGE = 0.8

# Границы вердикта. 70 — ниже этого недобор больше трети, и это уже повод
# усомниться в «насыщении». 90 — выше этого добирать почти нечего, и разница
# с 100 съедается округлением позиций.
ROOM_BELOW_VOLUME = 70.0
BOUGHT_OUT_VOLUME = 90.0

# Стратегия канала, означающая «в этом канале кампания не показывается»
# (константа API Директа в снимках стратегий: Search/Network
# BiddingStrategyType = SERVING_OFF).
SERVING_OFF = "SERVING_OFF"

SEARCH_ONLY = "только поиск"
NETWORK_ONLY = "только сети"
SEARCH_AND_NETWORK = "поиск+сети"
UNKNOWN_PLACEMENT = "неизвестно"

VERDICT_ROOM = "есть куда расти"
VERDICT_BOUGHT_OUT = "выкуплен"
VERDICT_UNDETERMINED = "неопределённо"

# Причины «неопределённо» — каждая своя строка: «мы не мерили» и «померили,
# но мало» ведут к разным следующим шагам, и один общий вердикт без причины
# прятал бы эту разницу.
REASON_NOT_SEARCH = (
    "объём трафика осмыслен только на поиске: у сетевых кампаний он приходит "
    "константой 100 (замер 2026-08-25, docs/AGENT-DATA-SOURCES.md)"
)
REASON_NO_SETTINGS = (
    "кампании нет в витрине настроек — где она показывается, неизвестно, "
    "а значит неизвестно и что означает её объём трафика"
)
REASON_LOW_COVERAGE = (
    "объём трафика измерен меньше чем на {share:.0%} показов окна: среднее "
    "посчитано по другим дням, чем показы"
)
REASON_FEW_IMPRESSIONS = (
    "показов за окно меньше {limit}: среднее по объёму трафика скачет сильнее "
    "измеряемой величины"
)
REASON_BETWEEN = (
    "объём трафика между порогами {room} и {bought}: ни недобора, ни выкупа"
)


def placement_mode(search_type: Optional[str],
                   network_type: Optional[str]) -> str:
    """Где кампания показывается — по типам стратегий обоих каналов.

    Повторяет probe_traffic_headroom.py:85 (та же функция, тот же источник):
    правило чтения настроек обязано быть одно, иначе разрез замера и разрез
    расчёта разъедутся молча. Строки без настроек остаются неизвестными:
    приписать им поиск значило бы выдать незнание за знание.
    """
    search_on = bool(search_type) and search_type != SERVING_OFF
    network_on = bool(network_type) and network_type != SERVING_OFF
    if search_on and network_on:
        return SEARCH_AND_NETWORK
    if search_on:
        return SEARCH_ONLY
    if network_on:
        return NETWORK_ONLY
    return UNKNOWN_PLACEMENT


def placement_modes(settings_by_campaign: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """Размещение по кампаниям из витрины edu_campaign_settings.

    Вход — то, что отдаёт agent_db.load_campaign_settings_raw(): СЛОВАРЬ
    {campaign_id: settings}, а не список строк. Форма settings —
    sync/edu_direct_settings.py:632: strategy.search / strategy.network с
    biddingStrategyType внутри.
    """
    out: Dict[str, str] = {}
    for campaign_id, settings in (settings_by_campaign or {}).items():
        strategy = (settings or {}).get("strategy") or {}
        search = strategy.get("search") or {}
        network = strategy.get("network") or {}
        out[str(campaign_id)] = placement_mode(
            search.get("biddingStrategyType") if isinstance(search, dict) else None,
            network.get("biddingStrategyType") if isinstance(network, dict) else None)
    return out


def traffic_headroom(facts: List[Dict[str, Any]],
                     window_from: str, window_to: str,
                     placement_by_campaign: Dict[str, str]) -> Dict[str, Dict[str, Any]]:
    """Недобор трафика по кампаниям за окно.

    Средний объём взвешивается ПОКАЗАМИ, а не днями: день с тысячей показов
    и день с сотней тысяч — не равноправные наблюдения одной величины.

    placement_by_campaign обязателен и без значения по умолчанию: кампания,
    про которую неизвестно, где она показывается, вердикта не получает, и
    «забыли передать» обязано выглядеть так же, как «не знаем», а не как
    молчаливое разрешение судить обо всех.
    """
    totals: Dict[str, Dict[str, float]] = {}
    for row in facts:
        day = str(row.get("fact_date"))[:10]
        if day < window_from or day > window_to:
            continue
        impressions = int(row.get("impressions") or 0)
        if impressions <= 0:
            continue
        volume = float(row.get("avg_traffic_vol") or 0.0)
        slot = totals.setdefault(str(row["campaign_id"]),
                                 {"impressions": 0.0, "weighted": 0.0,
                                  "cost": 0.0, "with_volume": 0.0})
        slot["impressions"] += impressions
        slot["weighted"] += volume * impressions
        slot["cost"] += float(row.get("cost") or 0.0)
        if volume > 0:
            slot["with_volume"] += impressions

    out: Dict[str, Dict[str, Any]] = {}
    for campaign_id, slot in totals.items():
        impressions = int(slot["impressions"])
        if impressions <= 0:
            continue
        placement = placement_by_campaign.get(campaign_id, UNKNOWN_PLACEMENT)
        coverage = slot["with_volume"] / impressions
        volume = slot["weighted"] / impressions

        # Порядок проверок — от «величины нет» к «величина есть, но слаба»:
        # непригодная к чтению величина наружу числом не отдаётся вовсе.
        if placement == UNKNOWN_PLACEMENT:
            measurable, reason = False, REASON_NO_SETTINGS
        elif placement != SEARCH_ONLY:
            measurable, reason = False, REASON_NOT_SEARCH
        elif coverage < MIN_VOLUME_COVERAGE:
            measurable = False
            reason = REASON_LOW_COVERAGE.format(share=MIN_VOLUME_COVERAGE)
        elif impressions < MIN_IMPRESSIONS:
            measurable = True
            reason = REASON_FEW_IMPRESSIONS.format(limit=MIN_IMPRESSIONS)
        else:
            measurable, reason = True, ""

        if not measurable or reason:
            verdict = VERDICT_UNDETERMINED
        elif volume < ROOM_BELOW_VOLUME:
            verdict = VERDICT_ROOM
        elif volume >= BOUGHT_OUT_VOLUME:
            verdict = VERDICT_BOUGHT_OUT
        else:
            verdict = VERDICT_UNDETERMINED
            reason = REASON_BETWEEN.format(room=ROOM_BELOW_VOLUME,
                                           bought=BOUGHT_OUT_VOLUME)

        out[campaign_id] = {
            # Интерпретируемое среднее. None — мерить нельзя: не поиск, нет
            # настроек, поле пришло не на всех показах.
            "traffic_volume": round(volume, 2) if measurable else None,
            # Что реально пришло из API — для разбора отчёта: у сетевых это
            # ровно 100, и видеть это число полезно, пользоваться им нельзя.
            "traffic_volume_raw": round(volume, 2),
            "headroom_share": (round(max(0.0, FULL_VOLUME - volume) / FULL_VOLUME, 4)
                               if measurable else None),
            "volume_coverage": round(coverage, 4),
            "placement": placement,
            "impressions": impressions,
            "cost": round(slot["cost"], 2),
            "verdict": verdict,
            "reason": reason,
        }
    return out


def computed_rows(section: Dict[str, Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Строки edu_agent_computed_settings по объектам.

    support_n — показы: сила этого числа измеряется показами, а не лидами,
    и потребитель обязан видеть, на чём оно стоит.

    Кампании без пригодной величины строк не получают вовсе: записать сети
    «объём 100, недобор 0» значит положить в витрину число, которого никто
    не мерил, — и любой её читатель примет его за замер.
    """
    out: Dict[str, List[Dict[str, Any]]] = {}
    for campaign_id, row in section.items():
        if row.get("headroom_share") is None:
            continue
        out[str(campaign_id)] = [
            {"setting_kind": "headroom", "setting_key": "traffic_volume",
             "value": row["traffic_volume"], "raw_value": row["traffic_volume"],
             "support_n": row["impressions"], "rel_error": 0.0},
            {"setting_kind": "headroom", "setting_key": "headroom_share",
             "value": row["headroom_share"], "raw_value": row["traffic_volume"],
             "support_n": row["impressions"], "rel_error": 0.0},
        ]
    return out
