"""Проба stat API Метрики для синка проектов: снимает фикстуры tests/fixtures/projects_metrika_<kind>.json.

Токен и счётчик — из BJORN_METRICA_TOKEN / BJORN_METRICA_COUNTER_ID (.env через python-dotenv).
Токен не печатается и не пишется.

  python scripts/probe_project_metrika.py --kind traffic|goals|purchases|products|all [--goals id,id]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sync.bjorn.common import metrica_stat_rows  # noqa: E402
from sync.projects.metrika_fetch import build_params  # noqa: E402

FALLBACK_ENV = Path("C:/vscode/edu-sync/.env")
KINDS = ["traffic", "goals", "purchases", "products"]
# Голые длинные числа не ловим: это id заказов и кампаний. Телефон — с плюсом или разделителями.
PII = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|\+\d[\d\s\-()]{9,}\d|\(?\d{3}\)?[\s-]\d{3}[\s-]\d{2}[\s-]?\d{2}")


def load_env(path: str | None) -> None:
    from dotenv import load_dotenv

    candidates = [Path(path)] if path else [ROOT / ".env", FALLBACK_ENV]
    for c in candidates:
        if c.is_file():
            load_dotenv(c, override=False)
            return
    sys.exit("env-файл не найден")


def pick_goals(counter: int, token: str, limit: int = 3) -> list[int]:
    url = f"https://api-metrika.yandex.net/management/v1/counter/{counter}/goals"
    r = requests.get(url, headers={"Authorization": f"OAuth {token}"}, timeout=60)
    if r.status_code != 200:
        sys.exit(f"goals management API {r.status_code}: {r.text[:300]}")
    return [int(g["id"]) for g in r.json().get("goals", [])][:limit]


def list_counters(token: str, day: str, with_visits: bool) -> int:
    r = requests.get(
        "https://api-metrika.yandex.net/management/v1/counters",
        params={"per_page": 1000},
        headers={"Authorization": f"OAuth {token}"},
        timeout=60,
    )
    if r.status_code != 200:
        print(f"counters API {r.status_code}: {r.text[:300]}")
        return 1
    for c in r.json().get("counters", []):
        line = f"{c['id']}	{c.get('name')}	{c.get('site') or c.get('site2', {}).get('site', '')}"
        if with_visits:
            s = requests.get(
                "https://api-metrika.yandex.net/stat/v1/data",
                params={"ids": c["id"], "metrics": "ym:s:visits", "date1": day, "date2": day},
                headers={"Authorization": f"OAuth {token}"},
                timeout=60,
            )
            line += f"	visits={s.json()['totals'][0] if s.status_code == 200 else 'ERR' + str(s.status_code)}"
        print(line)
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--env")
    ap.add_argument("--token-var", default="BJORN_METRICA_TOKEN")
    ap.add_argument("--counter", type=int, help="по умолчанию BJORN_METRICA_COUNTER_ID")
    ap.add_argument("--list-counters", action="store_true", help="id, name, site счётчиков токена")
    ap.add_argument("--visits-yesterday", action="store_true", help="с --list-counters: визиты за день")
    ap.add_argument("--kind", default="all", choices=[*KINDS, "all"])
    ap.add_argument("--goals", help="id,id")
    ap.add_argument("--date", help="YYYY-MM-DD, по умолчанию вчера по Москве")
    args = ap.parse_args()

    load_env(args.env)
    token = os.environ[args.token_var]
    day = args.date or (datetime.now(ZoneInfo("Europe/Moscow")).date() - timedelta(days=1)).isoformat()
    if args.list_counters:
        return list_counters(token, day, args.visits_yesterday)
    counter = args.counter or int(os.environ["BJORN_METRICA_COUNTER_ID"])

    out_dir = ROOT / "tests" / "fixtures"
    out_dir.mkdir(parents=True, exist_ok=True)
    for kind in KINDS if args.kind == "all" else [args.kind]:
        goal_ids = None
        if kind == "goals":
            goal_ids = [int(x) for x in args.goals.split(",")] if args.goals else pick_goals(counter, token)
        params = build_params(counter, day, day, kind=kind, goal_ids=goal_ids)
        try:
            data = metrica_stat_rows(params, token)
        except RuntimeError as exc:
            print(f"{kind}: FAIL {exc}")
            return 1
        sample = data[:5]
        if kind != "products":
            # Первые строки — прямые/органика с пустым DirectClickOrder; для фикстуры нужны и строки Директа.
            direct = [r for r in data if (r["dimensions"][-1] or {}).get("id")]
            sample = data[:2] + direct[:3]
        text = json.dumps(sample, ensure_ascii=False, indent=2)
        hits = PII.findall(text)
        if hits:
            print(f"{kind}: найдены email/телефоны ({len(hits)}), фикстура НЕ сохранена")
            return 2
        (out_dir / f"projects_metrika_{kind}.json").write_text(text + "\n", encoding="utf-8")
        print(f"{kind}: date={day} rows_total={len(data)} saved={len(sample)}"
              + (f" goals={goal_ids}" if goal_ids else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
