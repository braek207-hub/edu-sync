# -*- coding: utf-8 -*-
"""
sync/agent/writer/placements.py — Э3.7 (запись): запрет площадок сети.

Рычаг-близнец минус-фраз (writer/negatives.py) и устроен так же по тем же
причинам: список запрещённых площадок в API заменяется ЦЕЛИКОМ, значит
действие несёт объединение прежних и новых; за такт добавляется горсть самых
дорогих, потому что отсечённый трафик не вернуть, а обратная связь приходит
через дни; лимиты кабинета считаются и здесь, и в рельсе — независимо.

Отличий два: у площадки нет слов (это домен, bundle id или имя внешней сети),
и лимиты другие — 1000 элементов по 255 символов (ref-v5/campaigns/update).
"""

import hashlib
import re
from typing import Any, Dict, List, Optional, Tuple

from sync.agent.objects import CANDIDATE_WINDOW_DAYS
from sync.agent.writer import exposure, expectation
from sync.agent.writer.negatives import cut_evidence

PLACEMENT_KIND = "placement.exclude"
PLACEMENT_SETTING_KIND = "excluded_site"

# Конверсии вырезаемого трафика — отдельной строкой, ровно по тем же доводам,
# что у минус-фраз (negatives.NEGATIVE_CONVERSIONS_KIND): в таблице лежат
# строки старого формата без конверсий, и отсутствие строки обязано читаться
# как «не измеряли», а не как «ноль». Ноль даёт класс 0 — право резать без
# риск-бюджета, — и выдавать его за неизмеренное нельзя.
PLACEMENT_CONVERSIONS_KIND = "excluded_site_conversions"

# Ограничения Директа: не больше 1000 запрещённых площадок на кампанию,
# каждая — до 255 символов.
MAX_EXCLUDED_SITES = 1000
MAX_SITE_CHARS = 255

# Сколько площадок добавляется за такт — как у минус-фраз, по той же причине:
# такт обязан оставаться различимым в наблюдении.
MAX_SITES_PER_TICK = 10

NOT_TEXT_REASON = "запрет площадок доступен не для этого типа кампании"

_SPACES = re.compile(r"\s+")


def normalize_site(site: str) -> str:
    """Площадка в каноническом виде: нижний регистр, без пробелов по краям."""
    return str(site or "").strip().lower()


def site_is_valid(site: str) -> Tuple[bool, str]:
    """Проходит ли имя площадки ограничения Директа и здравый смысл."""
    normalized = normalize_site(site)
    if not normalized:
        return False, "пустое имя площадки"
    if len(normalized) > MAX_SITE_CHARS:
        return False, (f"имя длиннее {MAX_SITE_CHARS} символов: "
                       f"{normalized[:40]}…")
    if _SPACES.search(normalized):
        # Домен, bundle id и имя внешней сети пробелов не содержат: пробел
        # означает, что в строку попало что-то другое.
        return False, f"в имени площадки есть пробел: {normalized[:40]!r}"
    return True, ""


def plan_placements(
    candidates: List[Dict[str, Any]],
    max_per_tick: int = MAX_SITES_PER_TICK,
) -> Dict[str, Any]:
    """Кандидаты расчёта → площадки к запрету по кампаниям.

    Отбор по расходу, как у фраз: за такт уходят самые дорогие, остальные
    ждут следующего, и счётчик over_cap об этом говорит.
    """
    valid: List[Dict[str, Any]] = []
    invalid: List[Dict[str, Any]] = []
    for candidate in candidates:
        site = normalize_site(candidate.get("placement"))
        ok, reason = site_is_valid(site)
        if not ok:
            invalid.append({"placement": candidate.get("placement"),
                            "reason": reason})
            continue
        valid.append({**candidate, "site": site})

    valid.sort(key=lambda c: -float(c.get("cost") or 0.0))
    taken = valid[:max_per_tick]

    desired: Dict[str, List[str]] = {}
    # Вырезаемый расход по кампаниям — вход цены риска, как у минус-фраз.
    cut_cost: Dict[str, float] = {}
    # Конверсии вырезаемого трафика — вход ожидания, тем же правилом и по той
    # же причине, что у минус-фраз (writer/negatives.plan_negatives).
    cut_conversions: Dict[str, float] = {}
    # Кампании, чьи конверсии на вырезаемом трафике не измерены (строка
    # витрины старого формата). Ноль вместо них дал бы класс 0 по данным,
    # которых нет, — см. negatives.plan_negatives.
    unknown: set = set()
    for candidate in taken:
        split = candidate.get("cost_by_campaign") or {}
        conversions_split = candidate.get("conversions_by_campaign") or {}
        measured = candidate.get("conversions") is not None
        # Запасной путь, когда конверсии есть общим числом, но не разложены по
        # кампаниям, — раскладка по деньгам; см. negatives.plan_negatives.
        cost_total = sum(float(v) for v in split.values()) or float(
            candidate.get("cost") or 0.0)
        for campaign_id in candidate.get("campaigns") or []:
            if not campaign_id:
                continue
            sites = desired.setdefault(str(campaign_id), [])
            if candidate["site"] not in sites:
                sites.append(candidate["site"])
            campaign_cost = float(split.get(str(campaign_id), 0.0))
            cut_cost[str(campaign_id)] = cut_cost.get(str(campaign_id), 0.0) + campaign_cost
            if not measured:
                unknown.add(str(campaign_id))
                continue
            if conversions_split:
                lost = float(conversions_split.get(str(campaign_id), 0.0))
            else:
                share = (campaign_cost / cost_total) if cost_total > 0 else 0.0
                lost = float(candidate.get("conversions") or 0.0) * share
            cut_conversions[str(campaign_id)] = (
                cut_conversions.get(str(campaign_id), 0.0) + lost)
    for sites in desired.values():
        sites.sort()

    return {
        "desired": desired,
        "cut_cost": {cid: round(v, 2) for cid, v in sorted(cut_cost.items())},
        "cut_conversions": {cid: round(v, 2)
                            for cid, v in sorted(cut_conversions.items())
                            if cid not in unknown},
        "unknown_conversions": sorted(unknown),
        "over_cap": len(valid) - len(taken),
        "invalid": invalid,
        "cost_covered": round(sum(float(c.get("cost") or 0.0) for c in taken), 2),
    }


