"""Скользящее окно у сегментов ретаргетинга BJORN.

Сегменты «RT: <модель> · Nд · не отказ» несут в выражении абсолютную дату
(ym:s:date>='2026-09-22'), потому что срок в правилах Директа действует только для целей —
у сегментов Метрики он всегда 540 дней и игнорируется. Значит окно надо двигать самим:
каждое утро переписываем дату на «сегодня минус N», где N взят из имени сегмента.

Трогаем только сегменты с префиксом RT: — остальные в счётчике чужие.
APPLY=1 — писать. Без него только показываем, что поменялось бы.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, timedelta

import requests

METRIKA = "https://api-metrika.yandex.net/management/v1"
PREFIX = "RT: "
NAME_WINDOW = re.compile(r"·\s*(\d+)\s*д\s*·")
EXPR_DATE = re.compile(r"(ym:s:date>=')(\d{4}-\d{2}-\d{2})(')")


def main() -> None:
    apply = os.environ.get("APPLY", "").strip() == "1"
    token = os.environ["METRICA_TOKEN"].strip()
    counter = os.environ["METRICA_COUNTER_ID"].strip()
    h = {"Authorization": f"OAuth {token}"}
    print(f"режим: {'ЗАПИСЬ' if apply else 'план, ничего не пишем'}")

    r = requests.get(f"{METRIKA}/counter/{counter}/segments", headers=h, timeout=60)
    if r.status_code != 200:
        sys.exit(f"чтение сегментов: HTTP {r.status_code} {r.text[:300]}")
    segments = r.json().get("segments", [])

    touched = skipped = failed = 0
    for seg in segments:
        name = seg.get("name") or ""
        if not name.startswith(PREFIX):
            continue
        sid = seg.get("segment_id")
        expr = seg.get("expression") or ""

        m_win = NAME_WINDOW.search(name)
        if not m_win:
            print(f"  ! {sid} «{name}»: в имени нет окна «· Nд ·», пропускаю")
            skipped += 1
            continue
        if not EXPR_DATE.search(expr):
            print(f"  ! {sid} «{name}»: в выражении нет ym:s:date, пропускаю")
            skipped += 1
            continue

        window = int(m_win.group(1))
        since = (date.today() - timedelta(days=window)).isoformat()
        new_expr = EXPR_DATE.sub(rf"\g<1>{since}\g<3>", expr)
        if new_expr == expr:
            print(f"  = {sid} «{name}»: уже {since}")
            continue

        old = EXPR_DATE.search(expr).group(2)
        print(f"  → {sid} «{name}»: {old} -> {since}")
        if not apply:
            touched += 1
            continue

        body = json.dumps({"segment": {"name": name, "expression": new_expr}},
                          ensure_ascii=False).encode("utf-8")
        resp = requests.put(f"{METRIKA}/counter/{counter}/segment/{sid}", headers={
            **h, "Content-Type": "application/json; charset=utf-8"}, data=body, timeout=60)
        if resp.status_code not in (200, 201):
            print(f"    ОШИБКА HTTP {resp.status_code} {resp.text[:200]}")
            failed += 1
            continue
        touched += 1

    print(f"\nсдвинуто {touched}, без изменений или пропущено {skipped}, ошибок {failed}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
