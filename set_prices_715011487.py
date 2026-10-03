"""Цены в объявлениях 715011487: старая — текущая с сайта, новая — минус 15%.

Поле нашлось не там, где искали: у TEXT_AD его нет, а у RESPONSIVE_AD есть —
ResponsiveAd.PriceExtension (перечисление полей выдала сама ошибка Директа).
Пишем через v501 и обязательно читаем обратно: форма значения (микро или рубли)
документацией не подтверждена, поэтому проверяем на одном объявлении, а остальные
пишем уже рабочей формой.

Старая цена берётся с карточки товара из JSON-LD Product.offers, новая считается
как −15% с округлением вниз до 10 ₽.

APPLY=1 — писать. Без него только цены и план.
"""

from __future__ import annotations

import json
import os
import re
import sys

import requests

CAMPAIGN = 715011487
EXPECTED_LOGIN_PART = "bjorn"
DISCOUNT = 0.15


def call(version: str, service: str, params: dict, login: str, token: str,
         method: str = "get") -> dict:
    url = f"https://api.direct.yandex.com/json/{version}/{service}"
    body = json.dumps({"method": method, "params": params}, ensure_ascii=False).encode("utf-8")
    resp = requests.post(url, data=body, headers={
        "Authorization": f"Bearer {token}", "Client-Login": login,
        "Accept-Language": "ru", "Content-Type": "application/json; charset=utf-8"},
        timeout=180)
    try:
        return resp.json()
    except Exception:
        return {"error": {"error_string": f"HTTP {resp.status_code}",
                          "error_detail": resp.text[:300]}}


def need(body: dict, what: str) -> dict:
    e = body.get("error")
    if e:
        sys.exit(f"СТОП {what}: {e.get('error_string')} | {e.get('error_detail')}")
    return body.get("result", {})


