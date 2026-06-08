#!/usr/bin/env python3
"""Backfill Direct за 2025 (monthly upsert, без delete 2026). Env из .env.sync или EDU v2."""
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

os.environ.setdefault("DIRECT_SOURCE", "api")

DATE_FROM = os.environ.get("DIRECT_DATE_FROM", "2025-01-01")
DATE_TO = os.environ.get("DIRECT_DATE_TO", "2025-12-31")

if __name__ == "__main__":
    from main import _resolve_env
    from sync.db import ensure_schema
    from sync.direct import sync_direct_backfill_monthly

    _resolve_env()
    ensure_schema()
    print(f"Direct backfill: {DATE_FROM} — {DATE_TO} (monthly upsert)")
    n = sync_direct_backfill_monthly(DATE_FROM, DATE_TO)
    print(f"Записано строк: {n}")
