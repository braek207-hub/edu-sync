# -*- coding: utf-8 -*-
"""
sync/lime_urban.py — синк медийки Urban Ads (Яндекс Бизнес «Охватное продвижение»)
→ lime_media_stats.

Urban Ads открыл публичный API 16.07.2026. Флоу асинхронный:
  1) POST /v2/reports/banners-statistics/generate?format=JSON&sourceType=ADVERTISER
     body {businessId, dateFrom, dateTo} → result.reportId   (лимит 1 запрос / 2 мин)
  2) GET  /v2/reports/info/{reportId} → result.status (PENDING/PROCESSING/DONE/FAILED)
                                        + result.file (ссылка, живёт 60 мин)
  3) Скачать file (ZIP с JSON-листами) → распарсить лист date×campaign.

Пишем НАПРЯМУЮ в lime_media_stats (source='urban.ads'), как остальные lime-синки пишут в БД.
Директ-медийка сюда НЕ идёт — она в lime_direct_stats, в дашборде UNION.

Запуск:  python -m sync.lime_urban

ENV:
    DATABASE_URL
    API_LIME_URBAN            — токен Api-Key из кабинета urbanads.yandex.ru
    LIME_URBAN_BUSINESS_ID    — id бизнеса (default 216787226)
    LIME_URBAN_DAYS_BACK      — окно синка (default 14)
"""

import io
import os
import json
import time
import zipfile
import traceback
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import requests
import psycopg2.extras

from sync.db import get_connection

BASE_URL = "https://api.urbanads.yandex.ru"
DEFAULT_BUSINESS_ID = 216787226

# Ключи-измерения листов-срезов отчёта (по площадке/креативу/соцдему): такие строки
# суммируются поперёк доп. измерения и завысили бы итог — берём только лист date×campaign.
SLICE_KEYS = {
    "platform", "servicetype", "service", "creative", "creativeid", "placement",
    "gender", "age", "interest", "geo", "region", "device",
}


def _token() -> str:
    tok = os.environ.get("API_LIME_URBAN", "").strip()
    if not tok:
        raise RuntimeError("API_LIME_URBAN не задан (Api-Key токен Urban Ads)")
    return tok


def _headers() -> Dict[str, str]:
    return {"Api-Key": _token(), "Content-Type": "application/json"}


