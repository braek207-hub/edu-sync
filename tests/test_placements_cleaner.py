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


def test_avito_kept_on_both_platforms():
    for site in ("ru.avito.android", "com.avito.android", "ru.avito.app"):
        assert classify(site)[0] == "keep", site


def test_clients_json_gives_each_login_its_token():
    """BJORN: [{login, token}] — у каждого логина свой токен, пустой берёт BJORN_DIRECT_TOKEN."""
    from sync.modules.accounts import accounts_for

    raw = json.dumps([{"login": "a", "token": "ta"}, {"client_login": "b"}, "c", {"token": "t"}])
    found, _ = accounts_for("bjorn", {"BJORN_DIRECT_CLIENTS_JSON": raw,
                                      "BJORN_DIRECT_TOKEN": "common"})
    assert [(a.login, a.token) for a in found] == [("a", "ta"), ("b", "common"), ("c", "common")]

    found, why = accounts_for("bjorn", {"BJORN_DIRECT_CLIENTS_JSON": raw})
    assert [(a.login, a.token) for a in found] == [("a", "ta")]
    assert why


def test_bjorn_and_synergy_stay_in_cleaner_registry():
    """BJORN — проект модулей; Синергия без проекта Panda-BI — старый движок, свои кампании."""
    from sync.placements import accounts as reg

    assert reg.BY_KEY["bjorn"].slug == "bjorn"
    old = reg.legacy_accounts()
    assert [a.key for a in old] == ["synergy_school"]
    assert old[0].campaign_ids == (715539609, 715543179)
    assert old[0].token_env == "APPROVE_AGENCY_TOKEN"


def test_legacy_account_cleans_only_own_campaigns(monkeypatch):
    """Общий кабинет: campaigns.get сужен Ids, строки отчёта чужих кампаний отброшены."""
    from sync.placements import accounts as reg
    from sync.placements import direct, legacy

    acc = reg.Account("x", "X", None, token_env="X_TOKEN", login="l", campaign_ids=(1,))
    monkeypatch.setenv("X_TOKEN", "t")
    asked = {}

    def campaigns(token, login, ids=()):
        asked["ids"] = tuple(ids)
        return [_campaign(1)]

    rows = [_row(1, "com.junk.app", 50), _row(2, "com.other.app", 50)]
    monkeypatch.setattr(direct, "campaigns", campaigns)
    monkeypatch.setattr(direct, "placements_today", lambda token, login: rows)
    written = []
    monkeypatch.setattr(legacy, "set_excluded_sites",
                        lambda t, l, cid, sites: written.append((cid, list(sites))) or (True, ""))
    res = legacy.run_account(acc, apply=True, use_db=False, log=lambda s: None)
    assert asked["ids"] == (1,)
    assert written == [("1", ["com.junk.app"])]
    assert res["failed"] == 0


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


def test_unsupported_type_refused():
    rows = [_row(1, "com.junk.app", 50)]
    plan = plan_account(rows, [_campaign(1, type_="SMART_CAMPAIGN")])
    assert plan["actions"] == []
    assert "запрет площадок недоступен" in plan["refused"][0]["reason"]


def test_media_campaign_is_cleaned_with_its_own_limit():
    """Медийная поддерживает ExcludedSites, но её лимит вдесятеро короче."""
    rows = [_row(1, "com.junk.app", 50)]
    plan = plan_account(rows, [_campaign(1, type_="CPM_BANNER_CAMPAIGN")])
    assert plan["actions"][0]["added"][0]["placement"] == "com.junk.app"

    full = ["site%d.ru" % i for i in range(90)]
    plan = plan_account(rows, [_campaign(1, type_="CPM_BANNER_CAMPAIGN",
                                         excluded=full)])
    assert plan["actions"] == []
    assert "из 100" in plan["refused"][0]["reason"]


