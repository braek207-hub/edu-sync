"""Акция −15% до 11 октября в заголовках 715011487 + таблица цен (старая/новая).

Второй заголовок на комбинаторных ТГО через API молча теряется — Директ отвечает
предупреждением 10252 «Комбинаторный баннер изменён через устаревший API текстовых
баннеров», и Title2 не сохраняется. Поэтому вся акция живёт в первом заголовке:
модель + −15% + срок, формулировки ротируются между группами.

Цена: поля цены у TEXT_AD в API нет (читающий enum его не содержит). Скрипт пробует
записать PriceExtension и честно печатает ответ, но считать это сделанным нельзя —
прочитать обратно API не даёт. Для ручной простановки внизу печатается таблица
«модель; старая цена; новая (−15%)». Старая цена берётся с карточки товара строго
из JSON-LD Product.offers.

APPLY=1 — писать. Без него план с проверкой длин.
"""

from __future__ import annotations

import json
import os
import re
import sys

import requests

API = "https://api.direct.yandex.com/json/v5/"
CAMPAIGN = 715011487
EXPECTED_LOGIN_PART = "bjorn"
DISCOUNT = 0.15
PROMO_TAG = "-15%"

TITLE1_MAX = 56          # считаются все символы, включая узкие
WORD_MAX = 22

TITLE_VARIANTS = [
    "{p} BJORN LARSEN: -15% до 11 октября",
    "{p}: скидка 15% только до 11 октября",
    "{p} BJORN LARSEN: скидка 15% до 11.10",
    "{p}: -15%, успейте до 11 октября",
    "{p}: скидка 15% — до 11 октября",
    "{p} BJORN LARSEN: -15% до 11.10",
]


