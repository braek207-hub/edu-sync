# -*- coding: utf-8 -*-
"""
probe_ideas_pipeline.py — доезжает ли находка генератора до РЕАЛЬНОЙ таблицы.

Тесты задачи 16а идут на двойнике edu_agent_ideas (tests/conftest.py): он
хранит строки словарём и SQL не исполняет. Значит зелёный гейт ничего не
говорит о том, примет ли настоящая таблица настоящую строку: JSON-колонки,
типы чисел, длина текста, ограничения — всё это живёт в SQL, которого двойник
не видит. Пробник закрывает ровно этот разрыв: связка → генератор → upsert →
чтение обратно из базы.

Кабинет пробника — «__probe_16a__», и он не существует в Директе. Строки
убираются в finally: реестр читает бой, и мусорный кабинет попал бы человеку
на экран предложений.

Только эта таблица и только этот кабинет. Запуск:
    python probe_ideas_pipeline.py
ENV: DATABASE_URL
"""

import json
import sys
from datetime import date

import psycopg2

from sync.agent_e0 import collect_ideas
from sync.agent.ideas import registry
from sync.db import get_connection

ACCOUNT = "__probe_16a__"
CAMPAIGN = "999000001"

POOL = {"paid": 120.0, "deals": 200.0, "connected": 400.0,
        "eff": 900.0, "leads": 1800.0, "clicks": 90_000.0}


def _inputs():
    """Синтетика такта: сегмент, который окупается заметно лучше кампании."""
    return {
        "facts": [{"fact_date": "2026-07-01", "campaign_id": CAMPAIGN,
                   "direction": "vuz", "cost": 10_000.0, "eff_leads": 8}],
        "ladder_section": {
            "window_from": "2026-05-01", "window_to": "2026-07-29",
            "by_object": {CAMPAIGN: {"step": "eff",
                                     "events_by_step": dict(POOL)}},
            "counts": {"by_direction": {"vuz": dict(POOL)},
                       "account": dict(POOL)},
            "avg_check": {CAMPAIGN: 60_000.0},
        },
        "portfolio_section": {"accounts": {ACCOUNT: {
            "lambda": 1.0,
            "moves": {CAMPAIGN: {"direction": "vuz",
                                 "value_per_lead": 1_500.0,
                                 "cost_28d": 300_000.0, "leads_28d": 200,
                                 "limit_binding": True}}}}},
        "sliced_rows": [
            {"campaign_id": CAMPAIGN, "slice_kind": "device",
             "slice_key": "MOBILE", "clicks": 6_000.0, "conversions": 300.0,
             "cost": 90_000.0},
            {"campaign_id": CAMPAIGN, "slice_kind": "device",
             "slice_key": "DESKTOP", "clicks": 6_000.0, "conversions": 40.0,
             "cost": 180_000.0},
        ],
        "query_rows": [], "expansion": [], "demand": {},
        "settings_by_campaign": {CAMPAIGN: {"bidModifiers": {"total": 0,
                                                             "items": []}}},
        "login_by_campaign": {CAMPAIGN: ACCOUNT},
        "direction_by_campaign": {CAMPAIGN: "vuz"},
        "holdout_ids": [], "learning_reset": {}, "quality_drift": {},
        "config": {}, "slice_window_days": 90, "query_window_days": 30,
        "today": date(2026, 8, 27),
    }


def _cleanup() -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM edu_agent_ideas WHERE account = %s",
                        (ACCOUNT,))
            removed = cur.rowcount
        conn.commit()
    return removed


def main() -> int:
    report = {"account": ACCOUNT}
    try:
        # Уборка ПЕРЕД прогоном тоже: упавший прошлый запуск мог оставить
        # строку, и «идея на месте» читалось бы как успех этого прогона.
        report["stale_removed"] = _cleanup()

        summary = collect_ideas(**_inputs())
        report["generated"] = summary["by_source"].get("proven")
        report["bundles"] = summary["bundles"]
        report["failed"] = summary["failed"]

        rows = registry.open_ideas(ACCOUNT)
        report["read_back"] = len(rows)
        if rows:
            row = rows[0]
            subject = row["subject"]
            if isinstance(subject, str):
                subject = json.loads(subject)
            report["row"] = {
                "idea_id": row["idea_id"],
                "source": row["source"],
                "account": row["account"],
                "subject": subject,
                "tier": row["tier"],
                "lane": row["lane"],
                "status": row["status"],
                "test_cost_rub": float(row["test_cost_rub"] or 0),
                "horizon_days": row["horizon_days"],
                "has_action": bool(row.get("action")),
            }
            # Кабинет обязан входить в идентификатор: та же связка в другом
            # кабинете — другая идея (дефект, найденный при проводке).
            report["identity_scoped_to_account"] = (
                row["idea_id"] == registry.idea_id(row["source"], subject,
                                                   ACCOUNT)
                != registry.idea_id(row["source"], subject, "other"))

        report["verdict"] = ("OK" if report["read_back"] == 1
                             and report.get("identity_scoped_to_account")
                             else "FAIL")
    except (psycopg2.Error, KeyError, ValueError, registry.InvalidIdea) as exc:
        report["verdict"] = "FAIL"
        report["error"] = f"{type(exc).__name__}: {exc}"[:400]
    finally:
        try:
            report["cleaned"] = _cleanup()
        except psycopg2.Error as exc:  # noqa: BLE001
            report["cleaned"] = f"{type(exc).__name__}: {exc}"[:200]

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["verdict"] == "OK" and report["cleaned"] == 1 else 1


if __name__ == "__main__":
    sys.exit(main())
