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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List

from sync.agent import db as agent_db
from sync.agent.computed import compute_schedule, compute_segment_modifiers
from sync.agent.facts import assemble_facts
from sync.agent.guard import check_continuity, check_freshness, verdict
from sync.agent.holdout import select_holdout
from sync.agent.metrika import EDU_COUNTERS, fetch_campaign_behavior, fetch_hourly_profile
from sync.agent.mining import mine_quasi_experiments
from sync.agent.objects import build_object_rows, top_queries_by_cost
from sync.agent.power import power_report
from sync.agent.profile import build_profile, campaign_quality, distance_to_profile
from sync.agent.segments import (
    fetch_account_goal_ids,
    fetch_campaign_ids,
    fetch_objects,
    fetch_search_queries,
    fetch_segment_report,
)
from sync.agent.settings_snapshot import build_snapshot_rows
from sync.agent.slices import build_sliced_facts, collapse_tail

DEFAULT_DAYS = 180
# Срезы, структура и поисковые запросы — только за квартал: 90 дней покрывают все
# сезонные фазы, кроме прошлогодней приёмки, а объём таблиц держат в десятках МБ.
SLICE_WINDOW_DAYS = 90
# Поисковые запросы — самая объёмная витрина (450 МБ за 90 дней на 4 кабинета).
# Кандидаты в минус-слова считаются по свежим данным, глубокая история не нужна.
QUERY_WINDOW_DAYS = 30
PROFILE_FEATURES = ["groups_count", "phrases_per_group", "title2_fill_share"]
# Пороги свежести РАЗНЫЕ, потому что источники разной природы.
#
# Расход Директа приезжает своим синком и почти не отстаёт: трое суток без
# новых строк — это уже поломка синка, а не задержка.
DIRECT_MAX_AGE_HOURS = 72
# CRM отстаёт ШТАТНО на 2-4 дня: выгрузка из Битрикса в Google-таблицу идёт
# не каждый день (слова владельца кабинета + замер 21.08.2026). Общий порог в
# 72 часа ронял ВЕСЬ расчёт Э0 на этой норме — то есть защита срабатывала на
# штатной ситуации и просто останавливала работу. Это тот же класс дефекта,
# что вечный RED у гейта витрины сторожа.
#
# Шесть суток — запас поверх наблюдаемого лага, но заметно меньше настоящей
# поломки: 02.08.2026 таблица встала на четверо суток и никто не заметил
# четыре дня (см. sync/data_freshness.py). За неделю тишины падать обязаны.
CRM_MAX_AGE_HOURS = 144
# Оставлено для совместимости чтения старых записей гейта в edu_agent_guard.
HISTORY_MAX_AGE_HOURS = DIRECT_MAX_AGE_HOURS
# Директ ограничивает число одновременно формируемых отчётов на кабинет.
REPORT_WORKERS = 4


def _window(days: int) -> tuple:
    today = date.today()
    return (today - timedelta(days=days)).isoformat(), today.isoformat()


def _direct_clients() -> List[Dict[str, Any]]:
    """Кабинеты с целями. DIRECT_CLIENTS_JSON — список словарей
    {login, goal_ids, sheet_name}, формат задан sync/direct.py::_direct_clients.

    Цели нужны не для красоты: без Goals в запросе Reports API не отдаёт колонку
    Conversions и отвергает FieldNames с ошибкой 8000.

    Логин нормализуется общей функцией agent_db.normalize_login — той же, что
    у движка записи и у самой таблицы: он становится ключом object_id, и
    расхождение нормализаций разводит запись и чтение по разным ключам.
    """
    raw = (os.environ.get("DIRECT_CLIENTS_JSON") or "").strip()
    out: List[Dict[str, Any]] = []
    if raw:
        for item in json.loads(raw):
            if isinstance(item, dict):
                login = agent_db.normalize_login(item.get("login"))
                goals = item.get("goal_ids") or item.get("goals") or []
            else:
                login, goals = agent_db.normalize_login(item), []
            if login:
                out.append({"login": login, "goal_ids": [str(g) for g in goals]})
        if out:
            return out
    login = agent_db.normalize_login(os.environ.get("DIRECT_CLIENT_LOGIN"))
    return [{"login": login, "goal_ids": []}] if login else []