def test_junk_words_anywhere_in_name():
    """umkaplay.ru по точкам — один сегмент, токен «play» его не ловил."""
    for site in ("umkaplay.ru", "mozgoplay.com", "gamezone.net",
                 "freesoft.ru", "com.playrix.township"):
        verdict, why = classify(site)
        assert verdict in ("junk", "app"), site
        assert "слово" in why, site


def test_junk_words_do_not_override_allowlist():
    """ru.more.play — Okko, известное приложение остаётся."""
    assert classify("ru.more.play")[0] == "keep"


# --- спам-площадки -------------------------------------------------------

from sync.placements.spam import suspicious_sites


def _week(cid, site, clicks, conv, goal=1):
    return {"campaign_id": str(cid), "placement": site, "clicks": clicks,
            "cost": clicks * 30.0, "impressions": clicks * 100,
            "conversions": {goal: conv}}


def test_spam_site_by_conversion_rate():
    """Пример Павла: 12 достижений с 93 кликов при 3,4 % по кабинету."""
    rows = [_week(1, "umkaplay.ru", 93, 12)]
    rows += [_week(1, "site%d.ru" % i, 100, 2) for i in range(20)]
    goals = {"1": 1}
    hit = suspicious_sites(rows, goals)
    assert list(hit) == ["umkaplay.ru"]
    assert "12 достижений" in hit["umkaplay.ru"]


def test_spam_needs_volume_and_ratio():
    """Три заявки с двух кликов — шум; 8 % при 6 % по кабинету — норма."""
    rows = [_week(1, "tiny.ru", 2, 3)]
    rows += [_week(1, "site%d.ru" % i, 100, 6) for i in range(10)]
    rows += [_week(1, "good.ru", 100, 8)]
    assert suspicious_sites(rows, {"1": 1}) == {}


def test_spam_skips_known_apps_and_portals():
    """ВК даёт шесть заявок с 55 кликов — решать человеку, не правилу."""
    rows = [_week(1, "com.vk.vkclient", 55, 6), _week(1, "dzen.ru", 40, 6)]
    rows += [_week(1, "site%d.ru" % i, 100, 1) for i in range(10)]
    assert suspicious_sites(rows, {"1": 1}) == {}


def test_spam_counts_own_goal_and_sums_campaigns():
    """Конверсии — по цели стратегии кампании; площадка считается целиком."""
    rows = [_week(1, "x.ru", 30, 3, goal=1), _week(2, "x.ru", 30, 3, goal=2)]
    rows[0]["conversions"][2] = 99  # чужая цель кампании 1 не считается
    rows += [_week(1, "site%d.ru" % i, 100, 1) for i in range(10)]
    hit = suspicious_sites(rows, {"1": 1, "2": 2})
    assert "6 достижений с 60 кликов" in hit["x.ru"]


def test_spam_is_pinned_into_every_campaign():
    plan = plan_account([], [_campaign(1), _campaign(2)],
                        spam={"umkaplay.ru": "спам"})
    assert [a["added"][0]["verdict"] for a in plan["actions"]] == ["spam"] * 2


FORCED = ("mail.ru", "m.games.yandex.ru")


def test_forced_sites_are_banned_without_any_clicks():
    """Обязательная минусация не ждёт статистики: список стоит заранее."""
    plan = plan_account([], [_campaign(1)], always_block=FORCED)
    added = [a["placement"] for a in plan["actions"][0]["added"]]
    assert added == ["mail.ru", "m.games.yandex.ru"]
    assert plan["actions"][0]["added"][0]["verdict"] == "forced"


def test_forced_beats_the_dictionary():
    """mail.ru словарь считает крупным порталом — решение человека главнее."""
    assert classify("mail.ru")[0] == "site"
    plan = plan_account([_row(1, "mail.ru", 40)], [_campaign(1)],
                        always_block=FORCED)
    cut = {a["placement"]: a for a in plan["actions"][0]["added"]}
    assert cut["mail.ru"]["verdict"] == "forced"
    assert cut["mail.ru"]["clicks"] == 40
    assert plan["summary"]["forced"]["sites"] == 1


