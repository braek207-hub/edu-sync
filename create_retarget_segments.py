# -*- coding: utf-8 -*-
"""create_retarget_segments.py — сегменты Метрики для ретаргетинга Директа по интересам.

Идея (Павел, 2026-09-29): пользователь пришёл с поиска на ленд, направление интереса
известно по кампании-источнику (utm_campaign={campaign_id} стоит на всех объявлениях).
Сегмент Метрики = «визит с utm_campaign из списка кампаний направления». Дальше в Директе
условие ретаргетинга «был в сегменте, NONE заявка/отказы» и группа ЕПК с релевантным
посылом.

В списки входят ТОЛЬКО поисковые кампании: РСЯ и ретаргетинг исключены, иначе показ
ретаргета сам освежает сегмент (кольцо).

Идемпотентен: сегмент ищется по имени, существующий не пересоздаётся.
ENV: YM_TOKEN. Печатает JSON-маппинг {account: {slug: segment_id}} между маркерами
SEGMENTS_JSON_BEGIN/END — его забирает direct/retarget_interest.py в EDU кампании.
"""
import json
import os
import sys
import time

import requests

BASE = "https://api-metrika.yandex.net/management/v1/counter/{counter}/segments"

COUNTERS = {"vuz": 98627983, "vse": 96526110}

# Направление -> список ПОИСКОВЫХ кампаний-источников (id из кабинетов, 2026-09-29).
DIRECTIONS = {
    "vuz": {
        "med": [114141005, 114140964, 114141023, 708250353, 708276742, 709492658,
                709492661],
        "it": [709413835, 708885628],
        "college": [114113331, 114113342, 114113366, 114113385, 114113406, 114113414,
                    114113451, 114141054, 708070153, 709492499, 709492478, 709492654],
        "distant": [709090165, 709447342, 709492682, 709927544, 713822188, 713823285,
                    713823643, 713824087],
        "vpo": [704810783, 708468997, 710374054, 710464105, 710464138, 710464174,
                710464201, 712948570, 712973050, 114140985, 709888111, 710462474,
                114140971],
        "mag": [713251116],
        "asp": [713248859, 713668859],
        "perevod": [704114800, 704776259, 706944203, 706951103, 709492629],
    },
    "vse": {
        "med": [118609715, 701478302, 701950613, 709657165, 710369949],
        "design": [119480903, 709231644],
        "tech": [117986057, 119194059, 709231619],
        "it": [117932829],
        "college": [114057545, 707532491, 709928682, 713687358],
        "distant": [118569187, 712940873, 713935813],
        "vpo": [115470324, 710117793, 710118280, 710178257, 710687014, 712704859,
                712860984],
        "mag": [714032299, 714420887],
        "asp": [713148609, 714146085],
        "perevod": [706961734, 706993601],
    },
}

TITLES = {
    "med": "Медицина", "it": "IT", "college": "Колледж", "distant": "Дистанционное",
    "vpo": "Высшее", "mag": "Магистратура", "asp": "Аспирантура", "perevod": "Перевод",
    "design": "Дизайн", "tech": "Технические",
}


def _headers():
    return {"Authorization": f"OAuth {os.environ['YM_TOKEN'].strip()}",
            "Content-Type": "application/json"}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    out = {}
    for account, counter in COUNTERS.items():
        url = BASE.format(counter=counter)
        r = requests.get(url, headers=_headers(), timeout=60)
        r.raise_for_status()
        existing = {s["name"]: s for s in r.json().get("segments", [])}
        print(f"=== {account} (счётчик {counter}): сегментов уже {len(existing)}")
        for s in existing.values():
            print(f"  [{s['segment_id']}] {s['name']} :: {s.get('expression', '')[:100]}")

        out[account] = {}
        for slug, camp_ids in DIRECTIONS[account].items():
            name = f"Ретаргет-интерес: {TITLES[slug]} (поиск)"
            ids = ",".join(f"'{i}'" for i in camp_ids)
            expression = f"ym:s:UTMCampaign IN({ids})"
            if name in existing:
                seg = existing[name]
                print(f"  {slug}: уже есть -> {seg['segment_id']}")
                out[account][slug] = seg["segment_id"]
                continue
            resp = requests.post(url, headers=_headers(), timeout=60,
                                 data=json.dumps({"segment": {
                                     "name": name, "expression": expression}},
                                     ensure_ascii=False).encode("utf-8"))
            if resp.status_code != 200:
                print(f"  {slug}: ОШИБКА {resp.status_code} {resp.text[:300]}")
                return 1
            seg = resp.json()["segment"]
            print(f"  {slug}: создан -> {seg['segment_id']}")
            out[account][slug] = seg["segment_id"]
            time.sleep(0.3)

    print("\nSEGMENTS_JSON_BEGIN")
    print(json.dumps(out, ensure_ascii=False))
    print("SEGMENTS_JSON_END")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
