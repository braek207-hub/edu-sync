# -*- coding: utf-8 -*-
"""Каналы и кампании Роистата → таксономия LIME."""
from sync.roistat_channels import IS_OFFLINE, campaign_of, map_roistat_channel


def lvl(l2_id="", l2="", l3_id="", l3=""):
    return {"level2_id": l2_id, "level2": l2, "level3_id": l3_id, "level3": l3}


# ── Каналы ───────────────────────────────────────────────────────────────────

def test_paid_channels_match_lime_taxonomy():
    """Совпадение с sync/metrika_channels.py обязательно: иначе kz_roistat и kz_metrika
    не сравнить поканально — один канал распадётся на две строки."""
    assert map_roistat_channel("Google Ads 1") == ("SEM", "Google.Adwords", "Платный")
    assert map_roistat_channel("Яндекс.Директ 1") == ("SEM", "Яндекс.Директ", "Платный")
    assert map_roistat_channel("Facebook") == ("SMM paid", "Meta Ads", "Платный")


def test_free_channels():
    assert map_roistat_channel("Прямые визиты") == ("Direct", "Direct", "Бесплатный")
    assert map_roistat_channel("SEO") == ("SEO", "SEO Others", "Бесплатный")
    assert map_roistat_channel("Визиты с сайтов") == ("Referrals", "Реферал", "Бесплатный")


# ── Подканалы: level_2 несёт разбивку и ложится на канон LIME ────────────────
# Замер июня: SEO › Google 10 284 / Яндекс 1 076 / Bing 119; «Визиты с сайтов» — 81 домен
# реферера; manual_mindbox_kz › email. Смотреть только level_1 значит потерять подканал
# и не сойтись с Метрикой и витриной при сравнении.

def test_seo_splits_by_engine_like_the_canon():
    assert map_roistat_channel("SEO", "Google", "google")[1] == "SEO Google"
    assert map_roistat_channel("SEO", "Яндекс", "yandex")[1] == "SEO Yandex"
    assert map_roistat_channel("SEO", "Bing", "bing")[1] == "SEO Others"
    assert map_roistat_channel("SEO", "Mail.Ru", "mail")[1] == "SEO Others"


def test_referrals_subchannel_is_referrer_domain():
    """Канон: «Referrals → домен реферера». У Роистата он ровно на level_2."""
    assert map_roistat_channel("Визиты с сайтов", "l.instagram.com")[1] == "l.instagram.com"
    assert map_roistat_channel("Визиты с сайтов", "")[1] == "Реферал"


def test_crm_subchannel_is_mindbox():
    """Канон витрины — «Mindbox»; level_1 Роистата прямо называет систему."""
    assert map_roistat_channel("manual_mindbox_kz", "email") == ("CRM", "Mindbox", "Бесплатный")
    assert map_roistat_channel("mindboxkz_bk", "email")[1] == "Mindbox"


def test_paid_channels_ignore_level2_which_is_campaign_type():
    """У Google/Директа level_2 — тип кампании, а не подканал: канон его не знает."""
    assert map_roistat_channel("Google Ads 1", "Поиск", "g")[1] == "Google.Adwords"
    assert map_roistat_channel("Google Ads 1", "КМС", "d")[1] == "Google.Adwords"
    assert map_roistat_channel("Яндекс.Директ 1", "РСЯ", "context")[1] == "Яндекс.Директ"


def test_direct_has_no_level2():
    assert map_roistat_channel("Прямые визиты", "") == ("Direct", "Direct", "Бесплатный")


def test_channel_name_with_nbsp_still_maps():
    """Подписи приходят с U+00A0; маппер обязан быть к этому устойчив сам."""
    assert map_roistat_channel("Google\xa0Ads\xa01") == ("SEM", "Google.Adwords", "Платный")
    assert map_roistat_channel("Прямые\xa0визиты") == ("Direct", "Direct", "Бесплатный")


def test_crm_mailings_grouped():
    ch, sub, tt = map_roistat_channel("manual_mindbox_kz")
    assert ch == "CRM"
    assert tt == "Бесплатный"
    assert map_roistat_channel("mindboxkz_bk")[0] == "CRM"


def test_offline_deals_are_marked():
    """12% заявок июня (568 из 4 714) — сделки без визита. Рекламе не принадлежат."""
    assert "Сделки, созданные самостоятельно" in IS_OFFLINE
    assert "Сделки с некорректным номером визита" in IS_OFFLINE
    ch, sub, tt = map_roistat_channel("Сделки, созданные самостоятельно")
    assert ch == "Offline"
    assert tt == "Бесплатный"


def test_unknown_channel_is_not_guessed():
    """Незнакомый канал не приписываем наугад — он виден как есть."""
    ch, sub, tt = map_roistat_channel("новый_источник_2027")
    assert ch == "Others"
    assert sub == "новый_источник_2027"
    assert tt == "Бесплатный"


def test_empty_channel_is_unknown():
    assert map_roistat_channel("") == ("Others", "Unknown", "Бесплатный")


# ── Кампании ─────────────────────────────────────────────────────────────────

def test_campaign_of_google_and_direct_is_level3_with_real_id():
    """У Google/Директа level_2 — код типа, level_3 — кампания, её value = наш id."""
    assert campaign_of("Google Ads 1", lvl("g", "Поиск", "23237404958",
                                           "Бренд. Поиск 2. Гео: Казахстан #3")) == \
        ("23237404958", "Бренд. Поиск 2. Гео: Казахстан #3")
    assert campaign_of("Яндекс.Директ 1", lvl("context", "РСЯ", "117776765",
                                              "Смарт баннеры CPO")) == \
        ("117776765", "Смарт баннеры CPO")


def test_campaign_of_facebook_is_level2():
    """У Facebook наоборот: level_2 — кампания, level_3 — адсет.

    Взяв level_3 для всех, получим семь строк «CPO_Ж» вместо кампаний Meta.
    """
    assert campaign_of("Facebook", lvl("120254142253170405", "CPO: ЛЕТНИЙ SALE_ЖЕНЩИНЫ",
                                       "120254142253190405", "CPO_Ж")) == \
        ("120254142253170405", "CPO: ЛЕТНИЙ SALE_ЖЕНЩИНЫ")


def test_campaign_of_handles_no_value_literal():
    """Роистат пишет литерал «Нет значения», а не пустую строку (у SEO level_3)."""
    assert campaign_of("SEO", lvl("bing", "Bing", "", "Нет значения")) == ("", "")
    assert campaign_of("Прямые визиты", lvl()) == ("", "")


def test_campaign_of_survives_nbsp_in_channel():
    """Канал приходит с NBSP — выбор уровня не должен от этого зависеть."""
    assert campaign_of("Facebook", lvl("120254142253170405", "CPO: ТЕСТ",
                                       "120254142253190405", "CPO_Ж"))[1] == "CPO: ТЕСТ"
    assert campaign_of("Google\xa0Ads\xa01", lvl("g", "Поиск", "111", "Кампания"))[1] == \
        "Кампания"
