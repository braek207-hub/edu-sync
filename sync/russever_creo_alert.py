# -*- coding: utf-8 -*-
"""Напоминание в Telegram: в каком городе пора менять креативы РосСеверЭкспо.

Креативы заложены под фазы жизни выставки — анонс, открытие, финал, последний
день — и переключаются датами, а не человеком. Такт сравнивает фазу по календарю
с тем, что реально залито в кабинет (заголовки комбинаторного объявления в группе
автотаргета), и пишет только когда есть расхождение или смена наступит завтра.
Пока креативы не обновлены, напоминание приходит каждый день: это делает такт
устойчивым к пропускам расписания Actions, которые здесь обычное дело.

Запуск:
    python -m sync.russever_creo_alert            # боевой, шлёт в TG
    python -m sync.russever_creo_alert --dry-run  # печать без отправки
    python -m sync.russever_creo_alert --date 2026-09-13 --force
"""
import argparse
import datetime as dt
import json
import sys
from typing import Any, Dict, List, Optional

from sync.agent import notify
from sync.russever import direct
from sync.russever.cities import CITY, RSYA, SEARCH, is_over, phase
from sync.russever.copy import texts, titles

MSK = dt.timezone(dt.timedelta(hours=3))
ORDER = ["magadan", "salehard", "urengoy", "yakutsk", "kogalym", "chelyabinsk"]


def today_msk() -> dt.date:
    return dt.datetime.now(MSK).date()


def city_state(slug: str, day: dt.date, tok: Optional[str]) -> Dict[str, Any]:
    """Что с городом сегодня: фаза календаря, фаза в кабинете, смена завтра."""
    C = CITY[slug]
    ph_today = phase(C, day)[0]
    ph_tomorrow = phase(C, day + dt.timedelta(days=1))[0]
    ph_yesterday = phase(C, day - dt.timedelta(days=1))[0]
    want = titles(C, day)[1]
    live: List[str] = []
    read = False
    if tok:
        for gid in RSYA[slug].values():
            got = direct.group_titles(gid, tok=tok)
            if got:
                read = True
                live.extend(got)
    # «Залито» считаем по первому заголовку фазы: он уникален для фазы и не
    # зависит от того, сколько объявлений в группе и все ли уже промодерированы.
    in_place = bool(live) and any(w in live for w in want[:2])
    return {
        "slug": slug, "город": C["город"],
        "фаза": ph_today, "фаза_завтра": ph_tomorrow,
        "смена_завтра": ph_tomorrow != ph_today,
        # Смена сегодня — событие календаря: напоминаем даже когда кабинет
        # прочитать не удалось, иначе в день перехода такт промолчит.
        "смена_сегодня": ph_yesterday != ph_today,
        "кабинет_прочитан": read,
        "уже_залито": in_place if read else None,
        "закончилась": is_over(C, day),
        "конец": C["конец"],
    }


def build_message(day: dt.date, states: List[Dict[str, Any]]) -> Optional[str]:
    """Текст сообщения либо None, если менять нечего."""
    надо: List[Dict[str, Any]] = []
    завтра: List[Dict[str, Any]] = []
    закрылись: List[Dict[str, Any]] = []
    for s in states:
        if s["закончилась"]:
            # Сообщаем один раз — на следующий день после последнего.
            if s["конец"] == day - dt.timedelta(days=1):
                закрылись.append(s)
            continue
        if s["уже_залито"] is False or (s["смена_сегодня"] and s["уже_залито"] is not True):
            надо.append(s)
        elif s["смена_завтра"]:
            завтра.append(s)
    if not (надо or завтра or закрылись):
        return None

    L: List[str] = [f"РосСеверЭкспо · креативы на {day.strftime('%d.%m')}", ""]

    for s in надо:
        C = CITY[s["slug"]]
        ph, T = titles(C, day)
        _, TX, LINKS = texts(C, day)
        camps = ", ".join(str(c) for c in RSYA[s["slug"]])
        причина = ("сегодня переход" if s["смена_сегодня"]
                   else "в кабинете стоит другая фаза")
        L.append(f"МЕНЯТЬ СЕЙЧАС — {s['город']}: нужна фаза «{ph}» ({причина})")
        L.append(f"  РСЯ {camps} (группа автотаргета)"
                 f" · Поиск {', '.join(str(c) for c in SEARCH[s['slug']])}")
        L.append("  Заголовки:")
        L += [f"   {i}. {t}" for i, t in enumerate(T, 1)]
        L.append("  Тексты:")
        L += [f"   {i}. {t}" for i, t in enumerate(TX, 1)]
        L.append(f"  Быстрая ссылка про срок: {LINKS[2][0]}")
        L.append("")

    for s in завтра:
        C = CITY[s["slug"]]
        L.append(f"ЗАВТРА {(day + dt.timedelta(days=1)).strftime('%d.%m')}"
                 f" — {s['город']}: «{s['фаза']}» → «{s['фаза_завтра']}»."
                 f" Тексты придут утром")
    if завтра:
        L.append("")

    for s in закрылись:
        L.append(f"{s['город']}: выставка закончилась {s['конец'].strftime('%d.%m')},"
                 f" кампании отключились по дате")
    if закрылись:
        L.append("")

    прочие = [s for s in states
              if not s["закончилась"] and s not in надо and s not in завтра]
    if прочие:
        L.append("Без изменений: " + ", ".join(
            f"{s['город']} — {s['фаза']}" for s in прочие))
    if any(s["кабинет_прочитан"] is False for s in states):
        L.append("(кабинет не прочитан — сверка только по календарю)")
    return "\n".join(L)


def run(day: Optional[dt.date] = None, dry_run: bool = False,
        force: bool = False) -> Dict[str, Any]:
    day = day or today_msk()
    tok = direct.token() or None
    states = [city_state(s, day, tok) for s in ORDER]
    text = build_message(day, states)
    if text is None and force:
        text = (f"РосСеверЭкспо · {day.strftime('%d.%m')}: менять нечего.\n"
                + "\n".join(f"{s['город']} — {s['фаза']}" for s in states
                            if not s["закончилась"]))
    out: Dict[str, Any] = {
        "day": day.isoformat(),
        "cities": [{k: (v.isoformat() if isinstance(v, dt.date) else v)
                    for k, v in s.items()} for s in states],
        "quiet": text is None,
    }
    if text is None:
        out["sent"] = False
    elif dry_run:
        out["sent"] = False
        out["preview"] = text
    else:
        out.update(notify.send(text))
        out["chars"] = len(text)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="дата в формате ГГГГ-ММ-ДД (умолчание — сегодня по МСК)")
    ap.add_argument("--dry-run", action="store_true", help="печать без отправки")
    ap.add_argument("--force", action="store_true", help="слать даже когда менять нечего")
    a = ap.parse_args()
    day = dt.date.fromisoformat(a.date) if a.date else None
    res = run(day=day, dry_run=a.dry_run, force=a.force)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    if res.get("preview"):
        print("\n" + res["preview"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
