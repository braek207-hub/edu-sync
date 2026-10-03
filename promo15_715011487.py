"""Акция −15% до 11 октября в заголовках 715011487 + цены (старая/новая) в объявлениях.

Что делает:
  1. читает 33 ТГО (Title, Text, Href);
  2. тянет текущую цену модели с её карточки на bjornlarsen.ru — «старая» цена берётся
     с сайта, «новая» считается как −15% с округлением вниз до 10 ₽;
  3. в первый заголовок добавляет «-15%» (если не влезает в 56 — «BJORN LARSEN»
     сокращается до «BJORN»), во второй кладёт срок акции, формулировки ротируются;
  4. пробует записать блок цены: у TEXT_AD в читающем enum его нет, поэтому проверяем
     записью на одном объявлении и печатаем ответ Директа как есть.

Все 33 объявления сейчас DRAFT/OFF — ничего не показывается, правка обратима.
APPLY=1 — писать. Без него план с проверкой длин.
"""

from __future__ import annotations

import html
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
TITLE2_MAX = 30          # узкие символы не считаются, их сверху до 15
NARROW = set('!,.;:"')
WORD_MAX = 22

# Во всех вариантах есть и −15%, и срок — Директ ротирует их между группами
TITLE2_VARIANTS = [
    "-15% только до 11 октября",
    "Скидка 15% до 11 октября",
    "Успейте: -15% до 11 октября",
    "-15% на модель до 11.10",
    "Только до 11 октября: -15%",
    "Скидка 15%, успейте до 11.10",
]