def test_forced_not_repeated_and_not_re_added():
    """Уже запрещённую площадку второй раз не пишем."""
    plan = plan_account([_row(1, "mail.ru", 40)],
                        [_campaign(1, excluded=["mail.ru"])],
                        always_block=FORCED)
    added = [a["placement"] for a in plan["actions"][0]["added"]]
    assert added == ["m.games.yandex.ru"]


def test_forced_only_for_the_account_that_asked():
    """Без списка кабинета почта остаётся нетронутой."""
    plan = plan_account([_row(1, "mail.ru", 40)], [_campaign(1)])
    assert plan["actions"] == []


def test_forced_skips_campaigns_without_the_lever():
    """Остановленная и чужого типа кампания обязательного списка не получает."""
    stopped = dict(_campaign(1), State="SUSPENDED")
    smart = _campaign(2, type_="SMART_CAMPAIGN")
    plan = plan_account([], [stopped, smart], always_block=FORCED)
    assert plan["actions"] == []
    assert plan["refused"] == []


def test_unified_campaign_is_cleaned():
    """ЕПК — формат, в который Директ переводит все новые кампании."""
    plan = plan_account([_row(1, "com.junk.app", 50)],
                        [_campaign(1, type_="UNIFIED_CAMPAIGN")])
    assert plan["actions"][0]["added"][0]["placement"] == "com.junk.app"
    assert plan["refused"] == []


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


# --- вызов API ----------------------------------------------------------

class _Resp:
    status = 200

    def __init__(self, body):
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def test_call_retries_temporary_direct_error(monkeypatch):
    """1000 «Сервис временно недоступен» — повтор, а не потеря кабинета.

    Замер vpo-goroda 28.10.2026: 5 из 12 одинаковых campaigns.get подряд
    вернули 1000, остальные прошли.
    """
    from sync.placements import direct

    answers = [{"error": {"error_code": 1000, "error_string": "Сервис временно недоступен"}},
               {"result": {"Campaigns": [{"Id": 1}]}}]
    monkeypatch.setattr(direct.urllib.request, "urlopen",
                        lambda req, timeout: _Resp(answers.pop(0)))
    monkeypatch.setattr(direct.time, "sleep", lambda s: None)
    assert direct.call("t", "l", "campaigns", {}) == {"Campaigns": [{"Id": 1}]}


def test_call_does_not_retry_real_error(monkeypatch):
    """Ошибка запроса (8800 и прочие) повтором не лечится — сразу наверх."""
    import pytest
    from sync.placements import direct

    calls = []

    def urlopen(req, timeout):
        calls.append(1)
        return _Resp({"error": {"error_code": 8800, "error_string": "Объект не найден"}})

    monkeypatch.setattr(direct.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(direct.time, "sleep", lambda s: None)
    with pytest.raises(direct.DirectError):
        direct.call("t", "l", "campaigns", {})
    assert len(calls) == 1


def test_cli_routes_project_and_legacy_accounts(monkeypatch):
    """--accounts bjorn → раннер модулей; synergy_school → старый движок, не раннер."""
    from sync import placements_clean as cli

    projects, old = [], []
    monkeypatch.setattr(cli, "run_project", lambda slug, **kw: projects.append(slug) or 0)
    monkeypatch.setattr(cli.legacy, "run_account",
                        lambda acc, apply, use_db, log: old.append((acc.key, apply)))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert cli.main(["--apply", "--accounts", "bjorn,synergy_school"]) == 0
    assert projects == ["bjorn"] and old == [("synergy_school", True)]

    projects.clear(), old.clear()
    assert cli.main(["--apply", "--accounts", "synergy_school"]) == 0
    assert projects == [] and old == [("synergy_school", True)]

    projects.clear(), old.clear()
    assert cli.main(["--apply"]) == 0
    assert projects == ["edunetwork"] and old == []
