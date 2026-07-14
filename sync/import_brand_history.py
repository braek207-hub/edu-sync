"""Одноразовый импорт истории брендового трафика из файла Павла (CSV).

Сидит историю 2023-2025 (недельно) в lime_wordstat_demand + lime_brand_seo(source='file');
API дальше дописывает свежие недели (перетирают файловые строки по PK при пересечении).

Колонки по умолчанию: week_start, demand, seo_clicks. Реальный файл (выгрузка Google-листа)
подгоняется через COLMAP при получении.
"""
import csv
import datetime as dt
import os

COLMAP = {"week_start": "week_start", "demand": "demand", "seo_clicks": "seo_clicks"}


def _monday(date_str: str) -> str:
    d = dt.date.fromisoformat(date_str[:10])
    return (d - dt.timedelta(days=d.weekday())).isoformat()


def parse_history_csv(path: str, colmap: dict | None = None) -> list[dict]:
    cm = colmap or COLMAP
    out: list[dict] = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            out.append({
                "week_start": _monday(row[cm["week_start"]]),
                "demand": int(float(row[cm["demand"]])),
                "seo_clicks": int(float(row[cm["seo_clicks"]])),
            })
    return out


def import_history(path: str, colmap: dict | None = None) -> tuple[int, int]:
    rows = parse_history_csv(path, colmap)
    if not rows:
        return 0, 0
    from sync.db import get_connection  # ленивый импорт psycopg2

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO lime_wordstat_demand (week_start, region, frequency, updated_at)
                VALUES (%s, 'ru', %s, now())
                ON CONFLICT (week_start, region)
                DO UPDATE SET frequency = EXCLUDED.frequency, updated_at = now()
                """,
                [(r["week_start"], r["demand"]) for r in rows],
            )
            cur.executemany(
                """
                INSERT INTO lime_brand_seo (week_start, clicks, impressions, source, updated_at)
                VALUES (%s, %s, NULL, 'file', now())
                ON CONFLICT (week_start)
                DO UPDATE SET clicks = EXCLUDED.clicks, source = 'file', updated_at = now()
                """,
                [(r["week_start"], r["seo_clicks"]) for r in rows],
            )
        conn.commit()
    return len(rows), len(rows)