PRICE_PATTERNS = [
    r'"price"\s*:\s*"?(\d[\d\s.,]*)',
    r'og:price:amount"\s+content="(\d[\d\s.,]*)"',
    r'itemprop="price"\s+content="(\d[\d\s.,]*)"',
    r'<bdi>\s*(\d[\d\s  ]*)\s*(?:&nbsp;| |\s)*(?:₽|руб)',
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


def visible_len(text: str, count_narrow: bool) -> int:
    if count_narrow:
        return len(text)
    return sum(1 for ch in text if ch not in NARROW)


def longest_word(text: str) -> int:
    return max((len(w) for w in re.split(r"[\s\-/]+", text) if w), default=0)


def fetch_price(url: str) -> tuple[int | None, list[str]]:
    """Текущая цена с карточки. Возвращает цену и найденных кандидатов для контроля."""
    try:
        resp = requests.get(url, timeout=60, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept-Language": "ru-RU,ru;q=0.9"})
    except Exception as exc:
        return None, [f"ошибка запроса: {exc}"]
    if resp.status_code != 200:
        return None, [f"HTTP {resp.status_code}"]
    page = html.unescape(resp.text)
    found: list[int] = []
    raw: list[str] = []
    for pat in PRICE_PATTERNS:
        for m in re.finditer(pat, page):
            s = re.sub(r"[\s  ]", "", m.group(1)).replace(",", ".")
            raw.append(s)
            try:
                val = int(float(s))
            except ValueError:
                continue
            if 3000 <= val <= 300000:
                found.append(val)
    if not found:
        return None, raw[:6]
    # на карточке может быть и зачёркнутая, и акционная — берём минимальную как текущую
    return min(found), raw[:6]


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

        new_title = title
        if PROMO_TAG not in new_title:
            cand = f"{title} {PROMO_TAG}"
            if visible_len(cand, True) > TITLE1_MAX:
                cand = f"{title.replace('BJORN LARSEN', 'BJORN')} {PROMO_TAG}"
            new_title = cand
        title2 = TITLE2_VARIANTS[i % len(TITLE2_VARIANTS)]

        bad = []
        if visible_len(new_title, True) > TITLE1_MAX:
            bad.append(f"Title {visible_len(new_title, True)}>{TITLE1_MAX}")
        if longest_word(new_title) > WORD_MAX:
            bad.append(f"слово в Title {longest_word(new_title)}>{WORD_MAX}")
        if visible_len(title2, False) > TITLE2_MAX:
            bad.append(f"Title2 {visible_len(title2, False)}>{TITLE2_MAX}")
        if bad:
            problems.append((a["Id"], new_title, "; ".join(bad)))
            continue

        old_price, raw = fetch_price(href)
        new_price = round_down_10(old_price * (1 - DISCOUNT)) if old_price else None
        prices.append((a["Id"], href.rsplit("/product/", 1)[-1][:46], old_price, new_price, raw))

        upd: dict = {"Id": a["Id"], "TextAd": {"Title": new_title, "Title2": title2}}
        updates.append((upd, old_price, new_price))
        print(f"  {a['Id']} | {visible_len(new_title, True)}/{TITLE1_MAX} «{new_title}»")
        print(f"      Title2 {visible_len(title2, False)}/{TITLE2_MAX} «{title2}» | "
              f"цена {old_price} -> {new_price}")

    if problems:
        print("\nне прошли проверку длин:")
        for aid, t, why in problems:
            print(f"  ! {aid} «{t}» — {why}")

    got = sum(1 for p in prices if p[2])
    print(f"\nцены с сайта: {got} из {len(prices)}")
    for aid, slug, old, new, raw in prices:
        if not old:
            print(f"  ! {aid} {slug} — цена не найдена, кандидаты: {raw}")

    if not apply:
        print(f"\nбез APPLY=1 ничего не меняю (готово к записи: {len(updates)})")
        return

    # Заголовки
    payload = [u for u, _, _ in updates]
    out = need(call("ads", {"Ads": payload}, login, token, "update"), "обновление заголовков")
    ok = sum(1 for r in out.get("UpdateResults", []) if r.get("Id"))
    print(f"\nобновлено заголовков: {ok} из {len(payload)}")
    for r in out.get("UpdateResults", []):
        for er in r.get("Errors", []):
            print(f"    ошибка {er.get('Code')}: {er.get('Message')} {er.get('Details') or ''}")

    # Блок цены: в читающем enum его нет, проверяем записью на одном объявлении
    probe = next(((u, o, n) for u, o, n in updates if o and n), None)
    if probe:
        u, old, new = probe
        for field, body in (
            ("PriceExtension", {"Price": new * 1_000_000, "OldPrice": old * 1_000_000,
                                "PriceQualifier": "NONE", "PriceCurrency": "RUB"}),
            ("PriceExtension (целые рубли)", {"Price": new, "OldPrice": old,
                                              "PriceQualifier": "NONE",
                                              "PriceCurrency": "RUB"}),
        ):
            test = call("ads", {"Ads": [{"Id": u["Id"], "TextAd": {
                "PriceExtension": body}}]}, login, token, "update")
            if test.get("error"):
                print(f"  цена [{field}]: {test['error'].get('error_string')} | "
                      f"{test['error'].get('error_detail')}")
                continue
            r0 = (test.get("result") or {}).get("UpdateResults", [{}])[0]
            print(f"  цена [{field}]: {json.dumps(r0, ensure_ascii=False)}")
            if r0.get("Id") and not r0.get("Errors"):
                break

    print("\n### итог")
    chk = need(call("ads", {
        "SelectionCriteria": {"CampaignIds": [CAMPAIGN], "Types": ["TEXT_AD"]},
        "FieldNames": ["Id"], "TextAdFieldNames": ["Title", "Title2"],
        "Page": {"Limit": 1000}}, login, token), "сверка")
    rows = chk.get("Ads", [])
    with_promo = sum(1 for a in rows
                     if PROMO_TAG in ((a.get("TextAd") or {}).get("Title") or ""))
    with_t2 = sum(1 for a in rows if (a.get("TextAd") or {}).get("Title2"))
    print(f"  ТГО {len(rows)}: с «{PROMO_TAG}» в первом заголовке {with_promo}, "
          f"со вторым заголовком {with_t2}")
    variants: dict = {}
    for a in rows:
        t2 = (a.get("TextAd") or {}).get("Title2") or ""
        variants[t2] = variants.get(t2, 0) + 1
    for t2, n in sorted(variants.items(), key=lambda x: -x[1]):
        print(f"  {n}x «{t2}»")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
