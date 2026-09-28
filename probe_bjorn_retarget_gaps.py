"""Закрываем два пробела перед записью кампании BJORN.

  1. Сегменты Метрики: какие счётчики есть у доступа и есть ли сегмент «визит не отказ»
     (в условиях Директа фигурируют 1007318582 и 1007318587 — надо понять, чьи они и что значат).
  2. Картинки: какого Type картинки у действующих ТГО (для ТГО годятся только REGULAR и WIDE).

Read-only.
"""

from __future__ import annotations

import json
import os

import requests

API = "https://api.direct.yandex.com/json/v5/"
METRIKA = "https://api-metrika.yandex.net/management/v1"
HASHES = ["7ovkyj4xhZQsvHB5XM2kSg", "0F4qRKZuzmeoFuLkdgLKWw",
          "eEwm-zF2AJnQ6gj_6rk_vA", "c2MmwJYjB6F1gJsMh6ab6w"]


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


def metrika() -> None:
    token = os.environ.get("METRICA_TOKEN", "").strip()
    main_counter = os.environ.get("METRICA_COUNTER_ID", "").strip()
    h = {"Authorization": f"OAuth {token}"}
    print("### СЧЁТЧИКИ И СЕГМЕНТЫ")
    r = requests.get(f"{METRIKA}/counters", headers=h, timeout=60)
    if r.status_code != 200:
        print(f"counters: HTTP {r.status_code} {r.text[:200]}")
        return
    counters = r.json().get("counters", [])
    print(f"доступно счётчиков: {len(counters)} (основной {main_counter})")
    for c in counters:
        cid = c.get("id")
        print(f"\n--- счётчик {cid} | {c.get('name')} | {c.get('site')} ---")
        rs = requests.get(f"{METRIKA}/counter/{cid}/segments", headers=h, timeout=60)
        if rs.status_code != 200:
            print(f"   сегменты: HTTP {rs.status_code} {rs.text[:150]}")
            continue
        for s in rs.json().get("segments", []):
            print(f"   {s.get('segment_id')} | {s.get('name')} | {str(s.get('expression'))[:220]}")


def images(login: str, token: str) -> None:
    print("\n### КАРТИНКИ ТГО: тип и подтип")
    offset = 0
    found: dict[str, dict] = {}
    while True:
        r = call("adimages", {
            "SelectionCriteria": {"AdImageHashes": HASHES},
            "FieldNames": ["AdImageHash", "Name", "Type", "Subtype", "Associated"],
            "Page": {"Limit": 100, "Offset": offset},
        }, login, token)
        if r.get("error"):
            print(f"adimages.get: {r['error'].get('error_string')} | {r['error'].get('error_detail')}")
            return
        rows = r.get("result", {}).get("AdImages", [])
        for i in rows:
            found[i["AdImageHash"]] = i
        lim = r.get("result", {}).get("LimitedBy")
        if not lim or not rows:
            break
        offset = lim
    for hsh in HASHES:
        print(f"  {hsh}: {json.dumps(found.get(hsh), ensure_ascii=False)}")


def main() -> None:
    metrika()
    for login, token in clients():
        if token:
            print(f"\naккаунт {login}")
            images(login, token)
            return


if __name__ == "__main__":
    main()
