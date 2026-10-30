# -*- coding: utf-8 -*-
"""Ворота такта и план запретов: строки отчёта → что писать в каждую кампанию."""

from typing import Any, Callable, Dict, List, Optional

from sync.placements.classify import CUT_VERDICTS, classify, normalize
from sync.placements.direct import (CLEANABLE_STATES, CLEANABLE_TYPES,
                                    MAX_EXCLUDED_SITES, MAX_SITE_CHARS,
                                    sites_limit)

# Сколько верхних по кликам площадок кампании смотрит один такт. Ворота
# защищают лимит слотов: мусорный хвост исчисляется тысячами имён, и без
# ворот одна кампания выбрала бы 1000 слотов за пару дней. Площадка из
# хвоста ждёт, пока не станет заметна кликами.
TOP_N = 30

# Доля лимита, которую вправе занять робот. Остаток держим под ручные запреты
# директолога: список общий, и выбрать его целиком значит отнять у человека
# рычаг. Считается от лимита ТИПА кампании — у медийной он вдесятеро короче.
FILL_SHARE = 0.9
FILL_CEILING = int(MAX_EXCLUDED_SITES * FILL_SHARE)


def ceiling_for(limit: int, fill_ceiling: int = FILL_CEILING) -> int:
    """Сколько элементов списка запретов вправе занять робот при лимите типа кампании."""
    return fill_ceiling if limit >= MAX_EXCLUDED_SITES else int(limit * FILL_SHARE)


def site_is_valid(site: str):
    if not site:
        return False, "пустое имя площадки"
    if len(site) > MAX_SITE_CHARS:
        return False, "имя длиннее %d символов" % MAX_SITE_CHARS
    if " " in site:
        # Домен, bundle id и имя внешней сети пробелов не содержат.
        return False, "в имени есть пробел"
    return True, ""


def evictable(own, items, keep=()) -> List[str]:
    """Свои запреты кампании, которые ротация вправе снять, в порядке снятия.

    own — площадки, которые робот запрещал сам (его применённые находки), старейшие
    первыми. Снимается только элемент, стоящий в списке ровно в той форме, в какой робот
    его писал (нормализованной): вариант в другом регистре или с «/» ставил человек, —
    то же правило, что у отката. keep — что снимать нельзя: обязательный список кабинета,
    пришпиленное этим прогоном, площадки, которые сейчас же и пишутся.
    """
    exact = {str(x).strip() for x in (items or [])}
    hold = {normalize(x) for x in keep or []}
    out: List[str] = []
    for site in own or []:
        site = normalize(site)
        if site and site in exact and site not in hold and site not in out:
            out.append(site)
    return out


def free_slots(existing: int, ceiling: int, limit: int) -> int:
    """Сколько новых встаёт без ротации: до max(потолок, текущая длина), но не выше лимита.

    Список выше потолка заполнил человек: робот его не растит и не опускает чужими
    руками — меняет свои 1:1 (rotation_need).
    """
    return max(min(max(ceiling, existing), limit) - existing, 0)


def rotation_need(added: int, existing: int, ceiling: int, limit: int) -> int:
    """Сколько своих снять под added новых: не больше added, ровно столько, чтобы итог не
    превысил max(потолок, текущая длина) и лимит."""
    return min(max(added - free_slots(existing, ceiling, limit), 0), added)


def merge_sites(existing: List[str], added: List[str],
                max_sites: int = MAX_EXCLUDED_SITES) -> List[str]:
    """Прежний список плюс новые площадки, в пределах лимита.

    Прежние запреты неприкосновенны: их ставил человек, и вытеснять их ради
    своих — не право робота (свои старые снимает только ротация, plan_account(own=…)).
    Не поместившиеся ждут следующего такта.
    """
    merged = [normalize(s) for s in (existing or []) if normalize(s)]
    seen = set(merged)
    for site in added or []:
        site = normalize(site)
        if not site or site in seen:
            continue
        if len(merged) >= max_sites:
            break
        merged.append(site)
        seen.add(site)
    return merged


