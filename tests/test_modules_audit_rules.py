# -*- coding: utf-8 -*-
"""Правила audit: пара «срабатывает / молчит» на каждое из 15, на маленьких снимках."""

from sync.modules.audit_rules import RULES, SHARED_SETS, UNIFIED_TRACKING, Snapshot
from sync.modules.defaults import settings_for
from sync.modules.types import fingerprint

LOGIN = "login1"


def _camp(cid=1, ctype="TEXT_CAMPAIGN", counters=(77,), tracking=None, negatives=("а",),
          sites=0, settings=None, search=None, goal=None, **extra):
    block = {"BiddingStrategy": {"Search": search or {
        "BiddingStrategyType": "HIGHEST_POSITION"}}}
    if goal:
        block["BiddingStrategy"]["Search"] = {
            "BiddingStrategyType": "WB_MAXIMUM_CONVERSION_RATE",
            "WbMaximumConversionRate": {"GoalId": goal}}
    if counters:
        block["CounterIds"] = {"Items": list(counters)}
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


def _snap(campaigns=None, groups=(), ads=(), keywords=(), aud=None, imp=None, rejected=(),
          responsive=(), unavailable=None, frm=""):
    return Snapshot(login=LOGIN, campaigns=campaigns if campaigns is not None else [_camp()],
                    groups=list(groups), ads=list(ads), keywords=list(keywords),
                    audiences_by_group=aud or {}, impressions_3d=imp if imp is not None else {1: 5},
                    rejected_ads=None if rejected is None else list(rejected),
                    responsive_ads=responsive,
                    unavailable=unavailable or {}, impressions_from=frm)


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
    bad = _ad(status="REJECTED")
    bad["State"] = "SUSPENDED"  # отклонённое может быть любого State: фильтра нет
    (f,) = _run("ads_rejected", _snap(rejected=[bad]))
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
    snap = _snap([_camp(ctype="UNIFIED_CAMPAIGN")], ads=[_ad(href="https://x.ru/")],
                 unavailable={UNIFIED_TRACKING: "4000"})
    assert _run("href_no_utm", snap) == []
    assert snap.skipped["href_no_utm"] == {"Директ не отдал метки ссылок ЕПК": 1}
    # поле принято и пусто — это «разметки нет», правило судит
    ok = _snap([_camp(ctype="UNIFIED_CAMPAIGN")], ads=[_ad(href="https://x.ru/")])
    assert len(_run("href_no_utm", ok)) == 1


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
                 ads=[_ad(sitelink=0, title="Д", href="https://x.ru/")],
                 rejected=[_ad(status="REJECTED")], imp={})
    allf = [f for _r, _s, chk in RULES for f in chk(snap, settings_for("audit", {}))]
    assert {f.rule for f in allf} >= {"group_no_targeting", "ads_rejected",
                                       "no_metrika_counter", "on_no_impressions"}
    for f in allf:
        assert f.action is None and f.login == LOGIN
        assert f.details["fix"] and f.details["url"].startswith(
            "https://direct.yandex.ru/dna/") and "ulogin=login1&campaigns-ids=1" in f.details["url"]
    ad_f = next(f for f in allf if f.rule == "ads_rejected")
    assert ad_f.details["url"] == (
        "https://direct.yandex.ru/dna/banners-edit?ulogin=login1&campaigns-ids=1&banners-ids=100")


def test_object_id_stable_across_text_changes():
    a = _run("banned_words", _snap(ads=[_ad(title="Кредит")]), banned_words=["кредит"])[0]
    b = _run("banned_words", _snap(ads=[_ad(title="Ещё кредит тут")]), banned_words=["кредит"])[0]
    assert fingerprint("audit", a) == fingerprint("audit", b)


def test_campaign_and_group_links():
    (c,) = _run("no_negatives", _snap([_camp(negatives=())]))
    assert c.details["url"] == (
        "https://direct.yandex.ru/dna/campaigns-edit?ulogin=login1&campaigns-ids=1")
    (g,) = _run("group_no_targeting", _snap(groups=[_group()]))
    assert g.details["url"] == (
        "https://direct.yandex.ru/dna/grid/campaigns?ulogin=login1&campaigns-ids=1&groups-ids=10")