def fetch_price(url: str) -> tuple[int | None, str]:
    """Текущая цена с карточки — строго из JSON-LD Product.offers."""
    try:
        resp = requests.get(url, timeout=60, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept-Language": "ru-RU,ru;q=0.9"})
    except Exception as exc:
        return None, f"ошибка запроса: {exc}"
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}"

    found: list[tuple[int, str]] = []

    def take_offer(offer: dict) -> None:
        for key in ("price", "lowPrice", "highPrice"):
            val = offer.get(key)
            if val is None:
                continue
            try:
                num = int(float(str(val).replace(" ", "").replace(",", ".")))
            except ValueError:
                continue
            if num > 0:
                found.append((num, key))

    def walk(node) -> None:
        if isinstance(node, dict):
            if node.get("@type") in ("Product", "ProductGroup"):
                offers = node.get("offers")
                for off in (offers if isinstance(offers, list) else [offers]):
                    if isinstance(off, dict):
                        take_offer(off)
                        for sub in (off.get("offers") or []):
                            if isinstance(sub, dict):
                                take_offer(sub)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for block in re.findall(
            r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', resp.text, re.S):
        try:
            walk(json.loads(block))
        except Exception:
            continue
    if not found:
        return None, "в JSON-LD нет Product.offers"
    exact = [n for n, key in found if key == "price"]
    if exact:
        return max(exact), f"price {sorted(set(exact))}"
    return min(n for n, _ in found), f"диапазон {sorted({n for n, _ in found})}"


def read_ads(login: str, token: str) -> list[dict]:
    res = need(call("v501", "ads", {
        "SelectionCriteria": {"CampaignIds": [CAMPAIGN]},
        "FieldNames": ["Id", "AdGroupId", "Type"],
        "ResponsiveAdFieldNames": ["Titles", "Href", "PriceExtension"],
        "Page": {"Limit": 1000}}, login, token), "объявления")
    return [a for a in res.get("Ads", []) if a.get("Type") == "RESPONSIVE_AD"]


def main() -> None:
    apply = os.environ.get("APPLY", "").strip() == "1"
    token = os.environ.get("DIRECT_TOKEN", "").strip()
    login = ""
    for item in json.loads(os.environ.get("DIRECT_CLIENTS_JSON") or "[]"):
        cand = item.get("login") or item.get("client_login") if isinstance(item, dict) else item
        if cand and EXPECTED_LOGIN_PART in str(cand).lower():
            login = str(cand).strip()
            if isinstance(item, dict) and item.get("token"):
                token = str(item["token"]).strip()
            break
    if not login:
        sys.exit("логин BJORN не найден")
    print(f"кабинет {login} | кампания {CAMPAIGN} | режим: {'ЗАПИСЬ' if apply else 'план'}")

    ads = read_ads(login, token)
    print(f"ТГО: {len(ads)}")
    have = sum(1 for a in ads if (a.get("ResponsiveAd") or {}).get("PriceExtension"))
    print(f"уже с ценой: {have}\n")

    plan, missing = [], []
    for a in sorted(ads, key=lambda x: x["Id"]):
        ra = a.get("ResponsiveAd") or {}
        href = ra.get("Href") or ""
        title = next((t.get("Title") for t in ra.get("Titles", []) if t.get("Title")), "")
        old, note = fetch_price(href)
        if not old:
            missing.append((a["Id"], title[:40], note))
            continue
        new = int(old * (1 - DISCOUNT) // 10 * 10)
        plan.append((a["Id"], title[:40], old, new))
        print(f"  {a['Id']} «{title[:40]}»: {old} -> {new}")

    if missing:
        print("\nбез цены:")
        for aid, title, note in missing:
            print(f"  ! {aid} «{title}» — {note}")

    if not apply:
        print(f"\nбез APPLY=1 ничего не меняю (к записи {len(plan)})")
        return

    # Форма значения неизвестна: проверяем на одном объявлении и читаем обратно
    aid, title, old, new = plan[0]
    forms = {
        "микро": {"Price": new * 1_000_000, "OldPrice": old * 1_000_000,
                  "PriceCurrency": "RUB", "PriceQualifier": "NONE"},
        "рубли": {"Price": new, "OldPrice": old,
                  "PriceCurrency": "RUB", "PriceQualifier": "NONE"},
    }
    good = None
    for label, body in forms.items():
        out = call("v501", "ads", {"Ads": [{"Id": aid, "ResponsiveAd": {
            "PriceExtension": body}}]}, login, token, "update")
        if out.get("error"):
            print(f"форма «{label}»: ОТКАЗ {out['error'].get('error_string')} | "
                  f"{out['error'].get('error_detail')}")
            continue
        r0 = (out.get("result") or {}).get("UpdateResults", [{}])[0]
        if r0.get("Errors"):
            print(f"форма «{label}»: ошибки {r0['Errors']}")
            continue
        back = next((a for a in read_ads(login, token) if a["Id"] == aid), {})
        shown = (back.get("ResponsiveAd") or {}).get("PriceExtension")
        print(f"форма «{label}»: записано, прочитано обратно "
              f"{json.dumps(shown, ensure_ascii=False)}")
        if shown:
            good = label
            break

    if not good:
        print("\nни одна форма не прошла проверку чтением — цены не поставлены")
        return

    rest = []
    for aid2, _t, old2, new2 in plan[1:]:
        body = ({"Price": new2 * 1_000_000, "OldPrice": old2 * 1_000_000,
                 "PriceCurrency": "RUB", "PriceQualifier": "NONE"} if good == "микро"
                else {"Price": new2, "OldPrice": old2,
                      "PriceCurrency": "RUB", "PriceQualifier": "NONE"})
        rest.append({"Id": aid2, "ResponsiveAd": {"PriceExtension": body}})
    out = need(call("v501", "ads", {"Ads": rest}, login, token, "update"), "запись цен")
    rows = out.get("UpdateResults", [])
    ok = sum(1 for r in rows if r.get("Id") and not r.get("Errors"))
    print(f"\nзаписано цен: {ok} из {len(rest)} (формой «{good}»)")
    errs: dict = {}
    for r in rows:
        for er in r.get("Errors", []):
            key = (er.get("Code"), er.get("Message"))
            errs[key] = errs.get(key, 0) + 1
    for (code, msg), n in errs.items():
        print(f"    ошибка {code} x{n}: {msg}")

    print("\n### итог")
    rows2 = read_ads(login, token)
    with_price = [a for a in rows2 if (a.get("ResponsiveAd") or {}).get("PriceExtension")]
    print(f"  ТГО {len(rows2)}: с ценой {len(with_price)}")
    for a in with_price[:5]:
        pe = (a.get("ResponsiveAd") or {}).get("PriceExtension")
        print(f"    {a['Id']}: {json.dumps(pe, ensure_ascii=False)}")
    titles = {len((a.get("ResponsiveAd") or {}).get("Titles") or []) for a in rows2}
    print(f"  заголовков на объявление: {sorted(titles)}")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