def plan_account(rows: List[Dict[str, Any]],
                 campaigns: List[Dict[str, Any]],
                 top_n: int = TOP_N,
                 fill_ceiling: int = FILL_CEILING,
                 allow_exact=None,
                 allow_prefix=None,
                 overrides: Optional[Dict[str, Any]] = None,
                 always_block=None,
                 spam: Optional[Dict[str, str]] = None,
                 cut_verdicts=CUT_VERDICTS,
                 guard: Optional[Callable[[str, str, str], Optional[dict]]] = None,
                 own: Optional[Dict[str, List[str]]] = None
                 ) -> Dict[str, Any]:
    """Строки отчёта + кампании кабинета → план запретов по кампаниям.

    Возвращает действия (что писать), отказы (почему не пишем) и сводку
    по вердиктам за день — она едет в журнал и в отчёт человеку.

    cut_verdicts — вердикты, которые режутся: выключенная категория словаря
    остаётся вердиктом, но не режется. guard(площадка, кампания, вердикт) —
    защита человека (правило protect, «Никогда не закрывать»): вернула отказ —
    площадка в этой кампании не пишется, отказ уходит в plan["guarded"].

    own — {кампания: свои запреты робота, старейшие первыми}. Когда под потолком нет
    места, план снимает старейшие из них (ротация, action["evict"]) ровно столько, сколько
    нужно новым. Без own — прежний отказ «список запретов заполнен».
    """
    kwargs = {}
    if allow_exact is not None:
        kwargs["allow_exact"] = allow_exact
    if allow_prefix is not None:
        kwargs["allow_prefix"] = allow_prefix

    by_id = {str(c["Id"]): c for c in campaigns}

    # Свод по паре кампания × площадка: ворота считаются внутри кампании,
    # потому что и лимит слотов, и сам запрет живут на кампании.
    pair: Dict[str, Dict[str, Dict[str, Any]]] = {}
    day: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        site = normalize(row.get("placement"))
        if not site:
            continue
        cid = str(row.get("campaign_id") or "")
        clicks = int(row.get("clicks") or 0)
        cost = float(row.get("cost") or 0.0)
        slot = pair.setdefault(cid, {}).setdefault(
            site, {"clicks": 0, "cost": 0.0})
        slot["clicks"] += clicks
        slot["cost"] += cost
        d = day.setdefault(site, {"clicks": 0, "cost": 0.0})
        d["clicks"] += clicks
        d["cost"] += cost

    # Пришпиленные площадки: их запрет не зависит ни от ворот, ни от
    # сегодняшних кликов. Обязательный список кабинета — решение человека,
    # спам-площадки — статистика за неделю (spam.py). Порядок важен: сначала
    # человек, потом правило.
    pinned: Dict[str, tuple] = {}
    for site in (normalize(x) for x in (always_block or [])):
        if site:
            pinned[site] = ("forced", "обязательная минусация кабинета")
    for site, why in (spam or {}).items():
        site = normalize(site)
        if site and site not in pinned:
            pinned[site] = ("spam", why)

    verdicts = {site: classify(site, **kwargs) for site in day}
    # Поправки второго судьи (llm.py) ложатся поверх словаря. Только «резать»:
    # согласие модели со словарём ничего не меняет и в план не едет.
    for site, verdict in (overrides or {}).items():
        if site in verdicts:
            verdicts[site] = verdict
    # Пришпиленное главнее обоих судей: площадка запрещается, даже если
    # словарь считает её крупным порталом.
    for site, verdict in pinned.items():
        if site in verdicts:
            verdicts[site] = verdict

    summary: Dict[str, Dict[str, Any]] = {}
    for site, (verdict, _) in verdicts.items():
        s = summary.setdefault(verdict, {"sites": 0, "clicks": 0, "cost": 0.0})
        s["sites"] += 1
        s["clicks"] += day[site]["clicks"]
        s["cost"] += round(day[site]["cost"], 2)

    actions: List[Dict[str, Any]] = []
    refused: List[Dict[str, Any]] = []
    guarded: List[dict] = []
    # Имена в окне ворот, которые словарь оставил обычными сайтами: их и
    # показываем модели. Спрашивать про весь день незачем — за воротами
    # площадка всё равно не будет запрещена этим тактом.
    candidates: Dict[str, int] = {}

    # Кампании из отчёта плюс — если есть пришпиленные площадки — все
    # пригодные кампании кабинета: такой запрет не ждёт, пока площадка
    # наберёт клики, он должен стоять в списке заранее.
    cids = set(pair)
    if pinned:
        cids |= {str(c["Id"]) for c in campaigns
                 if c.get("Type") in CLEANABLE_TYPES
                 and c.get("State") in CLEANABLE_STATES}

    for cid in sorted(cids):
        sites = pair.get(cid, {})
        campaign = by_id.get(cid)
        if campaign is None:
            # Кампания есть в статистике, но campaigns/get её не отдаёт даже
            # по прямому Id: Директ отвечает «3500 Тип кампании не
            # поддерживается» (замер на Russever 07.09.2026). Это Мастер
            # кампаний и прочие новые форматы — API v5 их не знает, читать и
            # писать нечем. Деньги показываем: у Russever на таких кампаниях
            # шла треть дневного расхода, и человек должен знать, что этот
            # кусок чистится только руками.
            refused.append({
                "campaign_id": cid,
                "clicks": sum(v["clicks"] for v in sites.values()),
                "cost": round(sum(v["cost"] for v in sites.values()), 2),
                "reason": "не видна API v5 (Мастер кампаний) — роботу не "
                          "достать; руками: Отчёт по площадкам → галочки → "
                          "«Запретить показы»"})
            continue
        if campaign.get("Type") not in CLEANABLE_TYPES:
            refused.append({"campaign_id": cid, "name": campaign.get("Name"),
                            "reason": "тип %s: запрет площадок недоступен"
                                      % campaign.get("Type")})
            continue
        if campaign.get("State") not in CLEANABLE_STATES:
            # Клики за сегодня у неё есть, но кампания уже вне игры: архив,
            # завершение по дате, конверсия в другой формат. Запись либо
            # отклоняется Директом, либо не даёт эффекта.
            refused.append({"campaign_id": cid, "name": campaign.get("Name"),
                            "reason": "состояние %s: кампания вне игры, "
                                      "запрет не пишем" % campaign.get("State")})
            continue

        items = (campaign.get("ExcludedSites") or {}).get("Items") or []
        existing = [normalize(s) for s in items]
        existing = [s for s in existing if s]
        known = set(existing)

        # Топ по кликам среди ещё НЕ запрещённых: иначе ворота навсегда
        # заняты теми, кого уже отрезали, и новый мусор в них не попадает.
        fresh = [(site, v["clicks"], v["cost"])
                 for site, v in sites.items() if site not in known]
        fresh.sort(key=lambda item: (-item[1], -item[2], item[0]))

        added: List[Dict[str, Any]] = []
        # Пришпиленные — первыми и вне ворот: они не соревнуются за место в
        # топе по кликам, потому что решение о них уже принято.
        for site, (verdict, why) in pinned.items():
            if site in known:
                continue
            held = guard(site, cid, verdict) if guard else None
            if held:
                guarded.append(held)
                # Отказ один: ниже словарь увидел бы ту же площадку с тем же вердиктом.
                known.add(site)
                continue
            ok, bad = site_is_valid(site)
            if not ok:
                refused.append({"campaign_id": cid, "placement": site,
                                "reason": bad})
                continue
            stat = sites.get(site) or {"clicks": 0, "cost": 0.0}
            added.append({"placement": site, "clicks": stat["clicks"],
                          "cost": round(stat["cost"], 2), "verdict": verdict,
                          "reason": why})
            known.add(site)

        for site, clicks, cost in fresh[:top_n]:
            if site in known:
                continue  # уже взят обязательным списком выше
            verdict, why = verdicts[site]
            if verdict not in cut_verdicts:
                if verdict == "site":
                    candidates[site] = max(candidates.get(site, 0), clicks)
                continue
            held = guard(site, cid, verdict) if guard else None
            if held:
                guarded.append(held)
                continue
            ok, bad = site_is_valid(site)
            if not ok:
                refused.append({"campaign_id": cid, "placement": site,
                                "reason": bad})
                continue
            added.append({"placement": site, "clicks": clicks,
                          "cost": round(cost, 2), "verdict": verdict,
                          "reason": why})

        if not added:
            continue

        limit = sites_limit(campaign.get("Type"))
        ceiling = ceiling_for(limit, fill_ceiling)
        spare = evictable((own or {}).get(cid), items,
                          list(pinned) + [a["placement"] for a in added])
        # Ёмкость: свободное под потолком плюс свои запреты, которые можно снять (1:1).
        room = free_slots(len(existing), ceiling, limit) + len(spare)
        if room <= 0:
            refused.append({
                "campaign_id": cid, "name": campaign.get("Name"),
                "reason": "список запретов заполнен (%d из %d), чистильщик "
                          "исчерпан" % (len(existing), limit)})
            continue
        if len(added) > room:
            refused.append({
                "campaign_id": cid, "name": campaign.get("Name"),
                "reason": "до потолка %d осталось %d слотов, отложено %d "
                          "площадок" % (ceiling, room, len(added) - room)})
            added = added[:room]

        evict = spare[:rotation_need(len(added), len(existing), ceiling, limit)]
        gone = set(evict)
        merged = merge_sites([x for x in items if str(x).strip() not in gone],
                             [a["placement"] for a in added], max_sites=limit)
        actions.append({
            "campaign_id": cid,
            "campaign_name": campaign.get("Name"),
            "state": campaign.get("State"),
            "existing_count": len(existing),
            "added": added,
            "evict": evict,
            "sites": merged,
            "fill_after": len(merged),
            "cut_clicks": sum(a["clicks"] for a in added),
            "cut_cost": round(sum(a["cost"] for a in added), 2),
        })

    return {
        "actions": actions,
        "refused": refused,
        "guarded": guarded,
        "candidates": [site for site, _ in
                       sorted(candidates.items(), key=lambda kv: -kv[1])],
        "summary": summary,
        "day_sites": len(day),
        "day_clicks": sum(v["clicks"] for v in day.values()),
        "day_cost": round(sum(v["cost"] for v in day.values()), 2),
    }
