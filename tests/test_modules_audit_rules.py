# -*- coding: utf-8 -*-
"""Правила audit: пара «срабатывает / молчит» на каждое из 15, на маленьких снимках."""

from sync.modules.audit_rules import RULES, Snapshot
from sync.modules.defaults import settings_for
from sync.modules.types import fingerprint

LOGIN = "login1"


def _camp(cid=1, ctype="TEXT_CAMPAIGN", counters=(77,), tracking=None, negatives=("а",),
          sites=0, settings=None, search=None, goal=None, **extra):
    block = {"CounterIds": list(counters), "BiddingStrategy": {"Search": search or {
        "BiddingStrategyType": "HIGHEST_POSITION"}}}
    if goal:
        block["BiddingStrategy"]["Search"] = {
            "BiddingStrategyType": "WB_MAXIMUM_CONVERSION_RATE",
            "WbMaximumConversionRate": {"GoalId": goal}}
    if tracking is not None:
        block["TrackingParams"] = tracking
    if settings is not None:
        block["Settings"] = settings
    key = {"TEXT_CAMPAIGN": "TextCampaign", "UNIFIED_CAMPAIGN": "UnifiedCampaign"}.get(ctype)
    c = {"Id": cid, "Name": "К%d" % cid, "Type": ctype, "State": "ON",
         "NegativeKeywords": {"Items": list(negatives)} if negatives else None,
         "ExcludedSites": {"Items": ["s%d.ru" % i for i in range(sites)]} if sites else None}
    if key:
        c[key] = block
    c.update(extra)
    return c


def _group(gid=10, cid=1, gtype="TEXT_AD_GROUP"):
    return {"Id": gid, "Name": "Г%d" % gid, "CampaignId": cid, "Type": gtype}


def _ad(aid=100, gid=10, cid=1, status="ACCEPTED", title="Диплом онлайн без вступительных экзаменов",
        text="Обучение", href="https://x.ru/?utm_source=y&utm_campaign=z", sitelink=5):
    ta = {"Title": title, "Text": text, "Href": href}
    if sitelink:
        ta["SitelinkSetId"] = sitelink
    return {"Id": aid, "AdGroupId": gid, "CampaignId": cid, "State": "ON", "Status": status,
            "Type": "TEXT_AD", "TextAd": ta}


def _kw(kid, gid=10, cid=1, word="купить диплом"):
    return {"Id": kid, "AdGroupId": gid, "CampaignId": cid, "Keyword": word, "State": "ON"}


def _snap(campaigns=None, groups=(), ads=(), keywords=(), aud=None, imp=None):
    return Snapshot(login=LOGIN, campaigns=campaigns if campaigns is not None else [_camp()],
                    groups=list(groups), ads=list(ads), keywords=list(keywords),
                    audiences_by_group=aud or {}, impressions_3d=imp if imp is not None else {1: 5})


def _run(rule, snap, **settings):
    check = {r: c for r, _s, c in RULES}[rule]
    return check(snap, settings_for("audit", settings))


def test_registry_has_exactly_the_spec_rules():
    assert [(r, s) for r, s, _ in RULES] == [
        ("group_no_targeting", "critical"), ("ads_rejected", "critical"),
        ("no_metrika_counter", "critical"), ("banned_words", "critical"),
        ("on_no_impressions", "warning"), ("autotargeting_with_keywords", "warning"),
        ("no_negatives", "warning"), ("href_no_utm", "warning"),
        ("strategy_goal_not_allowed", "warning"), ("excluded_sites_near_limit", "warning"),
        ("negatives_near_limit", "warning"), ("no_sitelinks", "info"),
        ("title_underused", "info"), ("duplicate_keywords", "info"),
        ("area_of_interest_on", "info")]


def test_group_no_targeting():
    (f,) = _run("group_no_targeting", _snap(groups=[_group()]))
    assert f.object_id == "10" and f.severity == "critical" and f.action is None
    assert _run("group_no_targeting", _snap(groups=[_group()], keywords=[_kw(1)])) == []
    auto = _kw(2, word="---autotargeting")
    assert _run("group_no_targeting", _snap(groups=[_group()], keywords=[auto])) == []
    assert _run("group_no_targeting", _snap(groups=[_group()], aud={10: 1})) == []
    # динамическая группа таргетируется фильтрами, а не фразами
    assert _run("group_no_targeting", _snap(groups=[_group(gtype="DYNAMIC_TEXT_AD_GROUP")])) == []