def resolve_goal_ids(client: Dict[str, Any]) -> List[str]:
    """Цели кабинета: явно указанные оператором, иначе выведенные из кабинета.

    Без целей Reports API не отдаёт Conversions, а без Conversions сегментный
    расчёт отказывается работать целиком — на прогоне 32406152097 так отказали
    все двенадцать срезов, и применять автопилоту было нечего. Единственным
    источником целей был секрет DIRECT_CLIENTS_JSON: не проставил руками —
    агент слеп, и заметить это можно было только по причине отказа.

    Поэтому источник по умолчанию — сам кабинет: цели, на которые настроены
    стратегии его кампаний. Значение из секрета остаётся главнее — это явное
    решение оператора сузить набор, и молча подменять его нельзя.
    """
    explicit = [str(g) for g in (client.get("goal_ids") or [])]
    if explicit:
        return explicit
    login = client["login"]
    try:
        found = fetch_account_goal_ids(login)
    except Exception as e:
        # Отказ здесь не должен ронять весь прогон: остальные шаги Э0 (факты,
        # объекты, майнинг) от целей не зависят и обязаны отработать.
        print(f"  [agent_e0] цели кабинета {login} не получены: {e}")
        return []
    if not found:
        print(f"  [agent_e0] у кампаний кабинета {login} не задано ни одной цели "
              f"оптимизации — конверсии по срезам считаться не будут")
    else:
        print(f"  [agent_e0] цели кабинета {login} из стратегий кампаний: {len(found)}")
    return [str(g) for g in found]


