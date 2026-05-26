#!/usr/bin/env python3
"""Локальный пересинк Direct (без CRM). Env из EDU v2 .env.local или .env.sync."""
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

os.environ.setdefault("DIRECT_DATE_FROM", "2026-01-01")
os.environ.setdefault("DIRECT_SOURCE", "api")

if __name__ == "__main__":
    from sync.db import ensure_schema
    from sync.direct import sync_direct_all

    ensure_schema()
    print(
        f"Direct: source={os.environ.get('DIRECT_SOURCE')} "
        f"from={os.environ.get('DIRECT_DATE_FROM')}"
    )
    n = sync_direct_all()
    print(f"Записано строк: {n}")