def call(service: str, params: dict, login: str, token: str, method: str = "get") -> dict:
    body = json.dumps({"method": method, "params": params}, ensure_ascii=False).encode("utf-8")
    resp = requests.post(API + service, data=body, headers={
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


def longest_word(text: str) -> int:
    return max((len(w) for w in re.split(r"[\s\-/]+", text) if w), default=0)


def fetch_price(url: str) -> tuple[int | None, str]:
    """Текущая цена с карточки — строго из JSON-LD Product.offers.

    Регуляркой по всей странице нельзя: в блоках «похожие товары» лежат цены других
    моделей, и у Нарвика так находилось 19 900 вместо настоящих 47 900.
    """
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


def round_down_10(value: float) -> int:
    return int(value // 10 * 10)


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

    res = need(call("ads", {
        "SelectionCriteria": {"CampaignIds": [CAMPAIGN], "Types": ["TEXT_AD"]},
        "FieldNames": ["Id", "AdGroupId", "State", "Status"],
        "TextAdFieldNames": ["Title", "Title2", "Text", "Href"],
        "Page": {"Limit": 1000}}, login, token), "объявления")
    ads = res.get("Ads", [])
    print(f"ТГО: {len(ads)}\n")

    updates, prices, problems = [], [], []
    for i, a in enumerate(sorted(ads, key=lambda x: x["Id"])):
        ta = a.get("TextAd") or {}
        title = (ta.get("Title") or "").strip()
        href = ta.get("Href") or ""

        # «Аляска Нарвик BJORN LARSEN: гусиный пух, −50°C -15%» -> «Аляска Нарвик»
        base = re.sub(r"\s*-15%\s*$", "", title)
        model = base.split(" BJORN LARSEN")[0].split(":")[0].strip()

        new_title = TITLE_VARIANTS[i % len(TITLE_VARIANTS)].format(p=model)
        if len(new_title) > TITLE1_MAX:
            new_title = new_title.replace(" BJORN LARSEN", "")

        bad = []
        if len(new_title) > TITLE1_MAX:
            bad.append(f"Title {len(new_title)}>{TITLE1_MAX}")
        if longest_word(new_title) > WORD_MAX:
            bad.append(f"слово в Title {longest_word(new_title)}>{WORD_MAX}")
        if PROMO_TAG not in new_title and "скидка 15%" not in new_title.lower():
            bad.append("нет упоминания скидки")
        if bad:
            problems.append((a["Id"], new_title, "; ".join(bad)))
            continue

        old_price, note = fetch_price(href)
        new_price = round_down_10(old_price * (1 - DISCOUNT)) if old_price else None
        prices.append((a["Id"], model, old_price, new_price, note))
        updates.append(({"Id": a["Id"], "TextAd": {"Title": new_title}}, old_price, new_price))
        print(f"  {a['Id']} | {len(new_title)}/{TITLE1_MAX} «{new_title}»")
        print(f"      цена {old_price} -> {new_price}")

    if problems:
        print("\nне прошли проверку длин:")
        for aid, t, why in problems:
            print(f"  ! {aid} «{t}» — {why}")

    print(f"\nцены с сайта: {sum(1 for p in prices if p[2])} из {len(prices)}")
    for aid, model, old, new, note in prices:
        if not old:
            print(f"  ! {aid} {model} — цена не найдена: {note}")

    if not apply:
        print(f"\nбез APPLY=1 ничего не меняю (готово к записи: {len(updates)})")
        return

    out = need(call("ads", {"Ads": [u for u, _, _ in updates]},
                    login, token, "update"), "обновление заголовков")
    ok = sum(1 for r in out.get("UpdateResults", []) if r.get("Id"))
    print(f"\nобновлено заголовков: {ok} из {len(updates)}")
    warn: dict = {}
    for r in out.get("UpdateResults", []):
        for er in r.get("Errors", []):
            print(f"    ошибка {er.get('Code')}: {er.get('Message')} {er.get('Details') or ''}")
        for w in r.get("Warnings", []):
            warn[w.get("Code")] = warn.get(w.get("Code"), 0) + 1
    if warn:
        print(f"    предупреждения: {warn}")

    probe = next(((u, o, n) for u, o, n in updates if o and n), None)
    if probe:
        u, old, new = probe
        for label, body in (
            ("микро", {"Price": new * 1_000_000, "OldPrice": old * 1_000_000,
                       "PriceQualifier": "NONE", "PriceCurrency": "RUB"}),
            ("рубли", {"Price": new, "OldPrice": old,
                       "PriceQualifier": "NONE", "PriceCurrency": "RUB"}),
        ):
            test = call("ads", {"Ads": [{"Id": u["Id"], "TextAd": {"PriceExtension": body}}]},
                        login, token, "update")
            if test.get("error"):
                print(f"  цена [{label}]: {test['error'].get('error_string')} | "
                      f"{test['error'].get('error_detail')}")
                continue
            r0 = (test.get("result") or {}).get("UpdateResults", [{}])[0]
            print(f"  цена [{label}]: {json.dumps(r0, ensure_ascii=False)}")
            if r0.get("Id") and not r0.get("Errors"):
                break

    print("\n### итог")
    chk = need(call("ads", {
        "SelectionCriteria": {"CampaignIds": [CAMPAIGN], "Types": ["TEXT_AD"]},
        "FieldNames": ["Id"], "TextAdFieldNames": ["Title", "Title2"],
        "Page": {"Limit": 1000}}, login, token), "сверка")
    rows = chk.get("Ads", [])
    with_promo = sum(1 for a in rows
                     if PROMO_TAG in ((a.get("TextAd") or {}).get("Title") or "")
                     or "скидка 15%" in ((a.get("TextAd") or {}).get("Title") or "").lower())
    print(f"  ТГО {len(rows)}: со скидкой в первом заголовке {with_promo}")
    seen = {(a.get("TextAd") or {}).get("Title") or "" for a in rows}
    print(f"  уникальных заголовков: {len(seen)}")
    for t in sorted(seen):
        print(f"    «{t}»")

    print("\n### цены для простановки: модель; старая; новая -15%")
    for aid, model, old, new, note in prices:
        shown_old = old if old else f"НЕ НАЙДЕНА ({note})"
        print(f"  {model}; {shown_old}; {new if new else '—'}")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
