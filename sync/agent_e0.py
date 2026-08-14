# -*- coding: utf-8 -*-
"""
sync/agent_e0.py — прогон Э0 автопилота: фундамент и майнинг истории.

Э0 НИЧЕГО не пишет в Яндекс Директ. Только читает и складывает результат в БД.
Движок записи появляется на Э1.

Порядок: гейт данных → факты → заповедник → квазиэксперименты → вычисляемые
настройки → срезы → структура → снимок настроек → профиль → отчёт мощности.
Красный гейт останавливает прогон: работать на битых данных хуже, чем не работать.

Запуск:  python -m sync.agent_e0 [--days 180] [--skip-direct]
ENV:     DATABASE_URL, DIRECT_TOKEN, DIRECT_CLIENTS_JSON
"""

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List

from sync.agent import db as agent_db
from sync.agent.computed import compute_segment_modifiers
from sync.agent.facts import assemble_facts
from sync.agent.guard import check_continuity, check_freshness, verdict
from sync.agent.holdout import select_holdout
from sync.agent.mining import mine_quasi_experiments
from sync.agent.objects import build_object_rows
from sync.agent.power import power_report
from sync.agent.profile import build_profile, campaign_quality, distance_to_profile
from sync.agent.segments import fetch_objects, fetch_search_queries, fetch_segment_report
from sync.agent.settings_snapshot import build_snapshot_rows
from sync.agent.slices import build_sliced_facts, collapse_tail

DEFAULT_DAYS = 180
# Срезы, структура и поисковые запросы — только за квартал: 90 дней покрывают все
# сезонные фазы, кроме прошлогодней приёмки, а объём таблиц держат в десятках МБ.
SLICE_WINDOW_DAYS = 90
PROFILE_FEATURES = ["groups_count", "phrases_per_group", "title2_fill_share"]
# Окно истории, а не оперативный контур: дневной лаг синков допустим.
HISTORY_MAX_AGE_HOURS = 72


def _window(days: int) -> tuple:
    today = date.today()
    return (today - timedelta(days=days)).isoformat(), today.isoformat()


def _direct_clients() -> List[Dict[str, Any]]:
    """Кабинеты с целями. DIRECT_CLIENTS_JSON — список словарей
    {login, goal_ids, sheet_name}, формат задан sync/direct.py::_direct_clients.

    Цели нужны не для красоты: без Goals в запросе Reports API не отдаёт колонку
    Conversions и отвергает FieldNames с ошибкой 8000.
    """
    raw = (os.environ.get("DIRECT_CLIENTS_JSON") or "").strip()
    out: List[Dict[str, Any]] = []
    if raw:
        for item in json.loads(raw):
            if isinstance(item, dict):
                login = str(item.get("login", "")).strip()
                goals = item.get("goal_ids") or item.get("goals") or []
            else:
                login, goals = str(item).strip(), []
            if login:
                out.append({"login": login, "goal_ids": [str(g) for g in goals]})
        if out:
            return out
    login = (os.environ.get("DIRECT_CLIENT_LOGIN") or "").strip()
    return [{"login": login, "goal_ids": []}] if login else []


def _attach_expected_payments(
    segment_rows: List[Dict[str, Any]], total_expected: float
) -> List[Dict[str, Any]]:
    """Reports API не отдаёт p_pay — переносим ожидаемые оплаты на сегмент по доле кликов.

    Приближение осознанное: точная привязка лида к сегменту требует связки лид↔визит,
    которой на Э0 нет. Доля кликов — консервативная оценка, а сжатие к базе не даёт ей
    развернуться в большую корректировку.
    """
    total_clicks = sum(int(r.get("clicks") or 0) for r in segment_rows)
    out: List[Dict[str, Any]] = []
    for r in segment_rows:
        clicks = int(r.get("clicks") or 0)
        share = (clicks / total_clicks) if total_clicks else 0.0
        out.append({
            "segment_kind": r["segment_kind"],
            "segment_key": r["segment_key"],
            "clicks": clicks,
            "leads": 0,
            "sum_p_pay": total_expected * share,
        })
    return out


