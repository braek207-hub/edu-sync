"""Вернуть 715011487 набор заголовков — все с акцией −15% до 11 октября — через v501.

Что случилось: ads.update в v5 пишет комбинаторное объявление как одиночное и урезает
набор заголовков до одного (ровно warning 10252). Сверка показала: в образце 714000003
у ТГО по 7 заголовков, в дубле осталось по 1. Тексты (по 3) целы.

Правильный путь — v501 полем ResponsiveAd.Titles массивом. Название модели берём из
нетронутого образца, сопоставляя объявления по Href, и собираем до 7 вариантов
заголовка: во всех есть и −15%, и срок.

APPLY=1 — писать. Без него план.
"""

from __future__ import annotations

import json
import os
import re
import sys

import requests

DUP = 715011487
SOURCE = 714000003
EXPECTED_LOGIN_PART = "bjorn"
TITLE_MAX = 56
WORD_MAX = 22
MAX_TITLES = 7

# Порядок важен: берём первые влезающие, пока не наберём MAX_TITLES
CANDIDATES = [
    "{p} BJORN LARSEN: -15% до 11 октября",
    "{p}: скидка 15% только до 11 октября",
    "{p} со скидкой 15% — до 11 октября",
    "{p}: -15%, успейте до 11 октября",
    "{p} BJORN LARSEN: скидка 15% до 11.10",
    "{p}: -15% только до 11 октября",
    "{p}: скидка 15% до 11 октября",
    "{p}: -15% по 11 октября",
    "{p}: скидка 15%, до 11.10",
    "{p}: -15% до 11.10",
]


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


def longest_word(text: str) -> int:
    return max((len(w) for w in re.split(r"[\s\-/]+", text) if w), default=0)


def model_from_title(title: str) -> str:
    base = re.sub(r"\s*-15%\s*$", "", title)
    return base.split(" BJORN LARSEN")[0].split(":")[0].strip()


def build_titles(model: str) -> list[str]:
    out: list[str] = []
    for tpl in CANDIDATES:
        for cand in (tpl.format(p=model), tpl.format(p=model).replace(" BJORN LARSEN", "")):
            if len(cand) <= TITLE_MAX and longest_word(cand) <= WORD_MAX \
                    and cand not in out:
                out.append(cand)
                break
        if len(out) >= MAX_TITLES:
            break
    return out


def read_ads(camp: int, login: str, token: str) -> list[dict]:
    res = need(call("v501", "ads", {
        "SelectionCriteria": {"CampaignIds": [camp]},
        "FieldNames": ["Id", "AdGroupId", "Type"],
        "ResponsiveAdFieldNames": ["Titles", "Texts", "Href"],
        "Page": {"Limit": 1000}}, login, token), f"объявления {camp}")
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
    print(f"кабинет {login} | режим: {'ЗАПИСЬ' if apply else 'план'}")

    dup = read_ads(DUP, login, token)
    src = read_ads(SOURCE, login, token)
    by_href: dict[str, dict] = {}
    for a in src:
        href = (a.get("ResponsiveAd") or {}).get("Href") or ""
        if href:
            by_href.setdefault(href, a)
    print(f"дубль {DUP}: {len(dup)} ТГО | образец {SOURCE}: {len(src)} ТГО\n")

    plan, problems = [], []
    for a in sorted(dup, key=lambda x: x["Id"]):
        ra = a.get("ResponsiveAd") or {}
        href = ra.get("Href") or ""
        origin = by_href.get(href)
        source_titles = [t.get("Title") for t in ((origin or {}).get("ResponsiveAd")
                                                  or {}).get("Titles", [])]
        own_titles = [t.get("Title") for t in ra.get("Titles", [])]
        seed = next((t for t in source_titles + own_titles if t), "")
        if not seed:
            problems.append((a["Id"], "нет ни одного заголовка ни в дубле, ни в образце"))
            continue
        model = model_from_title(seed)
        titles = build_titles(model)
        if len(titles) < 3:
            problems.append((a["Id"], f"для «{model}» собралось вариантов: {len(titles)}"))
            continue
        plan.append((a["Id"], model, titles, len(own_titles), len(source_titles)))
        print(f"  {a['Id']} «{model}»: было {len(own_titles)}, в образце "
              f"{len(source_titles)}, станет {len(titles)}")
        for t in titles:
            print(f"      {len(t):>2}/{TITLE_MAX} {t}")

    if problems:
        print("\nне сопоставлено:")
        for aid, why in problems:
            print(f"  ! {aid} — {why}")

    if not apply:
        print(f"\nбез APPLY=1 ничего не меняю (к записи {len(plan)})")
        return

    # Форма записи Titles в v501 неизвестна наверняка — пробуем объекты, затем строки
    for form in ("objects", "strings"):
        payload = []
        for aid, _model, titles, _a, _b in plan:
            items = [{"Title": t} for t in titles] if form == "objects" else titles
            payload.append({"Id": aid, "ResponsiveAd": {"Titles": items}})
        out = call("v501", "ads", {"Ads": payload}, login, token, "update")
        if out.get("error"):
            print(f"форма {form}: {out['error'].get('error_string')} | "
                  f"{out['error'].get('error_detail')}")
            continue
        rows = (out.get("result") or {}).get("UpdateResults", [])
        ok = sum(1 for r in rows if r.get("Id") and not r.get("Errors"))
        print(f"форма {form}: обновлено {ok} из {len(payload)}")
        errs: dict = {}
        for r in rows:
            for er in r.get("Errors", []):
                errs[(er.get("Code"), er.get("Message"))] = \
                    errs.get((er.get("Code"), er.get("Message")), 0) + 1
        for (code, msg), n in errs.items():
            print(f"    ошибка {code} x{n}: {msg}")
        if ok:
            break

    print("\n### итог")
    rows = read_ads(DUP, login, token)
    acc: dict = {}
    bad = []
    for a in rows:
        titles = [t.get("Title") or "" for t in (a.get("ResponsiveAd") or {}).get("Titles", [])]
        acc[len(titles)] = acc.get(len(titles), 0) + 1
        for t in titles:
            if "-15%" not in t and "скидка 15%" not in t.lower():
                bad.append((a["Id"], t))
    print(f"  заголовков на объявление: {acc}")
    print(f"  заголовков без упоминания скидки: {len(bad)}")
    for aid, t in bad[:5]:
        print(f"    ! {aid} «{t}»")
    texts = {len((a.get("ResponsiveAd") or {}).get("Texts") or []) for a in rows}
    print(f"  текстов на объявление: {sorted(texts)}")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
