#!/usr/bin/env python3
"""Проверка диапазонов дат в Supabase после backfill 2025."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

for p in (
    ROOT / ".env.sync",
    Path(r"d:/vscode/EDU v2/.env.local"),
    ROOT / ".env",
):
    if p.is_file():
        load_dotenv(p, override=False)

if os.environ.get("DIRECT_URL") and not os.environ.get("GITHUB_ACTIONS"):
    os.environ["DATABASE_URL"] = os.environ["DIRECT_URL"]

TABLES = ("direct_stats", "crm_leads", "crm_payments")


def main() -> None:
    from main import _resolve_env
    from sync.db import get_connection

    _resolve_env()
    with get_connection() as conn:
        with conn.cursor() as cur:
            for table in TABLES:
                cur.execute(
                    f"SELECT MIN(date), MAX(date), COUNT(*) FROM {table}"
                )
                mn, mx, cnt = cur.fetchone()
                print(f"{table}: min={mn} max={mx} count={cnt}")


if __name__ == "__main__":
    main()
