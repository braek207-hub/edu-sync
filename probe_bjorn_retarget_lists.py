"""Проба BJORN №2 для мини-кампании на ретаргет: то, что не отдалось с первого раза.

  1. Условия ретаргетинга целиком (постранично) — ищем FOR_RETARGETING, а не только
     автосозданные AUDIENCE/FOR_TARGETS_ONLY.
  2. Образцы ТГО действующих кампаний с корректным набором полей + их наборы быстрых ссылок.
  3. Картинки аккаунта (какие форматы есть под РСЯ).
  4. Стратегия и настройки кампании-образца 714000003.

Read-only.
"""

from __future__ import annotations

import json
import os

import requests

API = "https://api.direct.yandex.com/json/v5/"
SAMPLE_CAMPAIGNS = [713525080, 713525055, 714000003, 714663647]


def call(service: str, params: dict, login: str, token: str, method: str = "get") -> dict:
    body = json.dumps({"method": method, "params": params}, ensure_ascii=False).encode("utf-8")
    resp = requests.post(
        API + service,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Client-Login": login,
            "Accept-Language": "ru",
            "Content-Type": "application/json; charset=utf-8",
        },
        timeout=120,
    )
    try:
        return resp.json()
    except Exception:
        return {"error": {"error_string": f"HTTP {resp.status_code}", "error_detail": resp.text[:300]}}


def err(body: dict) -> str | None:
    e = body.get("error")
    return None if not e else f"{e.get('error_string')} | {e.get('error_detail')}"[:600]


def clients() -> list[tuple[str, str]]:
    default_token = os.environ.get("DIRECT_TOKEN", "").strip()
    raw = os.environ.get("DIRECT_CLIENTS_JSON", "").strip()
    out: list[tuple[str, str]] = []
    if raw:
        data = json.loads(raw)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    login = str(item.get("login") or item.get("client_login") or "").strip()
                    token = str(item.get("token") or "").strip() or default_token
                    if login:
                        out.append((login, token))
                elif isinstance(item, str):
                    out.append((item.strip(), default_token))
    return out


def probe(login: str, token: str) -> None:
    print("=" * 78)
    print(f"АККАУНТ {login}")
    print("=" * 78)

    # ── 1. Все условия ретаргетинга ──────────────────────────────────────────
    print("\n### 1. УСЛОВИЯ РЕТАРГЕТИНГА (все страницы)")
    offset, total, kinds = 0, 0, {}
    interesting = []
    while True:
        r = call("retargetinglists", {
            "SelectionCriteria": {},
            "FieldNames": ["Id", "Name", "Type", "Scope", "IsAvailable", "Rules"],
            "Page": {"Limit": 500, "Offset": offset},
        }, login, token)
        e = err(r)
        if e:
            print(f"  ОШИБКА: {e}")
            break
        rows = r.get("result", {}).get("RetargetingLists", [])
        if not rows:
            break
        total += len(rows)
        for l in rows:
            key = f"{l.get('Type')}/{l.get('Scope')}"
            kinds[key] = kinds.get(key, 0) + 1
            if l.get("Type") == "RETARGETING":
                interesting.append(l)
        lim = r.get("result", {}).get("LimitedBy")
        if not lim:
            break
        offset = lim
    print(f"  всего {total}; по типам: {kinds}")
    print(f"\n  --- пригодные для ретаргетинга ({len(interesting)}) ---")
    for l in interesting:
        print(f"  {l['Id']} | {l.get('Type')}/{l.get('Scope')} | доступно={l.get('IsAvailable')} "
              f"| {l.get('Name')}")
        print(f"      {json.dumps(l.get('Rules'), ensure_ascii=False)[:500]}")



def main() -> None:
    for login, token in clients():
        if token:
            probe(login, token)
            return


if __name__ == "__main__":
    main()
