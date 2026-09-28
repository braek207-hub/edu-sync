# -*- coding: utf-8 -*-
"""Ежедневный чекап кабинета РосСеверЭкспо: бюджеты, площадки, качество.

Раз в сутки такт смотрит на кабинет глазами директолога и сам вносит правки:

1. **Бюджеты.** План недели берётся из таблицы Павла (`cities.BUDGET`), из него
   вычитается уже потраченное с понедельника, остаток раскладывается по ролям
   кампаний на оставшиеся дни. Недотрата прошлой недели переносится вперёд,
   перетрата вычитается — но только там, где город физически откручивает свой
   бюджет: маленькому городу поднимать лимит бессмысленно, он упирается в
   инвентарь, а не в деньги.
2. **Площадки.** Вчерашний отчёт по площадкам: мусор по словарю чистильщика
   (приложения, игры, внешние сети DSP) плюс площадки, где при заметном объёме
   отказы выше порога. Запрет пишется во все кампании города сразу — площадка,
   сливающая бюджет в одной, сольёт и в соседней.
3. **Качество.** Отказы, цена клика, цена цели и доля видео против требований
   Павла. Что не сошлось — попадает в сообщение с причиной, а не просто
   «показатель красный».

Ставка поднимается, когда кампания не откручивает бюджет при хорошем качестве,
и опускается, когда качество плохое, — обратное («дёшево, но мусор») лечится
площадками, а не ставкой.

Запуск:
    python -m sync.russever.checkup --dry-run    # только показать
    python -m sync.russever.checkup --apply      # записать и отправить в TG
"""
import argparse
import datetime as dt
import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from sync.russever import direct
from sync.russever.cities import (BUDGET, CITY, NEW, ROLE_CEILING, ROLE_SHARE,
                                  RSYA, is_over, phase)

MSK = dt.timezone(dt.timedelta(hours=3))
M = 1_000_000
API5 = "https://api.direct.yandex.com/json/v5/"
REPORTS = API5 + "reports"

# Требования Павла к трафику (26.09.2026).
MAX_BOUNCE = 30.0        # доля отказов, %
MAX_CPC = 15.0           # цена клика, ₽
VIDEO_SHARE = (40.0, 50.0)   # доля кликов видео-кампаний, %

# Пороги для запрета площадки по вчерашнему дню. Ниже 30 кликов статистика
# ничего не доказывает: одна случайная сессия двигает отказы на десятки пунктов.
MIN_CLICKS = 30
BAD_BOUNCE = 55.0
BAD_CPA_FACTOR = 3.0     # во сколько раз цель дороже средней по кабинету
BLOCK_CEILING = 950      # сколько слотов из 1000 вправе занять робот
DAY_BLOCK_CAP = 40       # сколько площадок такт вправе запретить за день

# Кампании, которые API не видит (Мастер кампаний), чистятся руками — такт
# только показывает их расход, чтобы он не терялся из бюджета города.
ROLES = ("Видео", "Баннеры", "Ретаргет")


