"""Поведение визитов Яндекс Метрики (Reporting API) для AI-скоринга лидов EDU.

Пилот — счётчик vuz (98627983). Тянем per-clientID агрегат поведения (визиты, отказы,
глубина, среднее время) и пишем ТОЛЬКО для client_id, которые есть в лидах
(crm_lead_details) — скорим лидов, а не весь трафик счётчика.

Путь — Reporting API (/stat/v1/data), тот же проверенный в проде, что и sync/polinarepik.py
(_metrica_get). Тот же OAuth YM_TOKEN, что и офлайн-конверсии (sync/metrika_offline.py).
Per-visit гранулярность + честный бот-флаг (Logs API ym:s:isRobot) и запись сессии —
отдельная фаза, здесь не нужны: для скоринга лида хватает клиентского агрегата поведения.
"""

import os
import time
from datetime import date, timedelta
from typing import Any, Dict, List

import requests

from sync.db import load_lead_client_ids, upsert_edu_visit_behavior
from sync.metrika_offline import COUNTER_VUZ

METRICA_API_URL = "https://api-metrika.yandex.net/stat/v1/data"
# lastsign — как polinarepik: атрибуция «последний значимый источник» (консистентно с воронкой).
ATTRIBUTION = "lastsign"


def _token() -> str:
    return os.environ.get("YM_TOKEN", "").strip()


def _metrica_get(params: Dict[str, Any], token: str) -> Dict[str, Any]:
    """GET Reporting API с ретраями. 403 на первом запросе = scope YM_TOKEN не пускает
    в статистику (нужен токен с доступом на чтение Метрики) — пробрасываем как есть."""
    headers = {"Authorization": f"OAuth {token}"}
    backoff = 2
    for attempt in range(6):
        resp = requests.get(METRICA_API_URL, params=params, headers=headers, timeout=120)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in {429, 500, 502, 503, 504} and attempt < 5:
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        raise RuntimeError(f"Metrica API {resp.status_code}: {resp.text[:400]}")
    raise RuntimeError("Metrica API: max retries")


def fetch_edu_client_visits(
    counter_id: str, date_from: str, date_to: str, token: str, keep_cids: set
) -> List[Dict[str, Any]]:
    """per (date, clientID): визиты + взвешенные (по визитам) отказы/глубина/время.
    Фильтр keep_cids на стороне Python — Reporting API не умеет фильтровать по списку id."""
    dimensions = "ym:s:date,ym:s:clientID"
    metrics = "ym:s:visits,ym:s:bounceRate,ym:s:pageDepth,ym:s:avgVisitDurationSeconds"

    aggregate: Dict[tuple, Dict[str, float]] = {}
    limit = 100000
    offset = 1

    while True:
        params = {
            "ids": counter_id,
            "metrics": metrics,
            "dimensions": dimensions,
            "date1": date_from,
            "date2": date_to,
            "accuracy": "full",
            "proposed_accuracy": "false",
            "attribution": ATTRIBUTION,
            "lang": "ru",
            "limit": limit,
            "offset": offset,
        }
        payload = _metrica_get(params, token)
        data = payload.get("data", [])
        if not data:
            break

        for record in data:
            dims = [str(d.get("name", "")).strip() for d in record.get("dimensions", [])]
            if len(dims) < 2:
                continue
            row_date, client_id = dims[0], dims[1]
            if not row_date or not client_id or client_id in {"(not set)", "0"}:
                continue
            if client_id not in keep_cids:
                continue
            mets = record.get("metrics") or []
            visits = int(float(mets[0] or 0)) if len(mets) > 0 else 0
            if visits <= 0:
                continue
            bounce = float(mets[1] or 0) if len(mets) > 1 else 0.0
            depth = float(mets[2] or 0) if len(mets) > 2 else 0.0
            duration = float(mets[3] or 0) if len(mets) > 3 else 0.0

            key = (row_date, client_id)
            acc = aggregate.get(key)
            if acc is None:
                acc = {"visits": 0, "bounce_w": 0.0, "depth_w": 0.0, "dur_w": 0.0}
                aggregate[key] = acc
            acc["visits"] += visits
            acc["bounce_w"] += bounce * visits
            acc["depth_w"] += depth * visits
            acc["dur_w"] += duration * visits

        if len(data) < limit:
            break
        offset += limit

    out: List[Dict[str, Any]] = []
    for (row_date, client_id), acc in sorted(aggregate.items()):
        v = acc["visits"]
        out.append(
            {
                "counter_id": int(counter_id),
                "visit_date": row_date,
                "client_id": client_id,
                "visits": v,
                "bounce_rate": round(acc["bounce_w"] / v, 2) if v else 0.0,
                "page_depth": round(acc["depth_w"] / v, 2) if v else 0.0,
                "avg_duration_sec": round(acc["dur_w"] / v, 1) if v else 0.0,
            }
        )
    return out


def sync_edu_visits(days_back: int = 90) -> int:
    token = _token()
    if not token:
        print("EDU visits: YM_TOKEN не задан — пропуск")
        return 0
    keep = load_lead_client_ids()
    if not keep:
        print("EDU visits: нет client_id в лидах — пропуск")
        return 0

    today = date.today()
    date_from = (today - timedelta(days=days_back)).isoformat()
    date_to = today.isoformat()
    print(f"EDU visits (vuz {COUNTER_VUZ}): {date_from} — {date_to}, лидов-client_id={len(keep)}")

    rows = fetch_edu_client_visits(COUNTER_VUZ, date_from, date_to, token, keep)
    matched = len({r["client_id"] for r in rows})
    print(f"  визитных строк для лидов: {len(rows)} (уникальных client_id с поведением: {matched})")
    if not rows:
        return 0
    return upsert_edu_visit_behavior(rows)


if __name__ == "__main__":
    import sys

    from dotenv import load_dotenv

    load_dotenv()
    days = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("EDU_VISITS_DAYS", "90"))
    n = sync_edu_visits(days)
    print(f"EDU visits: upsert {n} строк")
