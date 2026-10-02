"""Сегмент «интерес к каталогу» на BJORN — широкая качественная аудитория для ретаргета.

Замер за 30 дней (02.10.2026): всего 68 648 человек, из них блог 49 401 — почти три
четверти трафика информационные, в ретаргет им нельзя. Коммерческое ядро — каталог и
карточки товара: 16 469 человек, после отсечения отказов 12 092, без купивших 12 070.
Это и берём: порог Яндекс Аудиторий — 100 человек, запас стократный.

Глубину и время не докручиваем: «+2 страницы» срезает до 9 720, «+60 секунд» до 5 815,
а факт захода в каталог уже отделяет интерес от случайного чтения блога.

Имя в формате «RT: … · 30д · не отказ» — его подхватывает крон slide_bjorn_rt_segments.py
и каждое утро сдвигает дату в выражении. Это обязательно: для сегментов Метрики Директ
игнорирует срок в правилах ретаргетинга, поэтому окно живёт только внутри выражения.

Сегмент адресован Директу, не Аудиториям. Созданный через API сегмент получает
segment_source=api и не показывается ни в интерфейсе Метрики, ни в списке Аудиторий —
только в условиях подбора аудитории Директа (здесь условие 42777316). Порог Аудиторий
в 100 человек к нему не применяется; охват считаем, чтобы знать объём, а не чтобы пройти
проверку.

APPLY=1 — писать. Без него только показываем выражение и охват.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta

import requests

MGMT = "https://api-metrika.yandex.net/management/v1"
STAT = "https://api-metrika.yandex.net/stat/v1/data"
WINDOW = 30
GOAL_PURCHASE = 341172800
SEG_NAME = f"RT: интерес к каталогу · {WINDOW}д · не отказ"


def main() -> None:
    apply = os.environ.get("APPLY", "").strip() == "1"
    token = os.environ["METRICA_TOKEN"].strip()
    counter = os.environ["METRICA_COUNTER_ID"].strip()
    h = {"Authorization": f"OAuth {token}"}
    since = (date.today() - timedelta(days=WINDOW)).isoformat()

    expr = (f"ym:pv:URL=~'/(catalog|product)/' AND ym:s:bounce=='No' "
            f"AND ym:s:date>='{since}' "
            f"AND NOT(ym:s:goal{GOAL_PURCHASE}IsReached=='Yes')")
    print(f"режим: {'ЗАПИСЬ' if apply else 'план'}")
    print(f"имя:       {SEG_NAME}")
    print(f"выражение: {expr}\n")

    r = requests.get(STAT, headers=h, params={
        "ids": counter, "metrics": "ym:s:users,ym:s:visits",
        "date1": since, "date2": (date.today() - timedelta(days=1)).isoformat(),
        "accuracy": "full",
        "filters": (f"ym:pv:URL=~'/(catalog|product)/' AND ym:s:bounce=='No' "
                    f"AND NOT(ym:s:goal{GOAL_PURCHASE}IsReached=='Yes')")}, timeout=120)
    if r.status_code == 200:
        t = r.json().get("totals", [0, 0])
        print(f"охват за {WINDOW} дней: {int(t[0])} человек, {int(t[1])} визитов")
        if int(t[0]) < 100:
            sys.exit("охват меньше 100 человек — для ретаргета бессмысленно")
    else:
        print(f"охват посчитать не удалось: HTTP {r.status_code} {r.text[:200]}")

    rs = requests.get(f"{MGMT}/counter/{counter}/segments", headers=h, timeout=60)
    if rs.status_code != 200:
        sys.exit(f"чтение сегментов: HTTP {rs.status_code} {rs.text[:200]}")
    existing = {s.get("name"): s for s in rs.json().get("segments", [])}

    if SEG_NAME in existing:
        sid = existing[SEG_NAME]["segment_id"]
        print(f"\nсегмент уже есть: {sid}")
        if existing[SEG_NAME].get("expression") == expr:
            print("выражение совпадает — менять нечего")
            return
        if not apply:
            print("выражение отличается, в режиме записи перепишу")
            return
        body = json.dumps({"segment": {"name": SEG_NAME, "expression": expr}},
                          ensure_ascii=False).encode("utf-8")
        ru = requests.put(f"{MGMT}/counter/{counter}/segment/{sid}", headers={
            **h, "Content-Type": "application/json; charset=utf-8"}, data=body, timeout=60)
        print(f"обновление: HTTP {ru.status_code} {ru.text[:200]}")
        return

    if not apply:
        print("\nсегмента нет, в режиме записи создам")
        return
    body = json.dumps({"segment": {"name": SEG_NAME, "expression": expr}},
                      ensure_ascii=False).encode("utf-8")
    rp = requests.post(f"{MGMT}/counter/{counter}/segments", headers={
        **h, "Content-Type": "application/json; charset=utf-8"}, data=body, timeout=60)
    print(f"\nсоздание: HTTP {rp.status_code}")
    if rp.status_code not in (200, 201):
        sys.exit(rp.text[:400])
    seg = rp.json().get("segment", {})
    print(f"создан сегмент {seg.get('segment_id')} «{seg.get('name')}»")
    print(f"выражение в Метрике: {seg.get('expression')}")


if __name__ == "__main__":
    main()
