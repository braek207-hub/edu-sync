# -*- coding: utf-8 -*-
"""Снять рекламу города в 18:00 по местному в день закрытия выставки.

После закрытия дверей клик уже ничего не приносит, а платить за него кабинет
продолжает до полуночи. EndDate тут не помогает: он работает по суткам, а не по
часам, и в последний день реклама крутится весь вечер.

Такт идёт часто (каждый час), а решение принимает сам: для каждого города
считает местное время и останавливает кампании, если в городе уже 18:00 и
сегодня последний день выставки или позже. Это делает его устойчивым к
опозданиям расписания Actions — пропущенный час не отменяет остановку, просто
она случится на запуск позже.

Запуск:
    python -m sync.russever.close_city --dry-run
    python -m sync.russever.close_city --apply
    python -m sync.russever.close_city --apply --now 2026-10-04T15:00  # проверка
"""
import argparse
import datetime as dt
from typing import Any, Dict, List, Optional

from sync.russever import direct
from sync.russever.cities import CITY, MK, NEW, RSYA, SEARCH, TZ

CLOSE_HOUR = 18   # местное время, после которого показы городу не нужны


def local_now(slug: str, now_utc: dt.datetime) -> dt.datetime:
    return now_utc.astimezone(dt.timezone(dt.timedelta(hours=TZ.get(slug, 3))))


def should_stop(slug: str, now_utc: dt.datetime) -> bool:
    """Пора ли гасить город: местное время дошло до 18:00 последнего дня."""
    local = local_now(slug, now_utc)
    end = CITY[slug]["конец"]
    if local.date() > end:
        return True
    return local.date() == end and local.hour >= CLOSE_HOUR


def minutes_left(slug: str, now_utc: dt.datetime) -> Optional[int]:
    """Сколько минут до закрытия, если оно сегодня по местному. Иначе None.

    По этому признаку такт решает, дежурить ли ему дальше: расписание Actions
    опаздывает на часы, и один запуск в день закрытия должен досидеть до 18:00
    сам, а не надеяться на следующий будильник.
    """
    local = local_now(slug, now_utc)
    if local.date() != CITY[slug]["конец"] or local.hour >= CLOSE_HOUR:
        return None
    close = local.replace(hour=CLOSE_HOUR, minute=0, second=0, microsecond=0)
    return int((close - local).total_seconds() // 60)


def city_campaign_ids(slug: str) -> List[int]:
    ids = list((NEW.get(slug) or {}).values())
    ids += list((RSYA.get(slug) or {}).keys())
    ids += list(SEARCH.get(slug) or [])
    return sorted(set(ids))


def run(now_utc: dt.datetime, apply: bool) -> Dict[str, Any]:
    tok = direct.token()
    if not tok:
        return {"error": "нет токена в %s" % direct.TOKEN_ENV}
    live = direct.call("campaigns", "get", {
        "SelectionCriteria": {"States": ["ON"]}, "FieldNames": ["Id", "Name", "State"]}, tok=tok)
    if live is None:
        return {"error": "кабинет не прочитан"}
    on = {c["Id"]: c["Name"] for c in live.get("Campaigns", [])}

    out: Dict[str, Any] = {"stopped": {}, "waiting": {}, "watch": {}, "errors": []}
    to_stop: List[int] = []
    for slug in CITY:
        ids = [cid for cid in city_campaign_ids(slug) if cid in on]
        if not ids:
            continue
        local = local_now(slug, now_utc)
        if should_stop(slug, now_utc):
            out["stopped"][slug] = [on[cid] for cid in ids]
            to_stop += ids
        else:
            out["waiting"][slug] = "%s местное, закрытие %s" % (
                local.strftime("%d.%m %H:%M"), CITY[slug]["конец"].strftime("%d.%m"))
            left = minutes_left(slug, now_utc)
            if left is not None:
                out["watch"][slug] = left
    if apply and to_stop:
        res = direct.call("campaigns", "suspend", {"SelectionCriteria": {"Ids": to_stop}}, tok=tok)
        for r in (res or {}).get("SuspendResults", []):
            if r.get("Errors"):
                out["errors"].append("%s: %s" % (r.get("Id"), r["Errors"]))
        if res is None:
            out["errors"].append("suspend не выполнен — API не ответил")
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--now", default="", help="время UTC вида 2026-10-04T15:00 для проверки")
    args = ap.parse_args(argv)
    now = (dt.datetime.fromisoformat(args.now).replace(tzinfo=dt.timezone.utc)
           if args.now else dt.datetime.now(dt.timezone.utc))
    res = run(now, apply=args.apply)
    if res.get("error"):
        print(res["error"])
        return 1
    for slug, names in res["stopped"].items():
        print("СТОП %s: %s" % (CITY[slug]["город"], ", ".join(names)))
    for slug, why in res["waiting"].items():
        print("ждём %s — %s" % (CITY[slug]["город"], why))
    for e in res["errors"]:
        print("ОШИБКА", e)
    # Маркер для обёртки в Actions: город закрывается сегодня, до 18:00 столько
    # минут — значит прогон должен дежурить, а не выходить.
    if res["watch"]:
        print("WATCH %d" % min(res["watch"].values()))
    if res["stopped"] and args.apply:
        from sync.agent import notify
        text = ["РосСеверЭкспо: выставка закрылась, реклама снята"]
        text += ["%s — %d кампаний" % (CITY[s]["город"], len(v))
                 for s, v in res["stopped"].items()]
        # Мастера кампаний в API v5 не видны вовсе, остановить их может только
        # человек в интерфейсе — поэтому они уходят отдельной строкой с id.
        mk = [(s, MK.get(s) or []) for s in res["stopped"]]
        mk = [(s, ids) for s, ids in mk if ids]
        if mk:
            text.append("")
            text.append("Руками в интерфейсе — Мастера кампаний:")
            text += ["%s: %s" % (CITY[s]["город"], ", ".join(str(i) for i in ids))
                     for s, ids in mk]
        notify.send("\n".join(text))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
