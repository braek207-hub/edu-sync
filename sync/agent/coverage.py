# -*- coding: utf-8 -*-
"""
sync/agent/coverage.py — доля расхода вне видимости агента.

Настройки кампаний агент читает из edu_campaign_settings, которую наполняет
API Директа. Мастер кампаний и часть форматов туда не попадают: замер
25.08.2026 дал 15 % расхода (3,0 млн ₽/мес) мимо витрины. Пока эта зона не
закрыта, каждое число агента обязано нести рядом её размер — доля, посчитанная
по популяции, из которой выпала шестая часть денег, не становится неверной, но
и не имеет права выглядеть полной.

Меряем деньгами, а не числом кампаний: одна невидимая кампания на миллион и
двадцать невидимых по тысяче — разные вещи, и счётчик кампаний их путает.

Чего счётчик НЕ ловит: знаменатель cost_total берётся из edu_agent_facts, то
есть из того же Reports API, что и вся витрина. Слепота меряется ВНУТРИ
витрины. Расход, не попавший в саму витрину, невидим и здесь — доля выйдет
оптимистичной. Сверка суммы витрины с «Общей статистикой» кабинета — отдельный
шаг приёмки (docs/AGENT-DATA-SOURCES.md).
"""

from typing import Any, Dict, Iterable, List, Mapping

SAMPLE_LIMIT = 10


def known_campaign_ids(settings_rows: Any) -> set:
    """Идентификаторы кампаний, настройки которых агент видел.

    Витрину настроек отдают в двух формах: agent_db.load_campaign_settings_raw()
    (sync/agent/db.py:329) возвращает словарь campaign_id -> settings, а тесты и
    внешние вызовы — список строк с полем campaign_id. Обе читаются одинаково.
    """
    if isinstance(settings_rows, Mapping):
        return {str(cid) for cid in settings_rows.keys() if cid}
    known = set()
    for row in settings_rows or []:
        campaign_id = row.get("campaign_id") if isinstance(row, Mapping) else row
        if campaign_id:
            known.add(str(campaign_id))
    return known


def blind_share(cost_by_campaign: Mapping[str, float], settings_rows: Any,
                name_by_campaign: Mapping[str, str] = None) -> Dict[str, Any]:
    """Слепая доля по готовому расходу кампаний.

    Ядро счётчика. Такт расчёта приходит сюда через blind_spend (у него есть
    сырые факты), такт записи — напрямую своим агрегатом расхода: держать в
    двух тактах две реализации одной доли значит однажды напечатать в отчётах
    два разных числа под одним именем.
    """
    known = known_campaign_ids(settings_rows)
    names = name_by_campaign or {}

    total = sum(cost_by_campaign.values())
    blind = {cid: cost for cid, cost in cost_by_campaign.items() if cid not in known}
    blind_cost = sum(blind.values())
    sample: List[Any] = sorted(blind.items(), key=lambda kv: -kv[1])[:SAMPLE_LIMIT]

    return {
        "cost_total": round(total, 2),
        "cost_blind": round(blind_cost, 2),
        "blind_share": round(blind_cost / total, 4) if total > 0 else 0.0,
        "campaigns_total": len(cost_by_campaign),
        "campaigns_blind": len(blind),
        "sample": [{"campaign_id": cid,
                    "campaign_name": names.get(cid, ""),
                    "cost": round(cost, 2)}
                   for cid, cost in sample if cost > 0],
    }


def blind_spend(facts: Iterable[Dict[str, Any]], settings_rows: Any,
                window_from: str, window_to: str) -> Dict[str, Any]:
    """Расход вне витрины настроек за окно — по сырым фактам."""
    cost_by_campaign: Dict[str, float] = {}
    name_by_campaign: Dict[str, str] = {}
    for row in facts:
        day = str(row.get("fact_date"))[:10]
        if day < window_from or day > window_to:
            continue
        campaign_id = str(row["campaign_id"])
        cost_by_campaign[campaign_id] = (cost_by_campaign.get(campaign_id, 0.0)
                                         + float(row.get("cost") or 0.0))
        if row.get("campaign_name"):
            name_by_campaign[campaign_id] = str(row["campaign_name"])

    # Окно едет вместе с числом: такт расчёта печатает долю за два разных
    # окна, и без подписи 30 % и 14 % выглядят противоречием, а не разными
    # вопросами.
    out = blind_share(cost_by_campaign, settings_rows, name_by_campaign)
    out["window"] = [window_from, window_to]
    return out
