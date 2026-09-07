# -*- coding: utf-8 -*-
"""Чистильщик площадок РСЯ: классификатор и ворота такта."""

import json

from sync.placements.classify import classify, is_app, normalize
from sync.placements.direct import MAX_SITE_CHARS
from sync.placements.plan import merge_sites, plan_account


# --- классификатор -------------------------------------------------------

def test_bundle_id_by_last_segment():
    """Приложение опознаётся по хвосту, который не является доменной зоной."""
    for site in ("com.dom.home", "video.like", "ru.zen.android",
                 "pdf.office.doc.reader.editor", "bubbleshooter.orig",
                 "screw.puzzle.match3.brain.puzz", "limehd.ru.ctv"):
        assert is_app(site), site


def test_bundle_id_whose_tail_is_a_real_tld():
    """.app — настоящая зона, и по одному хвосту эти имена прошли бы как сайты.

    Ловит их признак обратной нотации: первый сегмент тоже зона.
    """
    assert is_app("ai.character.app")
    assert is_app("jp.ne.ibis.ibispaintx.app")
    assert classify("ai.character.app")[0] == "app"


def test_subdomain_whose_head_is_a_tld_is_still_a_site():
    """win.mail.ru — поддомен Mail.ru, а не приложение.

    Регрессия: первая версия правила считала обратной нотацией любой первый
    сегмент, совпавший с зоной IANA, и «.win» зарегистрирована. Репетиция на
    Russever поставила win.mail.ru в план запрета у пяти кампаний из восьми.
    """
    for site in ("win.mail.ru", "top.mail.ru", "news.mail.ru", "go.mail.ru"):
        assert not is_app(site), site
        assert classify(site)[0] == "site", site


def test_domains_are_not_apps():
    for site in ("mail.ru", "dzen.ru", "yandex.ru", "m.pogoda.yandex.ru",
                 "lady.mail.ru", "gismeteo.ru", "androidloading.com"):
        assert not is_app(site), site
        assert classify(site)[0] == "site", site


def test_allowlist_is_exact_not_substring():
    """vk.baraholka.russia — барахолка, а не ВКонтакте.

    Подстрока «vk» пропустила бы её; на живых данных EDU эта площадка
    открутила 27 тыс. ₽.
    """
    assert classify("com.vkontakte.android")[0] == "keep"
    assert classify("vk.baraholka.russia")[0] == "app"


def test_publisher_prefix_allowed():
    assert classify("ru.yandex.mail")[0] == "keep"
    assert classify("ru.yandex.mobile.weather")[0] == "keep"


def test_dsp_and_game_beat_app_shape():
    """dsp.yandex по структуре — bundle id, но резать его надо со своей причиной."""
    verdict, reason = classify("dsp.yandex")
    assert verdict == "dsp" and "обменник" in reason
    assert classify("game.yandex")[0] == "game"
    assert classify("com.game.minicraft.village")[0] == "game"


def test_junk_zone_and_content():
    assert classify("some-site.xyz")[0] == "junk"
    assert classify("kinogo.pro")[0] == "junk"
    assert classify("")[0] == "junk"


def test_normalize_strips_trailing_slash():
    """Справка Директа записывает приложения как «com.block.juggle/»."""
    assert normalize(" COM.Block.Juggle/ ") == "com.block.juggle"


# --- ворота и слияние ----------------------------------------------------

def _campaign(cid, type_="TEXT_CAMPAIGN", excluded=(), name="К"):
    return {"Id": int(cid), "Name": name, "Type": type_, "State": "ON",
            "ExcludedSites": {"Items": list(excluded)}}


def _row(cid, site, clicks, cost=1.0):
    return {"campaign_id": str(cid), "placement": site, "clicks": clicks,
            "cost": cost}


def test_merge_keeps_existing_first():
    """Прежние запреты ставил человек — вытеснять их робот не вправе."""
    merged = merge_sites(["human.ru", "old.app.one"], ["new.app.two"])
    assert merged[:2] == ["human.ru", "old.app.one"]
    assert "new.app.two" in merged


def test_merge_respects_limit():
    existing = ["site%d.ru" % i for i in range(1000)]
    merged = merge_sites(existing, ["com.new.app"])
    assert len(merged) == 1000
    assert "com.new.app" not in merged


def test_gate_takes_top_by_clicks_only():
    rows = [_row(1, "com.junk.%d" % i, 100 - i) for i in range(10)]
    plan = plan_account(rows, [_campaign(1)], top_n=3)
    added = [a["placement"] for a in plan["actions"][0]["added"]]
    assert added == ["com.junk.0", "com.junk.1", "com.junk.2"]


def test_gate_ignores_already_excluded():
    """Иначе ворота навсегда заняты теми, кого уже отрезали."""
    rows = [_row(1, "com.old.app", 500), _row(1, "com.fresh.app", 5)]
    plan = plan_account(rows, [_campaign(1, excluded=["com.old.app"])],
                        top_n=1)
    added = [a["placement"] for a in plan["actions"][0]["added"]]
    assert added == ["com.fresh.app"]


def test_sites_are_never_cut():
    rows = [_row(1, "mail.ru", 999), _row(1, "com.junk.app", 5)]
    plan = plan_account(rows, [_campaign(1)])
    added = [a["placement"] for a in plan["actions"][0]["added"]]
    assert added == ["com.junk.app"]


