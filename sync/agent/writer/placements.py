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
from typing import Any, Dict, List, Tuple

PLACEMENT_KIND = "placement.exclude"
PLACEMENT_SETTING_KIND = "excluded_site"

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
    for candidate in taken:
        for campaign_id in candidate.get("campaigns") or []:
            if not campaign_id:
                continue
            sites = desired.setdefault(str(campaign_id), [])
            if candidate["site"] not in sites:
                sites.append(candidate["site"])
    for sites in desired.values():
        sites.sort()

    return {
        "desired": desired,
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
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Желаемые запреты × прочитанные списки кабинета → (действия, отказы)."""
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

        actions.append({
            "action_kind": PLACEMENT_KIND,
            "object_level": "campaign",
            "object_id": str(campaign_id),
            "direct_type": "EXCLUDED_SITES",
            "key": "campaign",
            "payload": {
                "CampaignId": int(campaign_id),
                "ExcludedSites": {"Items": merged},
                "AddedSites": [s for s in merged if s not in existing],
            },
            "previous_state": {"ExcludedSites": {"Items": sorted(existing)}},
            "idempotency_key": _idempotency_key(str(campaign_id), merged),
        })
    return actions, refused


def computed_rows(candidates: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Кандидаты расчёта → строки computed по кампаниям (мост Э0 → Э1)."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for candidate in candidates:
        site = normalize_site(candidate.get("placement"))
        if not site:
            continue
        for campaign_id in candidate.get("campaigns") or []:
            if not campaign_id:
                continue
            out.setdefault(str(campaign_id), []).append({
                "setting_kind": PLACEMENT_SETTING_KIND,
                "setting_key": site,
                "value": float(candidate.get("cost") or 0.0),
                "raw_value": int(candidate.get("clicks") or 0),
                "support_n": int(candidate.get("clicks") or 0),
                "reason": candidate.get("reason"),
            })
    return out


def candidates_from_computed(
    computed_by_campaign: Dict[str, List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Строки computed → кандидаты в виде, который ждёт plan_placements."""
    by_site: Dict[str, Dict[str, Any]] = {}
    for campaign_id, rows in computed_by_campaign.items():
        for row in rows:
            if str(row.get("setting_kind")) != PLACEMENT_SETTING_KIND:
                continue
            site = normalize_site(row.get("setting_key"))
            if not site:
                continue
            slot = by_site.setdefault(site, {
                "placement": site, "cost": 0.0, "clicks": 0,
                "conversions": 0, "campaigns": [],
                "reason": row.get("reason"),
            })
            slot["cost"] += float(row.get("value") or 0.0)
            slot["clicks"] += int(row.get("raw_value") or 0)
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
