# -*- coding: utf-8 -*-
"""Модуль audit: аудитор кабинета. Только чтение из Директа и отчёт, записи нет.

Снимок кабинета собирается пачками по кампаниям ON, правила (audit_rules) судят его
без обращений в сеть. У модуля нет apply/rollback: раннер применяет только то, у чего
они есть, а режимы approve/auto для audit закрыты в defaults.ALLOWED_MODES.
"""

from collections import Counter
from typing import Any, Callable, Dict, List, Optional, Tuple

from sync.modules import cabinets, direct_api
from sync.modules.accounts import DirectAccount
from sync.modules.audit_rules import RULES, SHARED_SETS, START_DATE, UNIFIED_TRACKING, Snapshot
from sync.modules.queries import window
from sync.modules.types import Context, Finding, RunOutput

KEY = "audit"
# Отчёт показов — за три полных дня по вчера: сегодняшний отчёт Директа зависает.
IMPRESSION_DAYS = 3

CAMPAIGN_FIELDS = ["Id", "Name", "Type", "State", "Status", "NegativeKeywords", "ExcludedSites"]
# TrackingParams подтверждён кодом только у текстовых кампаний (build_bjorn_*,
# probe_bjorn_clone_shape2); у ЕПК и NegativeKeywordSharedSetIds — по документации API,
# поэтому запрашиваются с запасным путём (_campaigns): не принял API — читаем без поля.
TEXT_BLOCK = ["CounterIds", "TrackingParams", "Settings", "BiddingStrategy", "PriorityGoals"]
UNIFIED_BLOCK = ["CounterIds", "Settings", "BiddingStrategy", "PriorityGoals"]
UNIFIED_TRACKING_FIELD = "TrackingParams"
# TrackingParams у группы принимает adgroups.get (probe_bjorn_clone_shape2.py).
GROUP_FIELDS = ["Id", "Name", "CampaignId", "Status", "Type", "TrackingParams"]
# Состояния объявления без архивного: отклонённое в архиве исправлять незачем.
LIVE_AD_STATES = ["ON", "OFF", "SUSPENDED", "OFF_BY_MONITORING"]
V501_AD_FIELDS = ["Id", "CampaignId", "AdGroupId", "State", "Type"]
# Тексты объявлений — только в TextAdFieldNames (ads.get отвергает "TextAd" в FieldNames,
# agent/segments.py). Комбинаторные объявления (ResponsiveAd) живут лишь в API v501, а
# direct_api ходит в v5, поэтому их аудит не читает.
AD_FIELDS = ["Id", "CampaignId", "AdGroupId", "State", "Status", "Type"]
TEXT_AD_FIELDS = ["Title", "Title2", "Text", "Href", "SitelinkSetId"]
KEYWORD_FIELDS = ["Id", "CampaignId", "AdGroupId", "Keyword", "State", "Status"]
AUDIENCE_FIELDS = ["Id", "AdGroupId", "CampaignId", "State"]
# Подшаги снимка кабинета по порядку fetch_snapshot — ход «Аудит · кабинет 2 из 5 · объявления»:
# у проекта с одним-двумя кабинетами без них полоса стояла бы на нуле весь прогон.
PARTS = ("кампании", "группы", "объявления", "отклонённые", "объявления ЕПК", "фразы",
         "аудитории", "показы")


def _report(acc: DirectAccount, win: Tuple[str, str], ids: List[int]) -> List[Dict[str, str]]:
    params = {
        "SelectionCriteria": {
            "DateFrom": win[0], "DateTo": win[1],
            "Filter": [{"Field": "CampaignId", "Operator": "IN",
                        "Values": [str(i) for i in sorted(ids)]}],
        },
        "FieldNames": ["CampaignId", "Impressions"],
        "ReportType": "CAMPAIGN_PERFORMANCE_REPORT",
        "DateRangeType": "CUSTOM_DATE",
        "Format": "TSV",
        "IncludeVAT": "YES",
        "IncludeDiscount": "NO",
    }
    return direct_api.report_rows(acc.token, acc.login, params, prefix="module-audit")


