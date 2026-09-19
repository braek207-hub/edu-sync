# -*- coding: utf-8 -*-
"""Смена фазы текстов в кампаниях новой структуры (cities.NEW: Видео / Баннеры / Ретаргет).

    python -m sync.russever.phase <slug> [--go] [--date YYYY-MM-DD]

У каждого комбинаторного объявления 3 заголовка и 3 текста (сборка 19.09):
окно из 3 заголовков со сдвигом по номеру объявления внутри группы, тексты — первые 3 фазы. Быстрые ссылки — новый набор на фазу. Графические (IMAGE_AD)
без текста, их не трогаем. Без --go — только показать, что будет залито.
"""
import datetime as dt
import sys
from typing import Any, Dict, List

from sync.russever import direct
from sync.russever.cities import CITY, L, NEW, URL
from sync.russever.copy import texts, titles

API5 = "https://api.direct.yandex.com/json/v5/"


def _call5(service: str, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Наборы быстрых ссылок есть только в v5 — тот же вызов, другой хост."""
    saved = direct.API501
    direct.API501 = API5
    try:
        return direct.call(service, method, params) or {}
    finally:
        direct.API501 = saved


def main(argv: List[str]) -> int:
    slug = argv[0]
    go = "--go" in argv
    day = dt.date.fromisoformat(argv[argv.index("--date") + 1]) if "--date" in argv else dt.date.today()
    C = CITY[slug]
    camps = NEW[slug]
    ph, T = titles(C, day)
    _, TX, LK = texts(C, day)
    assert all(L(t) <= 56 for t in T) and all(L(t, True) <= 81 for t in TX)
    href = f"https://rossever-expo.ru/{URL.get(slug, slug)}"
    print(f"{C['город']} · фаза «{ph}» на {day} · кампании {camps}")
    for t in T:
        print(f"  T {L(t):>2} {t}")
    for t in TX:
        print(f"  X {L(t, True):>2} {t}")

    ads: List[Dict[str, Any]] = []
    for cid in camps.values():
        r = direct.call("ads", "get", {
            "SelectionCriteria": {"CampaignIds": [cid], "Types": ["RESPONSIVE_AD"], "States": ["ON", "OFF"]},
            "FieldNames": ["Id", "AdGroupId"], "Page": {"Limit": 1000}})
        if r is None:
            print("кабинет не прочитан (нет токена или ошибка API)")
            return 1
        ads += r.get("Ads", [])
    ads.sort(key=lambda a: (a["AdGroupId"], a["Id"]))
    pos: Dict[int, int] = {}   # номер объявления внутри группы — как t3(k) при сборке
    for a in ads:
        a["k"] = pos.get(a["AdGroupId"], 0)
        pos[a["AdGroupId"]] = a["k"] + 1
    print(f"  объявлений к обновлению: {len(ads)}")
    if not go:
        return 0

    sl = _call5("sitelinks", "add", {"SitelinksSets": [{"Sitelinks": [
        {"Title": a, "Description": b, "Href": href} for a, b in LK]}]})
    sl_id = (sl.get("AddResults") or [{}])[0].get("Id")
    if not sl_id:
        print("набор ссылок не создан:", sl)
        return 1
    print("  набор ссылок:", sl_id)

    ok = 0
    errs: List[Any] = []
    for i in range(0, len(ads), 100):
        batch = [{"Id": a["Id"], "ResponsiveAd": {"Titles": [T[(3 * a["k"] + n) % len(T)] for n in range(3)],
                                                  "Texts": TX[:3], "SitelinkSetId": sl_id}}
                 for a in ads[i:i + 100]]
        res = (direct.call("ads", "update", {"Ads": batch}) or {}).get("UpdateResults", [])
        ok += sum(1 for u in res if u.get("Id"))
        errs += [u.get("Errors") for u in res if not u.get("Id")]
    print(f"  обновлено {ok}/{len(ads)}", ("· ошибки: " + str(errs[:3])) if errs else "")

    # чтение обратно: первый заголовок фазы должен встретиться в кабинете
    live = direct.campaign_titles(camps["Видео"])
    print("  проверка:", "OK" if T[0] in live else "РАСХОЖДЕНИЕ", f"({len(live)} заголовков в «Видео»)")
    return 0 if ok == len(ads) and not errs else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