def ensure_media_schema() -> None:
    """Идемпотентно гарантирует таблицу (зеркалит supabase-миграцию create_lime_media_stats)."""
    ddl = """
        CREATE TABLE IF NOT EXISTS lime_media_stats (
            date            DATE        NOT NULL,
            region          TEXT        NOT NULL DEFAULT 'ru',
            source          TEXT        NOT NULL,
            campaign_group  TEXT        NOT NULL,
            media_type      TEXT        NOT NULL DEFAULT '',
            campaign_id     TEXT,
            impressions     BIGINT      NOT NULL DEFAULT 0,
            reach           BIGINT      NOT NULL DEFAULT 0,
            clicks          BIGINT      NOT NULL DEFAULT 0,
            cost            NUMERIC     NOT NULL DEFAULT 0,
            currency        TEXT,
            video_completes BIGINT      NOT NULL DEFAULT 0,
            vtr             NUMERIC,
            cpv             NUMERIC,
            conversions     JSONB       NOT NULL DEFAULT '{}'::jsonb,
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (date, region, source, campaign_group, media_type)
        )
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS lime_media_stats_source_date_idx "
                "ON lime_media_stats (source, date)"
            )


def generate_report(business_id: int, date_from: str, date_to: str) -> str:
    url = f"{BASE_URL}/v2/reports/banners-statistics/generate"
    params = {"format": "JSON", "sourceType": "ADVERTISER"}
    body = {"businessId": business_id, "dateFrom": date_from, "dateTo": date_to}
    resp = requests.post(url, headers=_headers(), params=params, json=body, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"generate HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    report_id = (data.get("result") or {}).get("reportId")
    if not report_id:
        raise RuntimeError(f"generate: нет reportId в ответе: {json.dumps(data)[:300]}")
    return report_id


def poll_report(report_id: str, max_wait_sec: int = 300) -> str:
    """Опрашивает info пока не DONE; возвращает ссылку на файл. Лимит info — 100 req/min."""
    url = f"{BASE_URL}/v2/reports/info/{report_id}"
    deadline = time.time() + max_wait_sec
    delay = 5
    while time.time() < deadline:
        resp = requests.get(url, headers=_headers(), params={"sourceType": "ADVERTISER"}, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"info HTTP {resp.status_code}: {resp.text[:300]}")
        result = resp.json().get("result") or {}
        status = str(result.get("status", "")).upper()
        if status == "DONE":
            file_url = result.get("file")
            if not file_url:
                # substatus NO_DATA — отчёт готов, но пуст: не ошибка, просто нет строк.
                if str(result.get("substatus", "")).upper() == "NO_DATA":
                    return ""
                raise RuntimeError(f"info DONE без file: {json.dumps(result)[:300]}")
            return file_url
        if status == "FAILED":
            raise RuntimeError(f"info FAILED: {json.dumps(result)[:300]}")
        time.sleep(delay)
        delay = min(delay + 5, 20)
    raise RuntimeError(f"report {report_id}: не готов за {max_wait_sec}с")


def download_rows(file_url: str) -> List[Dict[str, Any]]:
    """Скачивает ZIP с JSON-листами, возвращает строки листа date×campaign (без срезов)."""
    if not file_url:
        return []
    resp = requests.get(file_url, timeout=60)
    resp.raise_for_status()
    raw = resp.content

    sheets: List[List[Dict[str, Any]]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for name in zf.namelist():
                if not name.lower().endswith(".json"):
                    continue
                parsed = json.loads(zf.read(name).decode("utf-8"))
                rows = parsed if isinstance(parsed, list) else parsed.get("data") or parsed.get("rows") or []
                if isinstance(rows, list) and rows:
                    sheets.append(rows)
    except zipfile.BadZipFile:
        # На случай если отдали не ZIP, а голый JSON.
        parsed = json.loads(raw.decode("utf-8"))
        rows = parsed if isinstance(parsed, list) else parsed.get("data") or parsed.get("rows") or []
        if isinstance(rows, list):
            sheets.append(rows)

    # Берём лист, где строки имеют date+campaignId и НЕ содержат ключей-срезов.
    for rows in sheets:
        sample = {str(k).lower() for k in (rows[0].keys() if isinstance(rows[0], dict) else [])}
        has_grain = ("date" in sample) and any(k in sample for k in ("campaignid", "campaign_id"))
        if has_grain and not (sample & SLICE_KEYS):
            return rows
    # Фолбэк: первый лист с date+campaign, даже если со срезом (лучше залогировать, чем потерять).
    for rows in sheets:
        sample = {str(k).lower() for k in (rows[0].keys() if isinstance(rows[0], dict) else [])}
        if ("date" in sample) and any(k in sample for k in ("campaignid", "campaign_id")):
            print(f"[urban] предупреждение: подходящий лист date×campaign без срезов не найден, "
                  f"взят лист с ключами {sorted(sample)}")
            return rows
    print(f"[urban] в отчёте нет листа date×campaign; листов: {len(sheets)}")
    return []


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _int(v: Any) -> int:
    return int(round(_num(v)))


def map_rows(raw_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in raw_rows:
        d = str(r.get("date", "")).strip()[:10]
        if len(d) != 10:
            continue
        campaign_name = str(r.get("campaignName", "") or "").strip() or "urban.ads"
        media_type = str(r.get("campaignType", "") or "").strip()
        conversions = {
            "add_to_cart": _int(r.get("cartAddiction")),
            "purchase":    _int(r.get("orderedCount")),
            "revenue":     round(_num(r.get("orderedAmount")), 2),
        }
        out.append({
            "date":            d,
            "region":          "ru",
            "source":          "urban.ads",
            "campaign_group":  campaign_name,
            "media_type":      media_type,
            "campaign_id":     str(r.get("campaignId")) if r.get("campaignId") is not None else None,
            "impressions":     _int(r.get("shows")),
            "reach":           _int(r.get("coverage")),
            "clicks":          _int(r.get("clicks")),
            "cost":            round(_num(r.get("cost")), 2),
            "currency":        "RUB",
            "video_completes": _int(r.get("watchedVideo100")),
            "vtr":             _num(r.get("vtr100")) or None,
            "cpv":             _num(r.get("cpv")) or None,
            "conversions":     json.dumps(conversions, ensure_ascii=False),
        })
    return out


def upsert_media(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    # Дедуп по ключу PK: в пределах одной команды ON CONFLICT нельзя тронуть строку дважды.
    by_key: Dict[tuple, Dict[str, Any]] = {}
    for r in rows:
        by_key[(r["date"], r["region"], r["source"], r["campaign_group"], r["media_type"])] = r
    rows = list(by_key.values())

    sql = """
        INSERT INTO lime_media_stats (
            date, region, source, campaign_group, media_type, campaign_id,
            impressions, reach, clicks, cost, currency,
            video_completes, vtr, cpv, conversions, updated_at
        ) VALUES (
            %(date)s::date, %(region)s, %(source)s, %(campaign_group)s, %(media_type)s, %(campaign_id)s,
            %(impressions)s, %(reach)s, %(clicks)s, %(cost)s, %(currency)s,
            %(video_completes)s, %(vtr)s, %(cpv)s, %(conversions)s::jsonb, NOW()
        )
        ON CONFLICT (date, region, source, campaign_group, media_type) DO UPDATE SET
            campaign_id     = EXCLUDED.campaign_id,
            impressions     = EXCLUDED.impressions,
            reach           = EXCLUDED.reach,
            clicks          = EXCLUDED.clicks,
            cost            = EXCLUDED.cost,
            currency        = EXCLUDED.currency,
            video_completes = EXCLUDED.video_completes,
            vtr             = EXCLUDED.vtr,
            cpv             = EXCLUDED.cpv,
            conversions     = EXCLUDED.conversions,
            updated_at      = NOW()
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, rows, page_size=500)
        conn.commit()
    return len(rows)


def main() -> None:
    business_id = int(os.environ.get("LIME_URBAN_BUSINESS_ID", DEFAULT_BUSINESS_ID))
    days_back = int(os.environ.get("LIME_URBAN_DAYS_BACK", "14"))
    date_to = date.today() - timedelta(days=1)   # вчера (сегодня неполный)
    date_from = date_to - timedelta(days=days_back - 1)
    df, dt = date_from.isoformat(), date_to.isoformat()

    print(f"[urban] business={business_id} период {df}..{dt}")
    ensure_media_schema()

    report_id = generate_report(business_id, df, dt)
    print(f"[urban] reportId={report_id}, ждём генерацию…")
    file_url = poll_report(report_id)

    raw_rows = download_rows(file_url)
    print(f"[urban] строк в отчёте: {len(raw_rows)}")
    if raw_rows:
        # Диагностика на первый прогон: какие ключи реально пришли.
        print(f"[urban] ключи строки: {sorted(str(k) for k in raw_rows[0].keys())}")

    rows = map_rows(raw_rows)
    n = upsert_media(rows)
    print(f"[urban] upsert в lime_media_stats: {n} строк (source=urban.ads)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[urban] ОШИБКА: {e}")
        traceback.print_exc()
        raise
