# -*- coding: utf-8 -*-
"""Модуль audit: аудитор кабинета. Только чтение из Директа и отчёт, записи нет.

Снимок кабинета собирается пачками по кампаниям ON, правила (audit_rules) судят его
без обращений в сеть. У модуля нет apply/rollback: раннер применяет только то, у чего
они есть, а режимы approve/auto для audit закрыты в defaults.ALLOWED_MODES.
"""

from collections import Counter
from typing import Any, Dict, List, Tuple

from sync.modules import direct_api
from sync.modules.accounts import DirectAccount
from sync.modules.audit_rules import RULES, Snapshot
from sync.modules.queries import window
from sync.modules.types import Context, Finding, RunOutput

KEY = "audit"
# Отчёт показов — за три полных дня по вчера: сегодняшний отчёт Директа зависает.
IMPRESSION_DAYS = 3

CAMPAIGN_FIELDS = ["Id", "Name", "Type", "State", "Status", "NegativeKeywords", "ExcludedSites"]
# TrackingParams есть в формах запроса только у текстовых кампаний (build_bjorn_*,
# probe_bjorn_clone_shape2); для ЕПК поле не запрашиваем, правило ссылок её пропускает.
TEXT_BLOCK = ["CounterIds", "TrackingParams", "Settings", "BiddingStrategy", "PriorityGoals"]
UNIFIED_BLOCK = ["CounterIds", "Settings", "BiddingStrategy", "PriorityGoals"]
GROUP_FIELDS = ["Id", "Name", "CampaignId", "Status", "Type"]
# Тексты объявлений — только в TextAdFieldNames (ads.get отвергает "TextAd" в FieldNames,
# agent/segments.py). Комбинаторные объявления (ResponsiveAd) живут лишь в API v501, а
# direct_api ходит в v5, поэтому их аудит не читает.
AD_FIELDS = ["Id", "CampaignId", "AdGroupId", "State", "Status", "Type"]
TEXT_AD_FIELDS = ["Title", "Title2", "Text", "Href", "SitelinkSetId"]
KEYWORD_FIELDS = ["Id", "CampaignId", "AdGroupId", "Keyword", "State", "Status"]
AUDIENCE_FIELDS = ["Id", "AdGroupId", "CampaignId", "State"]


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


def fetch_snapshot(acc: DirectAccount, win: Tuple[str, str]) -> Snapshot:
    campaigns = [c for c in direct_api.get_all(
        acc.token, acc.login, "campaigns", {"States": ["ON"]}, CAMPAIGN_FIELDS,
        extra={"TextCampaignFieldNames": TEXT_BLOCK,
               "UnifiedCampaignFieldNames": UNIFIED_BLOCK})
        if c.get("State") == "ON"]
    snap = Snapshot(login=acc.login, campaigns=campaigns, groups=[], ads=[], keywords=[],
                    audiences_by_group={}, impressions_3d={})
    if not campaigns:
        return snap
    ids = [int(c["Id"]) for c in campaigns]

    def chunked(service, criteria, fields, **kw):
        return direct_api.get_all(acc.token, acc.login, service, criteria, fields,
                                  chunk_key="CampaignIds", ids=ids, **kw)

    snap.groups = chunked("adgroups", {}, GROUP_FIELDS)
    snap.ads = chunked("ads", {"States": ["ON"]}, AD_FIELDS,
                       extra={"TextAdFieldNames": TEXT_AD_FIELDS})
    snap.keywords = chunked("keywords", {"States": ["ON"]}, KEYWORD_FIELDS)
    for t in chunked("audiencetargets", {}, AUDIENCE_FIELDS, result_key="AudienceTargets"):
        if t.get("State") == "ON":
            gid = int(t.get("AdGroupId") or 0)
            snap.audiences_by_group[gid] = snap.audiences_by_group.get(gid, 0) + 1
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
    for acc in ctx.accounts:
        try:
            found = judge(fetch_snapshot(acc, win), ctx.settings)
        except Exception as err:  # кабинет не должен ронять остальные
            errors.append("%s: %s" % (acc.login, err))
            ctx.log("[%s] ошибка: %s" % (acc.login, err))
            continue
        # Находки кабинета берём только целиком: недочитанный кабинет не попадает в
        # scanned_logins, и его открытые находки не закрываются как исправленные.
        findings += found
        scanned.append(acc.login)
    if errors and not scanned:
        raise RuntimeError("ни один логин не прочитан: " + "; ".join(errors))
    summary: Dict[str, Any] = {"window": list(win),
                               "by_rule": dict(Counter(f.rule for f in findings))}
    if errors:
        summary["errors"] = errors
    return RunOutput(findings=findings, summary=summary, scanned_logins=scanned)