def _campaigns(acc: DirectAccount, unavailable: Dict[str, str]) -> List[dict]:
    """Кампании ON. Необязательные поля, которых API не принял, отбрасываются по одному."""
    optional = [SHARED_SETS, UNIFIED_TRACKING, START_DATE]
    while True:
        fields = CAMPAIGN_FIELDS + [f for f in (SHARED_SETS, START_DATE) if f in optional]
        unified = UNIFIED_BLOCK + ([UNIFIED_TRACKING_FIELD] if UNIFIED_TRACKING in optional
                                   else [])
        try:
            rows = direct_api.get_all(
                acc.token, acc.login, "campaigns", cabinets.with_ids(acc, {"States": ["ON"]}),
                fields,
                extra={"TextCampaignFieldNames": TEXT_BLOCK,
                       "UnifiedCampaignFieldNames": unified})
            return [c for c in rows if c.get("State") == "ON"]
        except Exception as err:
            if not optional or not direct_api.is_unknown_field_error(err):
                raise
            text = str(err)
            # Директ обычно называет поле в тексте ошибки; иначе снимаем по очереди.
            named = [f for f in optional
                     if (UNIFIED_TRACKING_FIELD if f == UNIFIED_TRACKING else f) in text]
            culprit = named[0] if named else optional[0]
            optional.remove(culprit)
            unavailable[culprit] = text[:300]


def _responsive_ads(acc: DirectAccount, ids: List[int], unavailable: Dict[str, str]
                    ) -> Optional[List[dict]]:
    """Комбинаторные объявления ЕПК (только v501). None — v501 не ответил."""
    try:
        rows = direct_api.get_all(
            acc.token, acc.login, "ads", {"States": ["ON"]}, V501_AD_FIELDS,
            extra={"ResponsiveAdFieldNames": ["Titles", "Texts"]},
            chunk_key="CampaignIds", ids=ids, version="v501")
    except Exception as err:  # v501 — доп. источник: его сбой не роняет кабинет
        unavailable["ResponsiveAds"] = str(err)[:300]
        return None
    return [a for a in rows if a.get("Type") == "RESPONSIVE_AD"]


def fetch_snapshot(acc: DirectAccount, win: Tuple[str, str],
                   step: Callable[[str], None] = lambda _part: None) -> Snapshot:
    """step(подшаг из PARTS) — перед каждым запросом в Директ."""
    unavailable: Dict[str, str] = {}
    step(PARTS[0])
    campaigns = _campaigns(acc, unavailable)
    snap = Snapshot(login=acc.login, campaigns=campaigns, groups=[], ads=[], keywords=[],
                    audiences_by_group={}, impressions_3d={}, unavailable=unavailable,
                    impressions_from=win[0])
    if not campaigns:
        return snap
    ids = [int(c["Id"]) for c in campaigns]

    def chunked(service, criteria, fields, **kw):
        return direct_api.get_all(acc.token, acc.login, service, criteria, fields,
                                  chunk_key="CampaignIds", ids=ids, **kw)

    step(PARTS[1])
    snap.groups = chunked("adgroups", {}, GROUP_FIELDS)
    step(PARTS[2])
    snap.ads = chunked("ads", {"States": ["ON"]}, AD_FIELDS,
                       extra={"TextAdFieldNames": TEXT_AD_FIELDS})
    # Отклонённые — отдельным запросом по Statuses: у отклонённого объявления State бывает
    # и OFF, и SUSPENDED, фильтр ON их терял бы. Архивные — не находка, их исключаем.
    step(PARTS[3])
    try:
        snap.rejected_ads = chunked(
            "ads", {"Statuses": ["REJECTED"], "States": LIVE_AD_STATES}, AD_FIELDS,
            extra={"TextAdFieldNames": TEXT_AD_FIELDS})
    except Exception as err:  # доп. запрос: его сбой не роняет остальные правила
        snap.rejected_ads = None
        unavailable["RejectedAds"] = str(err)[:300]
    step(PARTS[4])
    snap.responsive_ads = _responsive_ads(acc, ids, unavailable)
    step(PARTS[5])
    snap.keywords = chunked("keywords", {"States": ["ON"]}, KEYWORD_FIELDS)
    step(PARTS[6])
    for t in chunked("audiencetargets", {}, AUDIENCE_FIELDS, result_key="AudienceTargets"):
        if t.get("State") == "ON":
            gid = int(t.get("AdGroupId") or 0)
            snap.audiences_by_group[gid] = snap.audiences_by_group.get(gid, 0) + 1
    step(PARTS[7])
    for rec in _report(acc, win, ids):
        try:
            cid, shown = int(rec.get("CampaignId") or 0), int(float(rec.get("Impressions") or 0))
        except ValueError:
            continue
        snap.impressions_3d[cid] = snap.impressions_3d.get(cid, 0) + shown
    return snap