def main() -> int:
    days = DEFAULT_DAYS
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])
    skip_direct = "--skip-direct" in sys.argv
    date_from, date_to = _window(days)
    today_iso = date.today().isoformat()

    agent_db.ensure_agent_tables()

    direct_rows = agent_db.load_direct_rows(date_from, date_to)
    lead_rows = agent_db.load_lead_rows(date_from, date_to)
    score_rows = agent_db.load_score_rows(date_from, date_to)

    # 1. Гейт качества данных.
    now_iso = datetime.now(timezone.utc).isoformat()
    latest_direct = max((str(r["date"]) for r in direct_rows), default=None)
    latest_lead = max((str(r["created_date"]) for r in lead_rows), default=None)
    checks: List[Dict[str, Any]] = check_freshness(
        {
            "direct_stats": f"{latest_direct}T00:00:00+00:00" if latest_direct else None,
            "crm_lead_details": f"{latest_lead}T00:00:00+00:00" if latest_lead else None,
        },
        now_iso=now_iso,
        max_age_hours=HISTORY_MAX_AGE_HOURS,
    )
    checks.append(check_continuity(
        sorted({str(r["date"]) for r in direct_rows}),
        expected_last=latest_direct or date_to,
    ))
    agent_db.insert_guard_checks(checks)

    if verdict(checks) == "RED":
        print(json.dumps({"verdict": "RED", "checks": checks}, ensure_ascii=False, indent=2))
        return 1

    # 2. Факты.
    facts = assemble_facts(direct_rows, lead_rows, score_rows)
    agent_db.upsert_facts(facts)

    # 3. Агрегаты последних 30 дней — для заповедника и отчёта мощности.
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    recent = [f for f in facts if f["fact_date"] >= cutoff]
    aggregates: Dict[str, Dict[str, Any]] = {}
    for f in recent:
        agg = aggregates.setdefault(f["campaign_id"], {
            "campaign_id": f["campaign_id"],
            "direction": f.get("direction"),
            "cost_30d": 0.0, "leads_30d": 0, "eff_leads_30d": 0, "sum_p_pay_30d": 0.0,
        })
        agg["cost_30d"] += f["cost"]
        agg["leads_30d"] += f["leads"]
        agg["eff_leads_30d"] += f["eff_leads"]
        agg["sum_p_pay_30d"] += f["sum_p_pay"]

    # 4. Заповедник. Состав держится весь сезон — пересборка только по явному флагу.
    if "--rebuild-holdout" in sys.argv:
        agent_db.clear_holdout()
    holdout = select_holdout(list(aggregates.values()))
    agent_db.upsert_holdout(holdout, included_on=today_iso)

    # 5. Квазиэксперименты → блокнот.
    quasi = mine_quasi_experiments(facts)
    agent_db.upsert_experiments(quasi)

    slice_from = (date.today() - timedelta(days=SLICE_WINDOW_DAYS)).isoformat()
    clients = [] if skip_direct else _direct_clients()

    # 6. Вычисляемые настройки. Считаем и складываем — применение на Э1.
    base_clicks = sum(f["clicks"] for f in recent)
    base_expected = sum(f["sum_p_pay"] for f in recent)
    base_conv = (base_expected / base_clicks) if base_clicks else 0.0
    computed_rows: List[Dict[str, Any]] = []
    if base_conv > 0:
        for client in clients:
            login, goals = client["login"], client["goal_ids"]
            # Расписания здесь нет: HourOfDay отвергается Reports API (probe 31781715471),
            # почасовой расход придёт из Метрики на Э1.
            for kind in ("device", "gender", "age"):
                segment_rows = fetch_segment_report(login, kind, cutoff, date_to, goals=goals)
                enriched = _attach_expected_payments(segment_rows, base_expected)
                computed_rows += compute_segment_modifiers(enriched, base_conv)
    agent_db.upsert_computed_settings(computed_rows, calc_date=today_iso)

    # 7. Срезы по кампаниям — окно 90 дней, недельная грань, хвост в other.
    sliced_rows: List[Dict[str, Any]] = []
    for client in clients:
        login, goals = client["login"], client["goal_ids"]
        for kind in ("region", "network", "device"):
            report_rows = fetch_segment_report(
                login, kind, slice_from, date_to, by_campaign=True, goals=goals)
            sliced_rows += collapse_tail(build_sliced_facts(report_rows, kind))
    agent_db.upsert_sliced_facts(sliced_rows)

    # 8. Структура кабинета и поисковые запросы.
    object_rows: List[Dict[str, Any]] = []
    query_rows: List[Dict[str, Any]] = []
    for client in clients:
        login, goals = client["login"], client["goal_ids"]
        for level in ("adgroup", "keyword", "ad"):
            object_rows += build_object_rows(fetch_objects(login, level), level, seen_on=today_iso)
        query_rows += fetch_search_queries(login, slice_from, date_to, goals=goals)
    agent_db.upsert_objects(object_rows)
    agent_db.upsert_search_queries(query_rows)

    # 9. Снимок настроек.
    snapshot_rows = build_snapshot_rows(agent_db.load_campaign_settings_raw(), seen_on=today_iso)
    agent_db.upsert_settings_snapshot(snapshot_rows)

    # 10. Профиль успеха и дистанции (после снимка структуры — признаки берутся оттуда).
    feature_rows = agent_db.load_campaign_features(date_from, date_to)
    profile = build_profile(feature_rows, PROFILE_FEATURES)
    profile_rows: List[Dict[str, Any]] = []
    if profile:
        qualities = sorted(
            (campaign_quality(r) if campaign_quality(r) is not None else float("inf"), r["campaign_id"])
            for r in feature_rows
        )
        quartile_by_campaign = {
            cid: min(int(i * 4 / max(len(qualities), 1)) + 1, 4)
            for i, (_, cid) in enumerate(qualities)
        }
        for row in feature_rows:
            distance, gaps = distance_to_profile(row, profile, PROFILE_FEATURES)
            profile_rows.append({
                "campaign_id": row["campaign_id"],
                "distance": distance,
                "gaps": gaps,
                "quartile": quartile_by_campaign.get(row["campaign_id"], 4),
            })
    agent_db.upsert_profile(profile_rows, calc_date=today_iso)

    # 11. Отчёт мощности и фактический объём таблиц.
    report = power_report(list(aggregates.values()))
    sizes = agent_db.table_sizes()
    total_mb = round(sum(int(s["size_bytes"] or 0) for s in sizes) / 1024 / 1024, 1)

    print(json.dumps({
        "verdict": "GREEN",
        "facts_rows": len(facts),
        "sliced_rows": len(sliced_rows),
        "objects": len(object_rows),
        "search_queries": len(query_rows),
        "settings_snapshots": len(snapshot_rows),
        "holdout": len(holdout),
        "quasi_experiments": len(quasi),
        "computed_settings": len(computed_rows),
        "profile_rows": len(profile_rows),
        "power": report,
        "db_total_mb": total_mb,
        "db_tables": [{"t": s["table_name"], "size": s["size"]} for s in sizes],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