def test_banned_words_prefix_yo_and_zanyat():
    def hits(title, word="займ"):
        return _run("banned_words", _snap(ads=[_ad(title=title)]), banned_words=[word])
    assert hits("Займы онлайн")
    assert hits("Кредит наличными", "кредит")
    assert hits("Всё ещё в армии", "все") and hits("Все ещё", "всё")  # ё = е
    for form in ("займёт 2 года", "займет", "займу", "займешь", "займем", "займете", "займут"):
        assert hits("Обучение " + form) == [], form
    # слово не с начала — не срабатывает
    assert hits("подзаймы") == []


def test_skipped_counts_silent_rules():
    snap = _snap([_camp(ctype="CPM_BANNER_CAMPAIGN"), _camp(2)], ads=[_ad(), _ad(101, cid=2)],
                 groups=[_group(10, 2), _group(11, 2, gtype="DYNAMIC_TEXT_AD_GROUP"),
                         dict(_group(12, 2), Status="DRAFT")])
    for _rule, _s, chk in RULES:
        chk(snap, settings_for("audit", {}))
    sk = snap.skipped
    assert sk["no_metrika_counter"] == {"у этого типа кампании счётчик не проверяется": 1}
    assert sk["banned_words"] == {"список запрещённых слов не заполнен в настройках": 2}
    assert sk["strategy_goal_not_allowed"] == {
        "список допустимых целей не заполнен в настройках": 2}
    assert sk["group_no_targeting"] == {"группа не текстовая (таргет не по фразам)": 1,
                                        "группа в черновике": 1}
    assert sk["area_of_interest_on"] == {"у этого типа кампании настройки не проверяются": 1}
    assert sk["no_negatives"] == {"не поисковая кампания или тип без минус-фраз": 1}


def test_href_rule_off_is_reported():
    snap = _snap(ads=[_ad()])
    _run("href_no_utm", snap, require_utm=False)
    assert snap.skipped["href_no_utm"] == {"проверка меток выключена в настройках": 1}


def test_on_no_impressions_skips_campaign_started_in_window():
    snap = _snap([_camp(StartDate="2026-10-17")], imp={}, frm="2026-10-16")
    assert _run("on_no_impressions", snap) == []
    assert snap.skipped["on_no_impressions"] == {
        "кампания стартовала после начала окна показов": 1}
    old = _camp(StartDate="2026-09-01")
    assert len(_run("on_no_impressions", _snap([old], imp={}, frm="2026-10-16"))) == 1


def test_shared_negative_set_counts_as_negatives():
    c = _camp(negatives=())
    c[SHARED_SETS] = {"Items": [5]}
    assert _run("no_negatives", _snap([c])) == []


def test_no_negatives_skipped_when_shared_sets_field_rejected():
    snap = _snap([_camp(negatives=())], unavailable={SHARED_SETS: "8000"})
    assert _run("no_negatives", snap) == []
    assert snap.skipped["no_negatives"] == {"Директ не отдал общие наборы минус-фраз": 1}


def test_group_tracking_params_close_href_rule():
    g = dict(_group(), TrackingParams="utm_source=y&utm_campaign=z")
    assert _run("href_no_utm", _snap(groups=[g], ads=[_ad(href="https://x.ru/")])) == []


def test_group_draft_not_flagged():
    g = dict(_group(), Status="DRAFT")
    assert _run("group_no_targeting", _snap(groups=[g])) == []


def _resp(aid=200, titles=("Короткий",), texts=("Текст",)):
    return {"Id": aid, "AdGroupId": 10, "CampaignId": 1, "State": "ON",
            "Type": "RESPONSIVE_AD",
            "ResponsiveAd": {"Titles": [{"Title": t, "Status": "ACCEPTED"} for t in titles],
                             "Texts": [{"Text": t, "Status": "ACCEPTED"} for t in texts]}}


def test_responsive_ads_banned_words_and_title_longest():
    r = _resp(titles=("Ок", "Займы без отказа тут"), texts=("Текст",))
    (f,) = _run("banned_words", _snap(responsive=[r]), banned_words=["займ"])
    assert f.object_id == "200" and f.details["fields"] == {"займ": ["Title"]}
    # самый длинный заголовок 20 символов < 33.6 — недоиспользован
    (u,) = _run("title_underused", _snap(responsive=[r]))
    assert u.details["length"] == 20
    assert _run("title_underused", _snap(responsive=[_resp(titles=("Ок", "Д" * 40))])) == []


