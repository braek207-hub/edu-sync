#!/usr/bin/env python3
"""Ежедневный синк EDU Dashboard: Директ API + Google Sheets → Supabase."""

import os
import sys

from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    print("=== EDU Sync START ===")

    required = [
        "DATABASE_URL",
        "DIRECT_TOKEN",
        "DIRECT_CLIENT_LOGIN",
        "GOOGLE_SHEETS_ID",
    ]
    has_google = bool(
        os.environ.get("GOOGLE_SERVICE_ACCOUNT")
        or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    )
    if not has_google:
        required.append("GOOGLE_SERVICE_ACCOUNT")

    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"ОШИБКА: отсутствуют переменные: {', '.join(missing)}")
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

    print("=== EDU Sync DONE ===")
    if errors:
        print(f"Завершено с ошибками: {errors}")
        sys.exit(1)


if __name__ == "__main__":
    main()
