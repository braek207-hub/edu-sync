# -*- coding: utf-8 -*-
"""
probe_beta_readiness.py — готов ли агент к бете НА САМОМ ДЕЛЕ.

Протокол беты (docs/AGENT-BETA-PROTOCOL.md) перечисляет, что должно
работать. Этот скрипт спрашивает об этом живую систему, а не документ:
схему — у базы, настройки — тем же разборщиком, которым их читает прогон
записи, историю тактов — у чёрного ящика, права записи — у API Директа.

Проверка read-only. Единственный записывающий вызов идёт по заведомо
несуществующему идентификатору: ошибка уровня элемента подтверждает
право писать, ничего не меняя в кабинете.

ENV: DATABASE_URL, DIRECT_TOKEN, DIRECT_CLIENTS_JSON
"""

import json
import os
from datetime import date
from typing import Any, Dict, List

import requests

from sync.agent import config as agent_config
from sync.agent import db as agent_db
from sync.agent.gate import data_gate
from sync.agent.writer import db as writer_db

PROD = "https://api.direct.yandex.com/json/v5"
NONEXISTENT_ID = 999_999_999
# 53/54/152 — нет доступа к API, 513 — логин не подключён к приложению.
_RIGHTS_CODES = {53, 54, 152, 513}

REQUIRED_TABLES = (
    "edu_agent_runs", "edu_agent_rejects", "edu_agent_actions",
    "edu_agent_experiments", "edu_agent_config", "edu_agent_facts",
)

# Реестр гипотез: без этих колонок ставка не живёт как сущность, и цикл
# «запустил → замерил → закрыл» держать негде.
REGISTRY_COLUMNS = (
    "status", "stake_rub", "horizon_days", "success_criterion",
    "red_line", "idempotency_key",
)

# Журнал действий: наблюдение и откат.
ACTION_COLUMNS = (
    "applied_at", "rolled_back_at", "harmful_verdict_at",
    "observation_verdict", "observation_closed_at", "risk_rub",
)

STAGES = ("e0", "e1", "watchdog", "drift", "review", "config")

# Протокол беты, раздел «Настройки на бету».
PROTOCOL_CONFIG = {"autonomy": "full", "target_romi": 2.0}


def _columns(table: str) -> set:
    rows = writer_db._fetch(
        "SELECT column_name FROM information_schema.columns "
        " WHERE table_schema = 'public' AND table_name = %(t)s",
        {"t": table},
    )
    return {r["column_name"] for r in rows}


def check_schema() -> Dict[str, Any]:
    rows = writer_db._fetch(
        "SELECT table_name FROM information_schema.tables "
        " WHERE table_schema = 'public' AND table_name = ANY(%(names)s)",
        {"names": list(REQUIRED_TABLES)},
    )
    present = {r["table_name"] for r in rows}
    exp = _columns("edu_agent_experiments") if "edu_agent_experiments" in present else set()
    act = _columns("edu_agent_actions") if "edu_agent_actions" in present else set()
    return {
        "tables_missing": sorted(set(REQUIRED_TABLES) - present),
        "registry_columns_missing": sorted(set(REGISTRY_COLUMNS) - exp),
        "action_columns_missing": sorted(set(ACTION_COLUMNS) - act),
    }


def check_config() -> Dict[str, Any]:
    try:
        stored = agent_db.load_agent_config()
        active = agent_config.resolve(stored.get("preset"), stored.get("overrides"))
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}
    mismatch = {k: {"protocol": v, "actual": active.get(k)}
                for k, v in PROTOCOL_CONFIG.items() if active.get(k) != v}
    return {"preset": stored.get("preset"), "overrides": stored.get("overrides"),
            "active": active, "protocol_mismatch": mismatch}