def merge_sites(existing: List[str], added: List[str],
                max_sites: int = MAX_EXCLUDED_SITES) -> List[str]:
    """Объединение прежнего списка и новых площадок в пределах лимита.

    Прежние площадки неприкосновенны: их запрещал человек, и вытеснять их
    ради своих — не право рычага. Не поместившиеся ждут следующего такта.
    """
    merged = [normalize_site(s) for s in (existing or []) if normalize_site(s)]
    for site in added or []:
        normalized = normalize_site(site)
        if not normalized or normalized in merged:
            continue
        if len(merged) >= max_sites:
            break
        merged.append(normalized)
    return sorted(merged)


def _idempotency_key(campaign_id: str, sites: List[str]) -> str:
    raw = "placements:" + str(campaign_id) + ":" + "|".join(sorted(sites))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def diff_placements(
    desired: Dict[str, List[str]],
    actual_by_campaign: Dict[str, Dict[str, Any]],
    cut_cost: Optional[Dict[str, float]] = None,
    window_days: int = CANDIDATE_WINDOW_DAYS,
    cut_conversions: Optional[Dict[str, float]] = None,
    baseline_cpa: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Желаемые запреты × прочитанные списки кабинета → (действия, отказы).

    baseline_cpa — тот же порог, по которому кандидаты и отбирались; без него
    запрет площадки не может показать своё основание и приезжает в отбор
    ставкой вместо арифметики (см. negatives.diff_negatives).
    """
    actions: List[Dict[str, Any]] = []
    refused: List[Dict[str, Any]] = []

    for campaign_id in sorted(desired):
        state = actual_by_campaign.get(str(campaign_id))
        if state is None:
            continue
        if state.get("campaign_type") not in (None, "TEXT_CAMPAIGN"):
            refused.append({"campaign_id": campaign_id, "reason": NOT_TEXT_REASON})
            continue

        existing = [normalize_site(s) for s in (state.get("excluded_sites") or [])]
        merged = merge_sites(existing, desired[campaign_id])
        if merged == sorted(existing):
            continue

        cut_window = float((cut_cost or {}).get(str(campaign_id), 0.0))
        cut_daily = cut_window / max(1, int(window_days))
        # Потерянные лиды — по тому же окну, что и деньги: обещание и цена
        # риска обязаны стоять на одном окне. Отсутствие кампании в словаре —
        # «не измеряли», и в основание это едет как None.
        lost_window = (cut_conversions or {}).get(str(campaign_id))
        lost_leads_daily = float(lost_window or 0.0) / max(1, int(window_days))
        evidence = cut_evidence(cut_window, lost_window, baseline_cpa, window_days)
        actions.append(expectation.attach({
            "action_kind": PLACEMENT_KIND,
            **({"evidence": evidence} if evidence else {}),
            "object_level": "campaign",
            "object_id": str(campaign_id),
            "exposure": exposure.traffic_cut_exposure(
                cut_daily, f"запрет площадок ({len([s for s in merged if s not in existing])})"),
            "direct_type": "EXCLUDED_SITES",
            "key": "campaign",
            "payload": {
                "CampaignId": int(campaign_id),
                "ExcludedSites": {"Items": merged},
                "AddedSites": [s for s in merged if s not in existing],
            },
            "previous_state": {"ExcludedSites": {"Items": sorted(existing)}},
            "idempotency_key": _idempotency_key(str(campaign_id), merged),
        }, {"cut_conversions_per_day": lost_leads_daily}))
    return actions, refused


def computed_rows(candidates: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Кандидаты расчёта → строки computed по кампаниям (мост Э0 → Э1)."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for candidate in candidates:
        site = normalize_site(candidate.get("placement"))
        if not site:
            continue
        split = candidate.get("cost_by_campaign") or {}
        conversions_split = candidate.get("conversions_by_campaign") or {}
        for campaign_id in candidate.get("campaigns") or []:
            if not campaign_id:
                continue
            # Доля расхода этой кампании — см. тот же довод в negatives.
            rows = out.setdefault(str(campaign_id), [])
            rows.append({
                "setting_kind": PLACEMENT_SETTING_KIND,
                "setting_key": site,
                "value": float(split.get(str(campaign_id),
                                         candidate.get("cost") or 0.0)),
                "raw_value": int(candidate.get("clicks") or 0),
                "support_n": int(candidate.get("clicks") or 0),
                "reason": candidate.get("reason"),
            })
            # Ноль пишется явно: только тогда отсутствие строки означает
            # «не измеряли», а не «конверсий не было».
            rows.append({
                "setting_kind": PLACEMENT_CONVERSIONS_KIND,
                "setting_key": site,
                "value": float(conversions_split.get(
                    str(campaign_id), candidate.get("conversions") or 0.0)),
                "raw_value": float(candidate.get("conversions") or 0.0),
                "support_n": int(candidate.get("clicks") or 0),
                "reason": candidate.get("reason"),
            })
    return out


def candidates_from_computed(
    computed_by_campaign: Dict[str, List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Строки computed → кандидаты в виде, который ждёт plan_placements.

    conversions — None, если строк PLACEMENT_CONVERSIONS_KIND по площадке
    нет ни одной: не измеряли. См. negatives.candidates_from_computed.
    """
    by_site: Dict[str, Dict[str, Any]] = {}
    for campaign_id, rows in computed_by_campaign.items():
        for row in rows:
            kind = str(row.get("setting_kind"))
            if kind not in (PLACEMENT_SETTING_KIND, PLACEMENT_CONVERSIONS_KIND):
                continue
            site = normalize_site(row.get("setting_key"))
            if not site:
                continue
            slot = by_site.setdefault(site, {
                "placement": site, "cost": 0.0, "clicks": 0,
                "conversions": None, "campaigns": [],
                "cost_by_campaign": {},
                "conversions_by_campaign": {},
                "reason": row.get("reason"),
            })
            if kind == PLACEMENT_CONVERSIONS_KIND:
                conversions = float(row.get("value") or 0.0)
                slot["conversions"] = (slot["conversions"] or 0.0) + conversions
                slot["conversions_by_campaign"][str(campaign_id)] = (
                    slot["conversions_by_campaign"].get(str(campaign_id), 0.0)
                    + conversions)
                continue
            cost = float(row.get("value") or 0.0)
            slot["cost"] += cost
            slot["clicks"] += int(row.get("raw_value") or 0)
            # Расход площадки В ЭТОЙ кампании — вход цены риска: запрет
            # ставит под удар вырезаемый трафик, а не всю кампанию.
            slot["cost_by_campaign"][str(campaign_id)] = (
                slot["cost_by_campaign"].get(str(campaign_id), 0.0) + cost)
            if str(campaign_id) not in slot["campaigns"]:
                slot["campaigns"].append(str(campaign_id))
    return sorted(by_site.values(), key=lambda c: -c["cost"])


def fetch_excluded_sites(client, campaign_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Текущие запреты площадок — свежим чтением, как и всё состояние кабинета."""
    out: Dict[str, Dict[str, Any]] = {}
    ids = [int(c) for c in campaign_ids]
    if not ids:
        return out
    page = 1000
    for start in range(0, len(ids), page):
        chunk = ids[start:start + page]
        result = client.get("campaigns", {
            "SelectionCriteria": {"Ids": chunk},
            "FieldNames": ["Id", "Type", "ExcludedSites"],
        })
        for item in result.get("Campaigns") or []:
            out[str(item["Id"])] = {
                "excluded_sites": ((item.get("ExcludedSites") or {})
                                   .get("Items") or []),
                "campaign_type": item.get("Type"),
            }
    return out
