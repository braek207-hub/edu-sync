#!/usr/bin/env python3
"""Apply decortier schema migrations before sync (idempotent)."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

import psycopg2

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT / "migrations" / "decortier"


def connect():
    url = os.environ["DATABASE_URL"]
    if "pgbouncer=" in url:
        base, _, qs = url.partition("?")
        kept = "&".join(p for p in qs.split("&") if p and not p.startswith("pgbouncer="))
        url = f"{base}?{kept}" if kept else base
    parsed = urlparse(url)
    return psycopg2.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        user=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
        dbname=(parsed.path or "/postgres").lstrip("/") or "postgres",
        sslmode="require",
    )


def main() -> int:
    if not os.environ.get("DATABASE_URL"):
        print("ERROR: DATABASE_URL required", file=sys.stderr)
        return 1

    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print(f"ERROR: no migrations in {MIGRATIONS_DIR}", file=sys.stderr)
        return 1

    with connect() as conn:
        with conn.cursor() as cur:
            for path in files:
                print(f"Applying {path.name}")
                cur.execute(path.read_text(encoding="utf-8"))
            cur.execute(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name LIKE 'decortier%%'
                ORDER BY table_name
                """
            )
            print("Tables:", [row[0] for row in cur.fetchall()])
        conn.commit()

    print("Migrations OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