def _call5(service: str, method: str, params: Dict[str, Any],
           tok: str) -> Dict[str, Any]:
    body = json.dumps({"method": method, "params": params},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API5 + service, data=body, headers={
        "Authorization": "Bearer " + tok, "Client-Login": direct.LOGIN,
        "Accept-Language": "ru", "Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        j = json.loads(resp.read().decode("utf-8"))
    if "error" in j:
        raise RuntimeError("%s.%s: %s" % (service, method, j["error"]))
    return j.get("result") or {}


def report(tok: str, d1: str, d2: str, fields: List[str],
           campaign_ids: Optional[List[int]] = None) -> List[Dict[str, str]]:
    """Отчёт Директа со всеми граблями очереди.

    `DateTo = сегодня` заставляет отчёт висеть в очереди вечно — поэтому дата
    только вчерашняя и раньше. Занятая очередь отвечает HTTP 500, а не кодом
    ошибки Директа: это не отказ, а «подожди».
    """
    sel: Dict[str, Any] = {"DateFrom": d1, "DateTo": d2}
    if campaign_ids:
        sel["Filter"] = [{"Field": "CampaignId", "Operator": "IN",
                          "Values": [str(x) for x in campaign_ids]}]
    body = {"params": {"SelectionCriteria": sel, "FieldNames": fields,
                       "ReportName": "checkup-%s" % dt.datetime.now().strftime("%H%M%S%f"),
                       "ReportType": "CUSTOM_REPORT", "DateRangeType": "CUSTOM_DATE",
                       "Format": "TSV", "IncludeVAT": "YES", "IncludeDiscount": "NO"}}
    req = urllib.request.Request(REPORTS, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + tok, "Client-Login": direct.LOGIN,
                 "Accept-Language": "ru", "processingMode": "auto",
                 "returnMoneyInMicros": "false", "skipReportHeader": "true",
                 "skipReportSummary": "true",
                 "Content-Type": "application/json; charset=utf-8"})
    for _ in range(80):
        try:
            resp = urllib.request.urlopen(req, timeout=300)
        except urllib.error.HTTPError as exc:
            if exc.code != 500:
                raise
            time.sleep(15)
            continue
        if resp.status == 200:
            lines = resp.read().decode("utf-8").splitlines()
            if not lines:
                return []
            head = lines[0].split("\t")
            return [dict(zip(head, ln.split("\t"))) for ln in lines[1:] if ln.strip()]
        time.sleep(5)
    raise RuntimeError("отчёт не готов")


def money(x: float) -> str:
    """Рубли с неразрывным разделителем тысяч — чтобы читалось с телефона."""
    return f"{x:,.0f}".replace(",", " ")


def num(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def week_start(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def plan_of(slug: str, monday: dt.date) -> int:
    """План недели по таблице; для недели, которой в таблице нет, — последняя
    заданная. Ноль означает «город в плане не значится», и бюджет не трогаем."""
    table = BUDGET.get(slug) or {}
    if not table:
        return 0
    key = monday.isoformat()
    if key in table:
        return table[key]
    past = [k for k in sorted(table) if k <= key]
    return table[past[-1]] if past else 0


LIVE: set = set()   # id запущенных кампаний; заполняется в main перед расчётом


def city_campaigns(slug: str) -> Dict[int, str]:
    """Кампании города и их роль: три кампании новой структуры плюс старая РСЯ,
    если она ещё запущена (в Мирном и Сургуте одна такая осталась с прежней
    схемы, вторая в том же справочнике уже остановлена)."""
    out = {cid: role for role, cid in (NEW.get(slug) or {}).items()}
    for cid in (RSYA.get(slug) or {}):
        if not LIVE or cid in LIVE:
            out.setdefault(cid, "РСЯ")
    return {cid: role for cid, role in out.items() if not LIVE or cid in LIVE}


def active_cities(day: dt.date) -> List[str]:
    """Города, которым сегодня нужна реклама: выставка идёт или скоро."""
    return [slug for slug, C in CITY.items()
            if not is_over(C, day) and plan_of(slug, week_start(day))]


def collect(tok: str, day: dt.date) -> Dict[str, Any]:
    """Факты: расход недели по кампаниям и вчерашнее качество."""
    monday = week_start(day)
    yday = day - dt.timedelta(days=1)
    # Считаем с первой недели плана: сальдо города — это весь его план против
    # всего его факта, иначе недотрата предыдущих недель посчитается дважды.
    first = min((k for table in BUDGET.values() for k in table), default=monday.isoformat())
    rows = report(tok, first, yday.isoformat(),
                  ["Date", "CampaignId", "CampaignName", "Cost", "Clicks",
                   "Impressions", "BounceRate", "Conversions"])
    return {"rows": rows, "monday": monday, "yday": yday, "first": first}


def city_of(name: str) -> Optional[str]:
    """Город по имени кампании: имена строятся как «Город // Роль» и «Город / МК»."""
    for slug, C in CITY.items():
        if name.startswith(C["город"]):
            return slug
    return None


def budget_plan(tok: str, day: dt.date, facts: Dict[str, Any],
                qual: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Недельный лимит каждой кампании города.

    Деньги города из города не уходят: сальдо переносится только между его
    собственными неделями, и сумма всех недель не может превысить итог города
    по таблице. Недотратил на прошлой неделе — прибавили к текущей; перетратил —
    вычли. Внутри недели Директ раскладывает бюджет по дням сам, поэтому лимит
    ставится на неделю целиком, без ускорения к выходным: WeeklySpendLimit
    считается за календарную неделю и уже потраченное учитывает.
    """
    monday = facts["monday"]
    spent_week: Dict[str, float] = {}
    spent_before: Dict[str, float] = {}   # с начала истории города до этой недели
    spent_camp: Dict[int, float] = {}
    mk: Dict[str, float] = {}
    for r in facts["rows"]:
        slug = city_of(r["CampaignName"])
        if not slug:
            continue
        cost = num(r["Cost"])
        if r["Date"] >= monday.isoformat():
            spent_week[slug] = spent_week.get(slug, 0.0) + cost
            spent_camp[int(r["CampaignId"])] = spent_camp.get(int(r["CampaignId"]), 0.0) + cost
            if "МК" in r["CampaignName"]:
                mk[slug] = mk.get(slug, 0.0) + cost
        else:
            spent_before[slug] = spent_before.get(slug, 0.0) + cost

    out: List[Dict[str, Any]] = []
    for slug in active_cities(day):
        table = BUDGET.get(slug) or {}
        total = sum(table.values())
        plan = plan_of(slug, monday)
        # Сальдо прошлых недель: план до текущей недели минус факт до неё.
        planned_before = sum(v for k, v in table.items() if k < monday.isoformat())
        carry = planned_before - spent_before.get(slug, 0.0)
        # Итог города — потолок: сальдо не может вывести расход за общий бюджет.
        left_total = max(total - spent_before.get(slug, 0.0), 0.0)
        week = min(max(plan + carry, 0.0), left_total)
        # Выставка продолжается, а планов на будущие недели в таблице нет —
        # значит остаток города делится поровну, иначе одна неделя съест всё
        # и на последние дни выставки денег не останется.
        future = sum(v for k, v in table.items() if k > monday.isoformat())
        weeks_left = max((CITY[slug]["конец"] - monday).days // 7 + 1, 1)
        spread = not future and weeks_left > 1
        if spread:
            week = left_total / weeks_left
        shares = role_shares(qual.get(slug) if qual else None)
        for cid, role in city_campaigns(slug).items():
            share = shares.get(role, 0.0)
            if not share:
                continue
            limit = round(week * share / 100) * 100
            out.append({"city": slug, "campaign_id": cid, "role": role,
                        "week_plan": week, "plan": plan, "carry": carry,
                        "spread": spread, "weeks_left": weeks_left,
                        "total": total, "spent_total": spent_before.get(slug, 0.0),
                        "spent": spent_week.get(slug, 0.0),
                        "mk_spent": mk.get(slug, 0.0),
                        "limit": max(int(limit), 2100),
                        "ceiling": ROLE_CEILING.get(role, 15),
                        "camp_spent": spent_camp.get(cid, 0.0)})
    return out


def role_shares(q: Optional[Dict[str, Any]]) -> Dict[str, float]:
    """Доли ролей с поправкой на вчерашнюю долю видео.

    Павел держит видео в коридоре 40–50 % кликов. Вышли ниже — сдвигаем пять
    пунктов бюджета от Баннеров к Видео, выше — обратно. Сдвиг ограничен, чтобы
    один шумный день не перекроил раскладку.
    """
    shares = dict(ROLE_SHARE)
    if not q or not q.get("video_share"):
        return shares
    lo, hi = VIDEO_SHARE
    step = 0.05
    if q["video_share"] < lo and shares["Баннеры"] > 0.20:
        shares["Видео"] += step
        shares["Баннеры"] -= step
    elif q["video_share"] > hi and shares["Видео"] > 0.20:
        shares["Видео"] -= step
        shares["Баннеры"] += step
    return shares


def quality(facts: Dict[str, Any]) -> Dict[str, Any]:
    """Вчерашние показатели по городам и доля видео."""
    yday = facts["yday"].isoformat()
    by: Dict[str, Dict[str, float]] = {}
    video: Dict[str, float] = {}
    total: Dict[str, float] = {}
    for r in facts["rows"]:
        if r["Date"] != yday:
            continue
        slug = city_of(r["CampaignName"])
        if not slug:
            continue
        clicks = num(r["Clicks"])
        a = by.setdefault(slug, {"clicks": 0.0, "cost": 0.0, "bounce": 0.0, "conv": 0.0})
        a["clicks"] += clicks
        a["cost"] += num(r["Cost"])
        a["bounce"] += num(r["BounceRate"]) * clicks
        a["conv"] += num(r["Conversions"])
        total[slug] = total.get(slug, 0.0) + clicks
        if int(r["CampaignId"]) == (NEW.get(slug) or {}).get("Видео"):
            video[slug] = video.get(slug, 0.0) + clicks
    out = {}
    for slug, a in by.items():
        if not a["clicks"]:
            continue
        out[slug] = {
            "clicks": a["clicks"], "cost": a["cost"],
            "cpc": a["cost"] / a["clicks"],
            "bounce": a["bounce"] / a["clicks"],
            "conv": a["conv"],
            "cpa": a["cost"] / a["conv"] if a["conv"] else 0.0,
            "video_share": video.get(slug, 0.0) / total[slug] * 100 if total.get(slug) else 0.0,
        }
    return out


def bad_placements(tok: str, day: dt.date, campaign_ids: List[int]) -> Dict[str, str]:
    """Площадки вчерашнего дня, которые надо запретить по КАЧЕСТВУ трафика.

    Мусор по имени (приложения, игры, внешние сети) здесь не ищем: это работа
    чистильщика площадок, он ходит каждые три часа и берёт топ по кликам. Чекап
    добирает то, чего чистильщик не видит в принципе, — площадку с приличным
    именем, которая даёт отказы и не даёт целей. Ворота по расходу держат список
    коротким: тысяча слотов на кампанию — расходный ресурс, а не свалка.
    """
    yday = (day - dt.timedelta(days=1)).isoformat()
    rows = report(tok, yday, yday,
                  ["Placement", "Cost", "Clicks", "BounceRate", "Conversions"],
                  campaign_ids=campaign_ids)
    agg: Dict[str, List[float]] = {}
    for r in rows:
        site = r["Placement"].strip()
        if not site:
            continue
        clicks = num(r["Clicks"])
        a = agg.setdefault(site.lower(), [0.0, 0.0, 0.0, 0.0])
        a[0] += clicks
        a[1] += num(r["Cost"])
        a[2] += num(r["BounceRate"]) * clicks
        a[3] += num(r["Conversions"])
    total_cost = sum(a[1] for a in agg.values())
    total_conv = sum(a[3] for a in agg.values())
    avg_cpa = total_cost / total_conv if total_conv else 0.0

    pick: Dict[str, str] = {}
    for site, a in sorted(agg.items(), key=lambda kv: -kv[1][1]):
        clicks, cost, bounce_sum, conv = a
        if clicks < MIN_CLICKS:
            continue
        bounce = bounce_sum / clicks
        cpa = cost / conv if conv else 0.0
        if bounce >= BAD_BOUNCE:
            pick[site] = "отказы %.0f %% на %.0f кликах" % (bounce, clicks)
        elif avg_cpa and cpa > avg_cpa * BAD_CPA_FACTOR:
            pick[site] = "цель %.0f ₽ против %.0f ₽ по кабинету" % (cpa, avg_cpa)
        elif not conv and avg_cpa and cost > avg_cpa * BAD_CPA_FACTOR:
            pick[site] = "%.0f ₽ без единой цели" % cost
        if len(pick) >= DAY_BLOCK_CAP:
            break
    return pick


def apply_budgets(tok: str, plan: List[Dict[str, Any]]) -> List[str]:
    """Записать недельные лимиты и потолки ставок."""
    ids = [p["campaign_id"] for p in plan]
    camps = _call5("campaigns", "get", {"SelectionCriteria": {"Ids": ids},
        "FieldNames": ["Id", "Name"],
        "TextCampaignFieldNames": ["BiddingStrategy"],
        "UnifiedCampaignFieldNames": ["BiddingStrategy"]}, tok).get("Campaigns", [])
    by_id = {c["Id"]: c for c in camps}
    updates, notes = [], []
    for p in plan:
        c = by_id.get(p["campaign_id"])
        if not c:
            notes.append("кампания %s не прочитана" % p["campaign_id"])
            continue
        kind = "UnifiedCampaign" if "UnifiedCampaign" in c else "TextCampaign"
        net = c[kind]["BiddingStrategy"]["Network"]
        st = net["BiddingStrategyType"]
        key = {"WB_MAXIMUM_CONVERSION_RATE": "WbMaximumConversionRate",
               "AVERAGE_CPA_MULTIPLE_GOALS": "AverageCpaMultipleGoals",
               "WB_MAXIMUM_CLICKS": "WbMaximumClicks"}.get(st)
        if not key:
            notes.append("%s: стратегия %s — лимит не трогаем" % (c["Name"], st))
            continue
        det = dict(net[key])
        det["WeeklySpendLimit"] = p["limit"] * M
        det["BudgetType"] = "WEEKLY_BUDGET"
        det["BidCeiling"] = p["ceiling"] * M
        det.pop("ExplorationBudget", None)
        updates.append({"Id": c["Id"], kind: {"BiddingStrategy": {
            "Search": {"BiddingStrategyType": "SERVING_OFF"},
            "Network": {"BiddingStrategyType": st, key: det}}}})
    for i in range(0, len(updates), 10):
        res = _call5("campaigns", "update", {"Campaigns": updates[i:i + 10]}, tok)
        for r in res.get("UpdateResults", []):
            if r.get("Errors"):
                notes.append("update %s: %s" % (r.get("Id"), r["Errors"]))
    return notes


def apply_blocks(tok: str, sites: Dict[str, str], campaign_ids: List[int]) -> Tuple[int, List[str]]:
    """Дописать запрет во все кампании. Сравнение без учёта регистра: Директ
    считает com.Foo и com.foo одной площадкой и отбивает такую пару как дубль.

    Список читается прямо перед записью и только дополняется — чистильщик
    площадок ходит по тому же полю каждые 25 минут, и затирать его запреты
    нельзя. Совпасть в одну минуту они могут, но чистильщик добирает своё
    следующим тактом, а такт чекапа — раз в сутки.
    """
    if not sites:
        return 0, []
    camps = _call5("campaigns", "get", {"SelectionCriteria": {"Ids": campaign_ids},
        "FieldNames": ["Id", "Name", "ExcludedSites"]}, tok).get("Campaigns", [])
    updates, notes, added = [], [], 0
    for c in camps:
        cur = (c.get("ExcludedSites") or {}).get("Items") or []
        seen, ded = set(), []
        for s in cur:
            if s.lower() not in seen:
                seen.add(s.lower())
                ded.append(s)
        room = BLOCK_CEILING - len(ded)
        add = [s for s in sorted(sites) if s.lower() not in seen][:max(room, 0)]
        if room <= 0:
            notes.append("%s: список запретов заполнен (%d)" % (c["Name"], len(ded)))
        if add or len(ded) != len(cur):
            updates.append({"Id": c["Id"], "ExcludedSites": {"Items": ded + add}})
            added += len(add)
    for i in range(0, len(updates), 10):
        res = _call5("campaigns", "update", {"Campaigns": updates[i:i + 10]}, tok)
        for r in res.get("UpdateResults", []):
            if r.get("Errors"):
                notes.append("блок %s: %s" % (r.get("Id"), r["Errors"]))
    return added, notes


def message(day: dt.date, plan: List[Dict[str, Any]], qual: Dict[str, Any],
            blocked: int, sites: Dict[str, str], notes: List[str], dry: bool) -> str:
    head = "Чекап РосСеверЭкспо — %s%s" % (day.strftime("%d.%m"), " (репетиция)" if dry else "")
    lines = [head, ""]
    by_city: Dict[str, List[Dict[str, Any]]] = {}
    for p in plan:
        by_city.setdefault(p["city"], []).append(p)
    for slug, items in by_city.items():
        C = CITY[slug]
        q = qual.get(slug) or {}
        ph = phase(C, day)[0]
        it = items[0]
        lines.append("%s · %s · до %s" % (C["город"], ph, C["конец"].strftime("%d.%m")))
        if it.get("spread"):
            lines.append("  неделя: %s ₽ — остаток города поделён на %d недели до закрытия"
                         " (плана на эти недели в таблице нет)"
                         % (money(it["week_plan"]), it["weeks_left"]))
        else:
            lines.append("  неделя: план %s + сальдо %s = %s ₽, потрачено %s ₽ (МК %s ₽)"
                         % (money(it["plan"]), money(it["carry"]), money(it["week_plan"]),
                            money(it["spent"]), money(it["mk_spent"])))
        lines.append("  город всего: %s из %s ₽, осталось %s ₽"
                     % (money(it["spent_total"] + it["spent"]), money(it["total"]),
                        money(it["total"] - it["spent_total"] - it["spent"])))
        if q:
            flags = []
            if q["bounce"] > MAX_BOUNCE:
                flags.append("отказы выше нормы")
            if q["cpc"] > MAX_CPC:
                flags.append("клик дороже 15 ₽")
            if q["video_share"] and not (VIDEO_SHARE[0] <= q["video_share"] <= VIDEO_SHARE[1]):
                flags.append("доля видео вне 40–50 %")
            lines.append("  вчера: клик %.1f ₽, отказы %.0f %%, целей %.0f по %.0f ₽, видео %.0f %%"
                         % (q["cpc"], q["bounce"], q["conv"], q["cpa"], q["video_share"]))
            if flags:
                lines.append("  ⚠ " + "; ".join(flags))
        lines.append("  лимиты: " + ", ".join("%s %s ₽" % (p["role"], f"{p['limit']:,}".replace(",", " "))
                                              for p in sorted(items, key=lambda x: x["role"])))
        lines.append("")
    lines.append("Площадок запрещено: %d%s" % (blocked, "" if blocked else " — новых не нашлось"))
    for site, why in list(sites.items())[:8]:
        lines.append("  %s — %s" % (site, why))
    if notes:
        lines.append("")
        lines.append("Заметки:")
        lines.extend("  " + n for n in notes[:10])
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="записать правки и отправить в TG")
    ap.add_argument("--dry-run", action="store_true", help="только показать")
    ap.add_argument("--date", default="", help="дата ГГГГ-ММ-ДД (пусто — сегодня по МСК)")
    args = ap.parse_args(argv)
    dry = not args.apply

    tok = direct.token()
    if not tok:
        print("нет токена в %s" % direct.TOKEN_ENV)
        return 1
    day = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(MSK).date()

    live = _call5("campaigns", "get", {"SelectionCriteria": {"States": ["ON"]},
                                       "FieldNames": ["Id"]}, tok).get("Campaigns", [])
    LIVE.update(c["Id"] for c in live)

    facts = collect(tok, day)
    qual = quality(facts)
    plan = budget_plan(tok, day, facts, qual)
    ids = sorted({p["campaign_id"] for p in plan})
    sites = bad_placements(tok, day, ids) if ids else {}

    notes: List[str] = []
    blocked = 0
    if not dry:
        notes += apply_budgets(tok, plan)
        blocked, bn = apply_blocks(tok, sites, ids)
        notes += bn
    else:
        blocked = len(sites)

    text = message(day, plan, qual, blocked, sites, notes, dry)
    print(text)
    if not dry:
        from sync.agent import notify
        notify.send(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