def judge(snap: Snapshot, settings: dict) -> List[Finding]:
    out: List[Finding] = []
    for _rule, _severity, check in RULES:
        out += check(snap, settings)
    return out


def run(ctx: Context) -> RunOutput:
    win = window(ctx.now, IMPRESSION_DAYS)
    findings: List[Finding] = []
    scanned: List[str] = []
    errors: List[str] = []
    skipped: Dict[str, Dict[str, int]] = {}
    unavailable: Dict[str, Dict[str, str]] = {}
    unverified: List[Tuple[str, str]] = []
    skipped_campaigns: List[str] = []
    total = len(ctx.accounts)
    for i, acc in enumerate(ctx.accounts):
        def step(part: str, i=i, acc=acc) -> None:
            ctx.progress({"step": acc.login, "logins_done": i, "logins_total": total,
                          "part": part, "part_done": PARTS.index(part),
                          "part_total": len(PARTS)})
        try:
            snap = fetch_snapshot(acc, win, step)
            found = judge(snap, ctx.settings)
        except Exception as err:  # кабинет не должен ронять остальные
            errors.append("%s: %s" % (acc.login, err))
            ctx.log("[%s] ошибка: %s" % (acc.login, err))
            continue
        finally:
            ctx.progress({"step": acc.login, "logins_done": i + 1, "logins_total": total})
        # Находки кабинета берём только целиком: недочитанный кабинет не попадает в
        # scanned_logins, и его открытые находки не закрываются как исправленные.
        findings += found
        scanned.append(acc.login)
        skipped_campaigns += cabinets.unmatched_lines(acc, (c["Id"] for c in snap.campaigns))
        unverified += [(acc.login, rule) for rule in sorted(snap.unverified)]
        for rule, by in snap.skipped.items():
            slot = skipped.setdefault(rule, {})
            for reason, n in by.items():
                slot[reason] = slot.get(reason, 0) + n
        if snap.unavailable:
            unavailable[acc.login] = snap.unavailable
    if errors and not scanned:
        raise RuntimeError("ни один логин не прочитан: " + "; ".join(errors))
    summary: Dict[str, Any] = {"window": list(win),
                               "by_rule": dict(Counter(f.rule for f in findings)),
                               # что правила не проверили: молчание не равно «чисто»
                               "skipped": {r: {"count": sum(by.values()),
                                               "reason": "; ".join(sorted(by))}
                                           for r, by in sorted(skipped.items())}}
    if unavailable:
        summary["unavailable"] = unavailable
    if errors:
        summary["errors"] = errors
    return RunOutput(findings=findings, summary=summary, scanned_logins=scanned,
                     unverified=unverified, skipped_campaigns=skipped_campaigns)
