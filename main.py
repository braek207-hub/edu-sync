#!/usr/bin/env python3
"""Ежедневный синк EDU Dashboard: Директ API + Google Sheets → Supabase."""

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


def main() -> None:
    print("=== EDU Sync START ===")
    _resolve_env()

    required = ["DATABASE_URL", "DIRECT_TOKEN", "GOOGLE_SHEETS_ID"]
    has_direct_client = bool(
        os.environ.get("DIRECT_CLIENTS_JSON")
        or os.environ.get("DIRECT_CLIENT_LOGIN")
    )
    if not has_direct_client:
        required.append("DIRECT_CLIENTS_JSON (или DIRECT_CLIENT_LOGIN)")

    has_google = bool(
        os.environ.get("GOOGLE_SERVICE_ACCOUNT")
        or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    )
    if not has_google:
        required.append("GCP_SA_KEY / GOOGLE_SERVICE_ACCOUNT")

    missing = []
    for k in required:
        if "или" in k:
            continue
        if not os.environ.get(k):
            missing.append(k)
    if not has_direct_client:
        missing.append("DIRECT_CLIENTS_JSON")
    if not has_google:
        missing.append("GOOGLE_CREDENTIALS")

    if missing:
        print(f"ОШИБКА: отсутствуют: {', '.join(missing)}")
        sys.exit(1)

    errors: list[str] = []

    try:
        from sync.direct import sync_direct

        sync_direct(days_back=7)
    except Exception as e:
        print(f"ОШИБКА direct: {e}")
        errors.append(f"direct: {e}")

    try:
        from sync.crm import sync_crm_leads

        sync_crm_leads()
    except Exception as e:
        print(f"ОШИБКА crm_leads: {e}")
        errors.append(f"crm_leads: {e}")

    try:
        from sync.crm import sync_crm_payments

        sync_crm_payments()
    except Exception as e:
        print(f"ОШИБКА crm_payments: {e}")
        errors.append(f"crm_payments: {e}")

    try:
        from sync.plan import sync_plan_monthly

        sync_plan_monthly()
    except Exception as e:
        print(f"ОШИБКА plan: {e}")
        errors.append(f"plan: {e}")

    try:
        from sync.strategies import sync_strategies_daily

        sync_strategies_daily()
    except Exception as e:
        print(f"ОШИБКА strategies: {e}")
        errors.append(f"strategies: {e}")

    print("=== EDU Sync DONE ===")
    if errors:
        print(f"Завершено с ошибками: {errors}")
        sys.exit(1)


if __name__ == "__main__":
    main()
