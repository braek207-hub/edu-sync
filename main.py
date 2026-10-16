#!/usr/bin/env python3
"""Ежедневный синк EDU Dashboard: Директ API + Google Sheets → Supabase."""

import importlib
import os
import sys

from dotenv import load_dotenv

load_dotenv()


def _resolve_env() -> None:
    """Поддержка имён секретов из BJ_auto_metrica и edu-sync."""
    if not os.environ.get("GOOGLE_SHEETS_ID") and os.environ.get("SHEET_ID_EDU"):
        os.environ["GOOGLE_SHEETS_ID"] = os.environ["SHEET_ID_EDU"]
    if not os.environ.get("GOOGLE_SHEETS_ID") and os.environ.get("SHEET_ID"):
        os.environ["GOOGLE_SHEETS_ID"] = os.environ["SHEET_ID"]

    if not os.environ.get("DIRECT_TOKEN") and os.environ.get("DIRECT_TOKEN_EDU"):
        os.environ["DIRECT_TOKEN"] = os.environ["DIRECT_TOKEN_EDU"]

    if not os.environ.get("DIRECT_CLIENTS_JSON") and os.environ.get(
        "DIRECT_CLIENTS_JSON_EDU"
    ):
        os.environ["DIRECT_CLIENTS_JSON"] = os.environ["DIRECT_CLIENTS_JSON_EDU"]

    # GitHub Actions: pooler (IPv4). Локально можно DIRECT_URL (5432).
    if os.environ.get("GITHUB_ACTIONS"):
        pooler = os.environ.get("DATABASE_POOLER_URL") or os.environ.get(
            "DATABASE_URL"
        )
        if pooler and "pooler.supabase.com" in pooler:
            os.environ["DATABASE_URL"] = pooler
        elif os.environ.get("DATABASE_URL", "").find("db.") >= 0 and ":5432" in os.environ.get(
            "DATABASE_URL", ""
        ):
            print(
                "WARN: DATABASE_URL — direct :5432; с Actions часто недоступен. "
                "Задайте pooler URL (…pooler.supabase.com:6543) в секрете DATABASE_URL."
            )
    elif not os.environ.get("DATABASE_URL") and os.environ.get("DIRECT_URL"):
        os.environ["DATABASE_URL"] = os.environ["DIRECT_URL"]


# Шаги по источникам: (источник, модуль, функция). EDU_SOURCES=direct,crm,… выбирает подмножество —
# пилот Panda-BI запускает их отдельными юнитами; пусто — все, как в workflow sync.yml.
STEPS = [
    ("direct", "sync.direct", "sync_direct_all"),
    ("crm", "sync.crm", "sync_crm_leads"),
    ("crm", "sync.crm", "sync_crm_payments"),
    # План EDU УБРАН из daily: monthly_plans ведётся экраном планов Panda-BI
    # (/p/edunetwork/plans). Ночной TRUNCATE из листа plan_monthly стирал бы правки.
    # Стратегии УБРАНЫ из daily: снапшот стратегий пишет sync.edu_direct_settings из API Директа.
    # CRM lite УБРАН (аудит 2026-08-21): источник оплат и выручки от лидов —
    # колонки crm_leads.payments_from_leads / revenue_from_leads.
    # Офлайн-конверсии в Я.Метрику (если задан YM_TOKEN) — ЗАПИСЬ в Метрику, дедуп через журнал
    # в БД: двум базам нельзя гонять его одновременно, иначе конверсии уйдут дважды.
    ("metrika_offline", "sync.metrika_offline", "sync_metrika_offline"),
    # Поведение визитов Метрики (vuz) для AI-скоринга лидов (если задан YM_TOKEN).
    ("visits", "sync.edu_visits", "sync_edu_visits"),
    # LIME · AppMetrica: установки + когорты покупателей (если задан APPMETRICA_TOKEN).
    ("lime_appmetrica", "sync.lime_appmetrica", "sync_lime_appmetrica"),
]
SOURCES = sorted({src for src, _, _ in STEPS})


def selected_sources() -> set[str] | None:
    """Источники из EDU_SOURCES; None — неизвестное имя (ошибка конфигурации)."""
    sources = {s.strip() for s in os.environ.get("EDU_SOURCES", "").split(",") if s.strip()}
    unknown = sources - set(SOURCES)
    if unknown:
        print(f"ОШИБКА: EDU_SOURCES: неизвестные {sorted(unknown)} (есть {', '.join(SOURCES)})")
        return None
    return sources or set(SOURCES)


def missing_env(sources: set[str]) -> list[str]:
    missing = [] if os.environ.get("DATABASE_URL") else ["DATABASE_URL"]
    if "direct" in sources:
        if not os.environ.get("DIRECT_TOKEN"):
            missing.append("DIRECT_TOKEN")
        if not (os.environ.get("DIRECT_CLIENTS_JSON") or os.environ.get("DIRECT_CLIENT_LOGIN")):
            missing.append("DIRECT_CLIENTS_JSON")
    # Таблица CRM: лиды/оплаты и client_id для офлайн-конверсий.
    if sources & {"crm", "metrika_offline"}:
        if not os.environ.get("GOOGLE_SHEETS_ID"):
            missing.append("GOOGLE_SHEETS_ID")
        if not (os.environ.get("GOOGLE_SERVICE_ACCOUNT") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")):
            missing.append("GOOGLE_CREDENTIALS")
    return missing


def main() -> int:
    print("=== EDU Sync START ===")
    _resolve_env()

    sources = selected_sources()
    if sources is None:
        return 1
    missing = missing_env(sources)
    if missing:
        print(f"ОШИБКА: отсутствуют: {', '.join(missing)}")
        return 1

    errors: list[str] = []
    for src, module, func in STEPS:
        if src not in sources:
            continue
        try:
            getattr(importlib.import_module(module), func)()
        except Exception as e:
            print(f"ОШИБКА {func}: {e}")
            errors.append(f"{func}: {e}")

    if "crm" in sources:
        try:
            from sync.validate_sheets import run_validation

            run_validation()
        except Exception as e:
            print(f"WARN validate: {e}")

    print("=== EDU Sync DONE ===")
    if errors:
        print(f"Завершено с ошибками: {errors}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