def check_runs(days: int = 14) -> Dict[str, Any]:
    rows = writer_db._fetch(
        """
        SELECT DISTINCT ON (stage)
               stage, started_at, verdict, mode,
               round(extract(epoch FROM now() - started_at) / 3600.0, 1) AS age_hours
          FROM edu_agent_runs
         ORDER BY stage, started_at DESC
        """,
        {},
    )
    last = {r["stage"]: r for r in rows}
    counts = writer_db._fetch(
        """
        SELECT stage, mode, COUNT(*) AS cnt
          FROM edu_agent_runs
         WHERE started_at >= now() - make_interval(days => %(days)s)
         GROUP BY stage, mode
        """,
        {"days": days},
    )
    by_stage: Dict[str, Dict[str, int]] = {}
    for r in counts:
        by_stage.setdefault(r["stage"], {})[str(r["mode"])] = int(r["cnt"])
    out: Dict[str, Any] = {}
    for stage in STAGES:
        row = last.get(stage)
        out[stage] = {
            "last": str(row["started_at"]) if row else None,
            "age_hours": float(row["age_hours"]) if row else None,
            "verdict": row["verdict"] if row else None,
            "verdict_is_null": bool(row) and row["verdict"] is None,
            "modes_14d": by_stage.get(stage, {}),
            "runs_14d": sum(by_stage.get(stage, {}).values()),
        }
    return out


def check_actions(days: int = 30) -> Dict[str, Any]:
    rows = writer_db._fetch(
        """
        SELECT status, COUNT(*) AS cnt
          FROM edu_agent_actions
         WHERE created_at >= now() - make_interval(days => %(days)s)
         GROUP BY status ORDER BY 2 DESC
        """,
        {"days": days},
    )
    # Главный вопрос этапа 1: доезжала ли запись до кабинета хоть раз.
    live = writer_db._fetch(
        """
        SELECT COUNT(*) AS cnt, MAX(applied_at) AS last
          FROM edu_agent_actions
         WHERE status IN ('applied', 'stale', 'rolled_back')
        """,
        {},
    )[0]
    obs = writer_db._fetch(
        """
        SELECT coalesce(observation_verdict, 'ещё наблюдаем') AS v, COUNT(*) AS cnt
          FROM edu_agent_actions
         WHERE status IN ('applied', 'stale', 'rolled_back')
         GROUP BY 1 ORDER BY 2 DESC
        """,
        {},
    )
    return {
        "window_days": days,
        "by_status_30d": {str(r["status"]): int(r["cnt"]) for r in rows},
        "live_writes_total": int(live["cnt"] or 0),
        "live_writes_last": str(live["last"]) if live["last"] else None,
        "observation_verdicts": {str(r["v"]): int(r["cnt"]) for r in obs},
    }


def check_rejects(days: int = 7) -> List[Dict[str, Any]]:
    rows = writer_db._fetch(
        """
        SELECT reason, COUNT(*) AS cnt, round(sum(cost_rub)::numeric, 0) AS cost
          FROM edu_agent_rejects
         WHERE created_at >= now() - make_interval(days => %(days)s)
         GROUP BY reason ORDER BY 2 DESC LIMIT 12
        """,
        {"days": days},
    )
    return [{"reason": r["reason"], "count": int(r["cnt"]),
             "cost_rub": float(r["cost"] or 0)} for r in rows]


def check_hypotheses() -> Dict[str, Any]:
    rows = writer_db._fetch(
        """
        SELECT coalesce(status, 'посмертная запись') AS s, COUNT(*) AS cnt
          FROM edu_agent_experiments GROUP BY 1 ORDER BY 2 DESC
        """,
        {},
    )
    return {str(r["s"]): int(r["cnt"]) for r in rows}


def check_gate() -> Dict[str, Any]:
    try:
        gate = data_gate(date.today())
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}
    return {"status": gate.get("status"), "reason": gate.get("reason"),
            "latest_fact_date": gate.get("latest_fact_date"),
            "failed_checks": [c.get("name") for c in (gate.get("checks") or [])
                              if c.get("status") == "FAIL"]}


def check_holdout() -> Dict[str, Any]:
    try:
        return {"campaigns": len(agent_db.load_holdout_ids())}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}