def test_ads_rejected():
    (f,) = _run("ads_rejected", _snap(ads=[_ad(status="REJECTED")]))
    assert f.object_id == "100"
    assert _run("ads_rejected", _snap(ads=[_ad()])) == []


def test_no_metrika_counter():
    (f,) = _run("no_metrika_counter", _snap([_camp(counters=())]))
    assert f.object_type == "campaign"
    assert _run("no_metrika_counter", _snap()) == []
    # у медийной счётчик не запрашивается: молчим
    assert _run("no_metrika_counter", _snap([_camp(ctype="CPM_BANNER_CAMPAIGN")])) == []


def test_banned_words_case_insensitive():
    snap = _snap(ads=[_ad(title="Кредит на обучение")])
    (f,) = _run("banned_words", snap, banned_words=["кредит"])
    assert f.details["words"] == ["кредит"] and f.severity == "critical"
    assert _run("banned_words", snap) == []
    assert _run("banned_words", _snap(ads=[_ad()]), banned_words=["кредит"]) == []
    # слово в тексте, а не в заголовке
    assert len(_run("banned_words", _snap(ads=[_ad(text="РАССРОЧКА")]),
                    banned_words=["рассрочка"])) == 1


def test_on_no_impressions():
    (f,) = _run("on_no_impressions", _snap(imp={}))
    assert f.campaign_id == 1
    assert _run("on_no_impressions", _snap(imp={1: 3})) == []
    assert _run("on_no_impressions", _snap([_camp(Status="DRAFT")], imp={})) == []


def test_autotargeting_with_keywords():
    kws = [_kw(1), _kw(2, word="---autotargeting")]
    (f,) = _run("autotargeting_with_keywords", _snap(groups=[_group()], keywords=kws))
    assert f.object_id == "10"
    assert _run("autotargeting_with_keywords", _snap(groups=[_group()], keywords=[_kw(1)])) == []
    only_auto = [_kw(2, word="---autotargeting")]
    assert _run("autotargeting_with_keywords", _snap(groups=[_group()], keywords=only_auto)) == []


def test_no_negatives():
    (f,) = _run("no_negatives", _snap([_camp(negatives=())]))
    assert f.campaign_id == 1
    assert _run("no_negatives", _snap()) == []
    # сеть без поиска: минус-фразы здесь не нужны
    net = _camp(negatives=(), search={"BiddingStrategyType": "SERVING_OFF"})
    assert _run("no_negatives", _snap([net])) == []


def test_href_no_utm():
    bad = _ad(href="https://x.ru/")
    (f,) = _run("href_no_utm", _snap(ads=[bad]))
    assert f.details["href"] == "https://x.ru/"
    assert _run("href_no_utm", _snap(ads=[_ad()])) == []
    assert _run("href_no_utm", _snap(ads=[bad]), require_utm=False) == []
    # одна utm_source без utm_campaign — мало
    half = _ad(href="https://x.ru/?utm_source=y")
    assert len(_run("href_no_utm", _snap(ads=[half]))) == 1


def test_href_with_campaign_tracking_params_ok():
    snap = _snap([_camp(tracking="utm_source=yandex&utm_campaign={campaign_id}")],
                 ads=[_ad(href="https://x.ru/")])
    assert _run("href_no_utm", snap) == []


def test_href_silent_when_tracking_params_not_read():
    # у ЕПК TrackingParams не запрашивается: пустой ответ не значит «разметки нет»
    snap = _snap([_camp(ctype="UNIFIED_CAMPAIGN")], ads=[_ad(href="https://x.ru/")])
    assert _run("href_no_utm", snap) == []


def test_strategy_goal_rule_silent_without_list():
    snap = _snap([_camp(goal=999)])
    assert _run("strategy_goal_not_allowed", snap) == []
    (f,) = _run("strategy_goal_not_allowed", snap, allowed_goal_ids=[111])
    assert f.details["goal_ids"] == [999]
    assert _run("strategy_goal_not_allowed", snap, allowed_goal_ids=[999]) == []