def test_responsive_unread_reported_when_v501_failed():
    ep = {"Id": 300, "AdGroupId": 10, "CampaignId": 1, "State": "ON", "Type": "RESPONSIVE_AD"}
    snap = _snap(ads=[ep, _ad()], responsive=None)
    _run("banned_words", snap, banned_words=["кредит"])
    _run("title_underused", snap)
    reason = "объявления ЕПК не прочитаны — Директ не отдал их полностью"
    assert snap.skipped["banned_words"] == {reason: 1}
    assert snap.skipped["title_underused"] == {reason: 1}


def test_rejected_archived_ad_is_not_a_finding():
    live = _ad(status="REJECTED")
    old = dict(_ad(101, status="REJECTED"), State="ARCHIVED")
    out = _run("ads_rejected", _snap(rejected=[live, old]))
    assert [f.object_id for f in out] == ["100"]


def test_rejected_request_failed_reported_in_skipped():
    snap = _snap(rejected=None)
    assert _run("ads_rejected", snap) == []
    assert snap.skipped["ads_rejected"] == {"Директ не отдал отклонённые объявления": 1}


def test_same_ad_from_v5_and_v501_judged_once():
    v5 = _ad(200, title="Займы онлайн")  # v5 отдал комбинаторное как TextAd
    r = _resp(200, titles=("Займы", "Займы онлайн"), texts=("Текст",))
    snap = _snap(ads=[v5], responsive=[r])
    assert len(_run("banned_words", snap, banned_words=["займ"])) == 1
    assert len(_run("title_underused", snap)) == 1
    # предпочтена версия v501: самый длинный заголовок 12, а не TextAd
    assert _run("title_underused", snap)[0].details["length"] == 12


def test_start_date_unavailable_skips_rule():
    snap = _snap(imp={}, unavailable={"StartDate": "4000"})
    assert _run("on_no_impressions", snap) == []
    assert snap.skipped["on_no_impressions"] == {"Директ не отдал даты старта кампаний": 1}


def _open_ad(rule, obj, cid=1):
    from datetime import datetime, timezone

    from sync.modules.types import Finding, StoredFinding
    f = Finding(rule=rule, severity="info", title="t", login=LOGIN, campaign_id=cid,
                campaign_name="К%d" % cid, object_type="ad", object_id=obj, details={})
    return StoredFinding(**f.__dict__, id=1, status="open", fingerprint=fingerprint("audit", f),
                         last_seen_at=datetime(2026, 10, 17, tzinfo=timezone.utc))


def test_epk_ads_not_judged_without_v501():
    # v5 отдаёт объявление ЕПК то как TextAd, то без него: без v501 вердикт по нему зависел бы
    # от версии API — находка то появлялась бы, то закрывалась
    from datetime import datetime, timezone

    from sync.modules.lifecycle import reconcile
    epk = _camp(cid=2, ctype="UNIFIED_CAMPAIGN")
    ad = _ad(aid=200, cid=2, sitelink=0, href="https://x.ru/")
    now = datetime(2026, 10, 18, tzinfo=timezone.utc)
    for rule in ("no_sitelinks", "href_no_utm"):
        snap = _snap([_camp(), epk], ads=[ad], responsive=None)
        assert _run(rule, snap) == []
        assert rule in snap.unverified
        r = reconcile("audit", [_open_ad(rule, "200", cid=2)], [], now, [LOGIN],
                      unverified=[(LOGIN, x) for x in snap.unverified])
        assert r.resolve == [] and r.expire == []
        # v501 ответил — правило судит ЕПК как обычно
        snap = _snap([_camp(), epk], ads=[ad], responsive=[])
        assert [f.object_id for f in _run(rule, snap)] == ["200"]
        assert rule not in snap.unverified


def test_text_campaign_ads_judged_without_v501():
    # без ЕПК в кабинете недоступность v501 правило не держит
    snap = _snap(ads=[_ad(sitelink=0)], responsive=None)
    assert len(_run("no_sitelinks", snap)) == 1
    assert "no_sitelinks" not in snap.unverified