def computed_rows_for_job(job: Dict[str, Any]) -> tuple:
    """Результат одного отчёта → (кабинет, строки настроек, причина отказа).

    Кабинет едет ВМЕСТЕ с числами и дальше становится object_id записи. Без
    него строки четырёх кабинетов ложились в один ключ таблицы и перетирали
    друг друга, а движок записи раскатывал выживший набор на всех.

    Конверсионность считается по Conversions самого отчёта Директа. Раньше сюда
    подавались ожидаемые оплаты, размазанные по доле кликов, — из-за чего
    конверсионность всех сегментов среза совпадала и «корректировка по сегменту»
    сегменты не различала. Причина отказа возвращается наружу и печатается в
    отчёте прогона: вырождение обязано быть видно, а не тихо давать нули.
    """
    rows, reason = compute_segment_modifiers(job["rows"])
    return job["login"], rows, reason


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
    # Два вызова с разными порогами: у источников разная норма отставания, и
    # мерить их одной меркой значит либо ронять расчёт на штатном лаге CRM,
    # либо проспать вставший синк Директа.
    checks: List[Dict[str, Any]] = check_freshness(
        {"direct_stats": f"{latest_direct}T00:00:00+00:00" if latest_direct else None},
        now_iso=now_iso,
        max_age_hours=DIRECT_MAX_AGE_HOURS,
    )
    checks += check_freshness(
        {"crm_lead_details": f"{latest_lead}T00:00:00+00:00" if latest_lead else None},
        now_iso=now_iso,
        max_age_hours=CRM_MAX_AGE_HOURS,
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
    queries_from = (date.today() - timedelta(days=QUERY_WINDOW_DAYS)).isoformat()
    clients = [] if skip_direct else _direct_clients()

    # 6-7. Отчёты Директа. ПАРАЛЛЕЛЬНО: каждый отчёт формируется до 10 минут, а их
    # десятки — последовательный обход делал прогон многочасовым (run 31781846178
    # висел 25+ минут и был отменён). Воркеров немного: Директ ограничивает число
    # одновременно формируемых отчётов на кабинет.
    jobs: List[Dict[str, Any]] = []
    for client in clients:
        login, goals = client["login"], resolve_goal_ids(client)
        # Расписания здесь нет: HourOfDay отвергается Reports API (probe 31781715471),
        # почасовой профиль приходит из Метрики (шаг 9).
        #
        # Срезы запрашиваются независимо от объёма оплат: конверсионность сегмента
        # считается по Conversions самого отчёта Директа. Прежний гейт по оплатам
        # аккаунта остался от расчёта, который сегменты не различал.
        for kind in ("device", "gender", "age"):
            jobs.append({"purpose": "computed", "login": login, "goals": goals,
                         "kind": kind, "date_from": cutoff, "by_campaign": False})
        for kind in ("region", "network", "device"):
            jobs.append({"purpose": "sliced", "login": login, "goals": goals,
                         "kind": kind, "date_from": slice_from, "by_campaign": True})

    def _run_job(job: Dict[str, Any]) -> Dict[str, Any]:
        rows = fetch_segment_report(
            job["login"], job["kind"], job["date_from"], date_to,
            by_campaign=job["by_campaign"], goals=job["goals"],
        )
        return {**job, "rows": rows}

    # Вычисленные настройки копятся ПО КАБИНЕТАМ: числа посчитаны по аудитории
    # конкретного кабинета и записываются под его логином (object_id). Общий
    # идентификатор на всех схлопывал четыре набора в один — ключ таблицы
    # совпадал, и в базе оставались числа последнего успевшего кабинета.
    computed_by_account: Dict[str, List[Dict[str, Any]]] = {}
    # Срезы, по которым корректировки НЕ посчитаны, и почему. Молчаливый пропуск
    # неотличим от «данных нет» — а именно так и выглядел дефект размазывания.
    computed_skipped: List[Dict[str, Any]] = []
    sliced_rows: List[Dict[str, Any]] = []
    if jobs:
        with ThreadPoolExecutor(max_workers=REPORT_WORKERS) as pool:
            for done in as_completed([pool.submit(_run_job, j) for j in jobs]):
                job = done.result()
                if job["purpose"] == "computed":
                    login, rows, reason = computed_rows_for_job(job)
                    if reason:
                        computed_skipped.append(
                            {"account": login, "slice": job["kind"], "reason": reason})
                    if rows:
                        computed_by_account.setdefault(login, []).extend(rows)
                else:
                    sliced_rows += collapse_tail(build_sliced_facts(job["rows"], job["kind"]))

    if computed_skipped:
        agent_db.insert_guard_checks([
            {"check_name": f"computed:{sk['account']}:{sk['slice']}", "status": "SKIP",
             "detail": {"reason": sk["reason"]}}
            for sk in computed_skipped
        ])

    computed_count = 0
    for login, rows in computed_by_account.items():
        agent_db.upsert_computed_settings(rows, calc_date=today_iso, object_id=login)
        computed_count += len(rows)
    agent_db.upsert_sliced_facts(sliced_rows)

    # 8. Структура кабинета и поисковые запросы. Только по живым кампаниям окна.
    if "--rebuild-bulk" in sys.argv:
        agent_db.clear_bulk_tables()
    live_campaigns = {str(f["campaign_id"]) for f in facts
                      if f["fact_date"] >= slice_from and (f["cost"] > 0 or f["leads"] > 0)}
    object_rows: List[Dict[str, Any]] = []
    query_rows: List[Dict[str, Any]] = []
    for client in clients:
        login, goals = client["login"], client["goal_ids"]
        # Только ЖИВЫЕ кампании окна, а не весь кабинет за всю историю:
        # fetch_campaign_ids отдавал все 163+ кампании включая архивные, и снимок
        # структуры раздувался до 367k строк / 378 МБ (прогон 31785888375).
        campaign_ids = [cid for cid in fetch_campaign_ids(login) if str(cid) in live_campaigns]
        for level in ("adgroup", "keyword", "ad"):
            object_rows += build_object_rows(
                fetch_objects(login, level, campaign_ids), level, seen_on=today_iso)
        query_rows += fetch_search_queries(login, queries_from, date_to, goals=goals)
    agent_db.upsert_objects(object_rows)
    query_rows = top_queries_by_cost(query_rows)
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

    # 9. Обогащение Метрикой: почасовой профиль (Директ HourOfDay не отдаёт) и
    # поведение по кампаниям — ранний сигнал качества до созревания оплат.
    hourly_rows: List[Dict[str, Any]] = []
    behavior_rows: List[Dict[str, Any]] = []
    if not skip_direct and os.environ.get("YM_TOKEN"):
        for counter in EDU_COUNTERS:
            try:
                hourly_rows += fetch_hourly_profile(counter, cutoff, date_to)
                behavior_rows += fetch_campaign_behavior(counter, slice_from, date_to)
            except Exception as exc:  # счётчик может быть недоступен токену
                agent_db.insert_guard_checks([{
                    "check_name": f"metrika:{counter}", "status": "FAIL",
                    "detail": {"error": f"{type(exc).__name__}: {exc}"[:300]},
                }])

    if hourly_rows:
        # База считается внутри compute_schedule по тем же строкам: внешняя база
        # в чужих единицах — та самая ошибка, что вырождала сегментные корректировки.
        schedule_rows, schedule_reason = compute_schedule(hourly_rows)
        if schedule_reason:
            computed_skipped.append(
                {"account": "*", "slice": "hour", "reason": schedule_reason})
            agent_db.insert_guard_checks([{
                "check_name": "computed:*:hour", "status": "SKIP",
                "detail": {"reason": schedule_reason},
            }])
        # Почасовой профиль посчитан по счётчикам Метрики всего EDU, а не
        # по одному кабинету, но применяется в каждом — пишем его под
        # каждым логином. Хранить его под общим «ничьим» идентификатором
        # нельзя: тогда загрузчик, который читает настройки строго по
        # кабинету, не найдёт расписание ни для одного из них.
        if schedule_rows:
            for client in clients:
                agent_db.upsert_computed_settings(
                    schedule_rows, calc_date=today_iso, object_id=client["login"])
                computed_count += len(schedule_rows)

    # Метрика отдаёт имя кампании, а не Id (probe 31788247020) — резолвим по фактам.
    id_by_name = {str(f.get("campaign_name") or "").strip(): f["campaign_id"]
                  for f in facts if f.get("campaign_name")}
    resolved_behavior = []
    unresolved = 0
    for row in behavior_rows:
        campaign_id = id_by_name.get(row["campaign_name"])
        if not campaign_id:
            unresolved += 1
            continue
        resolved_behavior.append({k: v for k, v in row.items() if k != "campaign_name"}
                                 | {"campaign_id": campaign_id})
    if unresolved:
        agent_db.insert_guard_checks([{
            "check_name": "metrika:name_resolution", "status": "OK",
            "detail": {"unresolved_campaigns": unresolved, "resolved": len(resolved_behavior)},
        }])
    agent_db.upsert_behavior(resolved_behavior, window_from=slice_from, window_to=date_to)

    # 11. Отчёт мощности и фактический объём таблиц.
    report = power_report(list(aggregates.values()))
    sizes = agent_db.table_sizes()
    total_mb = round(sum(int(s["size_bytes"] or 0) for s in sizes) / 1024 / 1024, 1)

    # Граница зрелости CRM и величина отставания — в отчёт каждого прогона.
    # Без них «лидов за последние дни нет» читается как обвал конверсии, а не
    # как «выгрузка ещё не приехала», и разбор уходит не туда.
    crm_lag = (date.today() - date.fromisoformat(latest_lead)).days if latest_lead else None

    print(json.dumps({
        "verdict": "GREEN",
        "crm_through": latest_lead,
        "crm_lag_days": crm_lag,
        "facts_rows": len(facts),
        "sliced_rows": len(sliced_rows),
        "objects": len(object_rows),
        "search_queries": len(query_rows),
        "settings_snapshots": len(snapshot_rows),
        "holdout": len(holdout),
        "quasi_experiments": len(quasi),
        "computed_settings": computed_count,
        "computed_settings_by_account": {k: len(v) for k, v in computed_by_account.items()},
        "computed_settings_skipped": computed_skipped,
        "profile_rows": len(profile_rows),
        "metrika_hourly": len(hourly_rows),
        "metrika_behavior": len(resolved_behavior),
        "power": report,
        "db_total_mb": total_mb,
        "db_tables": [{"t": s["table_name"], "size": s["size"]} for s in sizes],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