def test_strategy_goal_rule_reads_unified_priority_goals():
    c = _camp(ctype="UNIFIED_CAMPAIGN")
    c["UnifiedCampaign"]["PriorityGoals"] = {"Items": [{"GoalId": 555, "Value": 1}]}
    assert len(_run("strategy_goal_not_allowed", _snap([c]), allowed_goal_ids=[111])) == 1


def test_excluded_sites_near_limit():
    (f,) = _run("excluded_sites_near_limit", _snap([_camp(sites=900)]))
    assert (f.details["count"], f.details["limit"]) == (900, 1000)
    assert _run("excluded_sites_near_limit", _snap([_camp(sites=899)])) == []


def test_cpm_banner_sites_limit_100():
    cpm = _camp(ctype="CPM_BANNER_CAMPAIGN", sites=90)
    (f,) = _run("excluded_sites_near_limit", _snap([cpm]))
    assert f.details["limit"] == 100
    assert _run("excluded_sites_near_limit",
                _snap([_camp(ctype="CPM_BANNER_CAMPAIGN", sites=89)])) == []


def test_negatives_near_limit():
    (f,) = _run("negatives_near_limit", _snap([_camp(negatives=["x" * 1000] * 18)]))
    assert f.details["chars"] == 18000
    assert _run("negatives_near_limit", _snap([_camp(negatives=["x" * 1000] * 17)])) == []


def test_no_sitelinks():
    (f,) = _run("no_sitelinks", _snap(ads=[_ad(sitelink=0)]))
    assert f.severity == "info"
    assert _run("no_sitelinks", _snap(ads=[_ad()])) == []


def test_title_underused():
    short = _ad(title="Диплом")
    (f,) = _run("title_underused", _snap(ads=[short]))
    assert f.details["length"] == 6
    assert _run("title_underused", _snap(ads=[_ad()])) == []
    # порог настраивается: 6 из 56 проходят при title_fill_min=0.1
    assert _run("title_underused", _snap(ads=[short]), title_fill_min=0.1) == []


def test_duplicate_keywords():
    kws = [_kw(1, gid=10), _kw(2, gid=11, word="Купить  ДИПЛОМ")]
    (f,) = _run("duplicate_keywords", _snap(groups=[_group(10), _group(11)], keywords=kws))
    assert f.object_id == "купить диплом" and f.details["group_ids"] == [10, 11]
    assert _run("duplicate_keywords", _snap(keywords=[_kw(1), _kw(2, word="другое")])) == []
    # одна и та же фраза внутри одной группы дублем между группами не считается
    assert _run("duplicate_keywords", _snap(keywords=[_kw(1), _kw(2)])) == []


def test_area_of_interest_on():
    on = [{"Option": "ENABLE_AREA_OF_INTEREST_TARGETING", "Value": "YES"}]
    off = [{"Option": "ENABLE_AREA_OF_INTEREST_TARGETING", "Value": "NO"}]
    assert len(_run("area_of_interest_on", _snap([_camp(settings=on)]))) == 1
    assert _run("area_of_interest_on", _snap([_camp(settings=off)])) == []
    assert _run("area_of_interest_on", _snap()) == []


def test_findings_have_fix_url_and_no_action():
    snap = _snap([_camp(counters=(), negatives=())], groups=[_group()],
                 ads=[_ad(status="REJECTED", sitelink=0, title="Д", href="https://x.ru/")],
                 imp={})
    allf = [f for _r, _s, chk in RULES for f in chk(snap, settings_for("audit", {}))]
    assert {f.rule for f in allf} >= {"group_no_targeting", "ads_rejected",
                                       "no_metrika_counter", "on_no_impressions"}
    for f in allf:
        assert f.action is None and f.login == LOGIN
        assert f.details["fix"] and f.details["url"].startswith(
            "https://direct.yandex.ru/dna/grid/campaigns?ulogin=login1&campaigns-ids=1")
    ad_f = next(f for f in allf if f.rule == "ads_rejected")
    assert ad_f.details["url"].endswith("&groups-ids=10&banners-ids=100")


def test_object_id_stable_across_text_changes():
    a = _run("banned_words", _snap(ads=[_ad(title="Кредит")]), banned_words=["кредит"])[0]
    b = _run("banned_words", _snap(ads=[_ad(title="Ещё кредит тут")]), banned_words=["кредит"])[0]
    assert fingerprint("audit", a) == fingerprint("audit", b)