def check_write_rights() -> List[Dict[str, Any]]:
    """Право писать проверяется у ПОЛУЧАТЕЛЯ — у API, а не по наличию токена."""
    raw = (os.environ.get("DIRECT_CLIENTS_JSON") or "").strip()
    logins = [str(i["login"]) for i in json.loads(raw or "[]")
              if isinstance(i, dict) and str(i.get("login", "")).strip()]
    out: List[Dict[str, Any]] = []
    for login in logins:
        resp = requests.post(
            f"{PROD}/bidmodifiers",
            data=json.dumps({"method": "set", "params": {
                "BidModifiers": [{"Id": NONEXISTENT_ID, "BidModifier": 110}]}},
                ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {os.environ['DIRECT_TOKEN']}",
                     "Client-Login": login, "Accept-Language": "ru",
                     "Content-Type": "application/json; charset=utf-8"},
            timeout=60,
        )
        resp.encoding = "utf-8"
        try:
            body = resp.json()
        except ValueError:
            body = {}
        code = (body.get("error") or {}).get("error_code")
        if code in _RIGHTS_CODES or resp.status_code == 403:
            verdict = f"НЕТ ПРАВ[{code}]"
        elif code is not None:
            verdict = f"ОТКАЗ[{code}]"
        else:
            # Отказ уровня элемента («объект не найден») = право писать есть.
            verdict = "WRITE_OK"
        out.append({"account": login, "verdict": verdict})
    return out


def verdict(report: Dict[str, Any]) -> Dict[str, Any]:
    blockers: List[str] = []
    warnings: List[str] = []

    sch = report["schema"]
    if sch["tables_missing"]:
        blockers.append("нет таблиц: " + ", ".join(sch["tables_missing"]))
    if sch["registry_columns_missing"]:
        blockers.append("реестр гипотез без колонок: "
                        + ", ".join(sch["registry_columns_missing"]))
    if sch["action_columns_missing"]:
        blockers.append("журнал действий без колонок наблюдения: "
                        + ", ".join(sch["action_columns_missing"]))

    cfg = report["config"]
    if cfg.get("error"):
        blockers.append("настройки не читаются: " + cfg["error"])
    for key, pair in (cfg.get("protocol_mismatch") or {}).items():
        warnings.append(f"настройка {key}: протокол требует {pair['protocol']}, "
                        f"стоит {pair['actual']}")

    gate = report["data_gate"]
    if gate.get("error"):
        blockers.append("гейт данных не считается: " + gate["error"])
    elif str(gate.get("status")) != "GREEN":
        blockers.append(f"гейт данных {gate.get('status')}: {gate.get('reason')}")

    for stage, row in report["runs"].items():
        if row["runs_14d"] == 0:
            blockers.append(f"такт {stage} за 14 дней не запускался ни разу")
        elif row["verdict_is_null"]:
            warnings.append(f"такт {stage}: последний прогон без вердикта в журнале")

    bad = [r for r in report["write_rights"] if r["verdict"] != "WRITE_OK"]
    if bad:
        blockers.append("нет прав записи: "
                        + ", ".join(f"{r['account']} {r['verdict']}" for r in bad))
    if not report["write_rights"]:
        blockers.append("список кабинетов пуст (DIRECT_CLIENTS_JSON)")

    if report["holdout"].get("campaigns", 0) == 0:
        blockers.append("заповедник пуст — сравнивать эффект будет не с чем")

    if report["actions"]["live_writes_total"] == 0:
        warnings.append("боевой записи в кабинет не было ни разу — это предмет "
                        "этапа 1 беты, а не дефект готовности")

    return {"ready": not blockers, "blockers": blockers, "warnings": warnings}


def main() -> int:
    report: Dict[str, Any] = {
        "today": date.today().isoformat(),
        "schema": check_schema(),
        "config": check_config(),
        "data_gate": check_gate(),
        "runs": check_runs(),
        "actions": check_actions(),
        "rejects_7d": check_rejects(),
        "hypotheses": check_hypotheses(),
        "holdout": check_holdout(),
        "write_rights": check_write_rights(),
    }
    report["verdict"] = verdict(report)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report["verdict"]["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
