"""Из чего собраны условия ретаргетинга групп 714000003 и что можно взять как «смотрел эту куртку».

  1. Правила условий 42326630..42326662 (по одному на группу) и общего 41921267.
  2. Сегменты и цели счётчика BJORN — есть ли URL-сегменты на карточки товаров.
  3. Права на счётчик (можем ли создавать сегменты) — поле permission из списка счётчиков.

Read-only.
"""

from __future__ import annotations

import json
import os

import requests

API = "https://api.direct.yandex.com/json/v5/"
METRIKA = "https://api-metrika.yandex.net/management/v1"
GROUP_LISTS = list(range(42326630, 42326663)) + [41921267]


def call(service: str, params: dict, login: str, token: str, method: str = "get") -> dict:
    body = json.dumps({"method": method, "params": params}, ensure_ascii=False).encode("utf-8")
    resp = requests.post(API + service, data=body, headers={
        "Authorization": f"Bearer {token}",
        "Client-Login": login,
        "Accept-Language": "ru",
        "Content-Type": "application/json; charset=utf-8",
    }, timeout=120)
    try:
        return resp.json()
    except Exception:
        return {"error": {"error_string": f"HTTP {resp.status_code}", "error_detail": resp.text[:300]}}


def clients() -> list[tuple[str, str]]:
    default_token = os.environ.get("DIRECT_TOKEN", "").strip()
    raw = os.environ.get("DIRECT_CLIENTS_JSON", "").strip()
    out: list[tuple[str, str]] = []
    if raw:
        for item in json.loads(raw):
            if isinstance(item, dict):
                login = str(item.get("login") or item.get("client_login") or "").strip()
                token = str(item.get("token") or "").strip() or default_token
                if login:
                    out.append((login, token))
            elif isinstance(item, str):
                out.append((item.strip(), default_token))
    return out


def rights_and_segments() -> None:
    token = os.environ.get("METRICA_TOKEN", "").strip()
    counter = os.environ.get("METRICA_COUNTER_ID", "").strip()
    h = {"Authorization": f"OAuth {token}"}

    print("### ПРАВА НА СЧЁТЧИК")
    r = requests.get(f"{METRIKA}/counters", headers=h,
                     params={"field": "permission,owner_login", "per_page": 200}, timeout=60)
    if r.status_code != 200:
        print(f"  counters: HTTP {r.status_code} {r.text[:250]}")
    else:
        for c in r.json().get("counters", []):
            if str(c.get("id")) == counter:
                print(f"  счётчик {c.get('id')} «{c.get('name')}» {c.get('site')}")
                print(f"  permission={c.get('permission')} | владелец={c.get('owner_login')}")

    print("\n### ЦЕЛИ СЧЁТЧИКА (тип и условия)")
    rg = requests.get(f"{METRIKA}/counter/{counter}/goals", headers=h, timeout=60)
    if rg.status_code != 200:
        print(f"  goals: HTTP {rg.status_code} {rg.text[:250]}")
    else:
        goals = rg.json().get("goals", [])
        print(f"  всего целей: {len(goals)}")
        for g in goals:
            conds = g.get("conditions") or []
            ct = "; ".join(f"{c.get('type')}={c.get('url')}" for c in conds)[:200]
            print(f"  {g.get('id')} | {g.get('name')} | {g.get('type')} | {ct}")

    print("\n### СЕГМЕНТЫ СЧЁТЧИКА")
    rs = requests.get(f"{METRIKA}/counter/{counter}/segments", headers=h, timeout=60)
    if rs.status_code != 200:
        print(f"  segments: HTTP {rs.status_code} {rs.text[:250]}")
    else:
        segs = rs.json().get("segments", [])
        print(f"  всего сегментов: {len(segs)}")
        for s in segs:
            print(f"  {s.get('segment_id')} | {s.get('name')} | {str(s.get('expression'))[:200]}")


def lists(login: str, token: str) -> None:
    print("\n### УСЛОВИЯ РЕТАРГЕТИНГА ГРУПП")
    for chunk_start in range(0, len(GROUP_LISTS), 10):
        ids = GROUP_LISTS[chunk_start:chunk_start + 10]
        r = call("retargetinglists", {
            "SelectionCriteria": {"Ids": ids},
            "FieldNames": ["Id", "Name", "Description", "Type", "Scope", "Rules"],
            "Page": {"Limit": 50},
        }, login, token)
        if r.get("error"):
            print(f"  error: {r['error'].get('error_string')} | {r['error'].get('error_detail')}")
            return
        for l in r.get("result", {}).get("RetargetingLists", []):
            print(f"\n  {l['Id']} «{l.get('Name')}» type={l.get('Type')} scope={l.get('Scope')}")
            if l.get("Description"):
                print(f"     описание: {l['Description']}")
            for rule in l.get("Rules", []):
                args = ", ".join(f"{a.get('ExternalId')}/{a.get('MembershipLifeSpan')}д"
                                 for a in rule.get("Arguments", []))
                print(f"     {rule.get('Operator')}: {args}")


def main() -> None:
    rights_and_segments()
    for login, token in clients():
        if token:
            print(f"\naккаунт {login}")
            lists(login, token)
            return


if __name__ == "__main__":
    main()