def test_non_text_campaign_refused():
    rows = [_row(1, "com.junk.app", 50)]
    plan = plan_account(rows, [_campaign(1, type_="CPM_BANNER_CAMPAIGN")])
    assert plan["actions"] == []
    assert "запрет площадок недоступен" in plan["refused"][0]["reason"]


def test_fill_ceiling_leaves_room_for_human():
    existing = ["site%d.ru" % i for i in range(899)]
    rows = [_row(1, "com.a.app", 9), _row(1, "com.b.app", 8)]
    plan = plan_account(rows, [_campaign(1, excluded=existing)],
                        fill_ceiling=900)
    assert len(plan["actions"][0]["added"]) == 1
    assert "отложено 1" in plan["refused"][0]["reason"]


def test_ceiling_reached_reports_exhaustion():
    existing = ["site%d.ru" % i for i in range(900)]
    plan = plan_account([_row(1, "com.a.app", 9)],
                        [_campaign(1, excluded=existing)], fill_ceiling=900)
    assert plan["actions"] == []
    assert "чистильщик исчерпан" in plan["refused"][0]["reason"]


def test_too_long_name_refused():
    long_site = "com." + "x" * MAX_SITE_CHARS
    plan = plan_account([_row(1, long_site, 9)], [_campaign(1)])
    assert plan["actions"] == []
    assert "длиннее" in plan["refused"][0]["reason"]


def test_summary_counts_whole_day_not_just_cuts():
    rows = [_row(1, "mail.ru", 10, 100.0), _row(1, "com.junk.app", 90, 5.0)]
    plan = plan_account(rows, [_campaign(1)])
    assert plan["summary"]["site"]["clicks"] == 10
    assert plan["summary"]["app"]["clicks"] == 90
    assert plan["day_sites"] == 2


def test_archived_campaign_refused():
    """Клики за сегодня есть, но архивную кампанию Директ обновлять не даст."""
    rows = [_row(1, "com.junk.app", 50)]
    plan = plan_account(rows, [dict(_campaign(1), State="ARCHIVED")])
    assert plan["actions"] == []
    assert "вне игры" in plan["refused"][0]["reason"]


def test_paused_campaign_is_left_alone():
    """Чистим только запущенные: остановленная сегодня уже не открутится."""
    rows = [_row(1, "com.junk.app", 50)]
    for state in ("OFF", "SUSPENDED"):
        plan = plan_account(rows, [dict(_campaign(1), State=state)])
        assert plan["actions"] == [], state
        assert "вне игры" in plan["refused"][0]["reason"]


def test_dsp_subdomain_with_hyphen():
    """Обменники подписываются составным поддоменом — по точкам он неделим.

    Регрессия: из шести dsp-площадок в топе Russever 07.09.2026 словарь узнавал
    одну, остальные шли как «обычный сайт».
    """
    for site in ("dsp-opera-exchange.yandex.ru", "dsp-ironsource.yandex.ru",
                 "dsp-yeahmobi.yandex.ru"):
        assert classify(site)[0] == "dsp", site
    assert classify("com.d_one_games.escape_from_school")[0] == "game"


# --- второй судья (модель) -----------------------------------------------

from sync.placements import llm  # noqa: E402


def _ask_junk(prompt):
    """Модель, которая всё считает мусором: проверяем не её, а щиты вокруг."""
    sites = [ln.strip() for ln in prompt.splitlines() if "." in ln
             and not ln.startswith("-") and " " not in ln.strip()]
    items = [{"site": s, "verdict": "junk", "why": "дорвей"} for s in sites]
    return json.dumps({"items": items}, ensure_ascii=False)


def test_llm_never_touches_protected():
    """Крупные порталы не режем вовсе — мнение модели этого не отменяет."""
    verdicts = llm.judge(["mail.ru", "news.mail.ru", "dzen.ru", "hlam-xxx.ru"],
                         ask=_ask_junk)
    assert "mail.ru" not in verdicts
    assert "news.mail.ru" not in verdicts
    assert llm.overrides_from({"mail.ru": ("junk", "х")}) == {}


def test_llm_overrides_only_cut():
    """«keep» ничего не меняет: словарь и так оставил площадку."""
    out = llm.overrides_from({"a.ru": ("junk", "пиратка"),
                              "b.ru": ("keep", "СМИ")})
    assert list(out) == ["a.ru"]
    assert out["a.ru"][0] == "llm"


def test_llm_broken_answer_is_silence():
    """Оборванный JSON — молчание, а не согласие: резать нечего."""
    assert llm.judge(["x.ru"], ask=lambda p: "не json") == {}
    assert llm.judge(["x.ru"], ask=lambda p: (_ for _ in ()).throw(IOError)) == {}


def test_llm_without_key_is_off():
    assert llm.judge(["x.ru"], ask=None) == {}


def test_plan_offers_only_site_candidates():
    """Модели показываем лишь то, что словарь оставил и что попало в ворота."""
    rows = [_row(1, "portal.ru", 90), _row(1, "com.junk.app", 80),
            _row(1, "tail.ru", 1)]
    plan = plan_account(rows, [_campaign(1)], top_n=2)
    assert plan["candidates"] == ["portal.ru"]


def test_override_turns_site_into_cut():
    rows = [_row(1, "doorway.ru", 90)]
    plan = plan_account(rows, [_campaign(1)],
                        overrides={"doorway.ru": ("llm", "модель: дорвей")})
    added = plan["actions"][0]["added"][0]
    assert added["placement"] == "doorway.ru" and added["verdict"] == "llm"
