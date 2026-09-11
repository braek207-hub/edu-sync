"""Ключ кампании Метрики RU в терминах витрины PROCONTEXT (аудит 12.08–10.09.2026)."""
from sync.lime_ru_metrika import COLUMNS, build_rows
from sync.lime_ru_metrika_keys import campaign_key, ru_channel

VK_GROUPS = {"149891557": ("2780001", "APP: iOs Женщины")}


def _col(row, name):
    return row[COLUMNS.index(name)]


def test_direct_uses_click_order_id_when_utm_is_empty():
    # 114 тыс. визитов/мес: клик Директа без utm_campaign, кампания известна по yclid
    assert campaign_key("SEM", "Яндекс.Директ", "Платный", "", "117431086",
                        "Смарт Баннеры Ретаргет. CPO 1", None) == ("117431086", "Смарт Баннеры Ретаргет. CPO 1")


def test_direct_click_order_beats_broken_utm():
    assert campaign_key("SEM", "Яндекс.Директ", "Платный", "{campaign_id}", "117431086", "X", None)[0] == "117431086"
    assert campaign_key("SEM", "Яндекс.Директ", "Платный", "W_Osen2026_14.08.26", "117431086", "X", None)[0] == "117431086"


def test_direct_numeric_utm_without_click_order_is_kept():
    assert campaign_key("SEM", "Яндекс.Директ", "Платный", "703083615", None, "", None) == ("703083615", "")


def test_direct_without_any_id_falls_to_channel_level():
    assert campaign_key("SEM", "Яндекс.Директ", "Платный", "W_Osen2026", None, "", None) == ("", "")


def test_vk_group_id_resolves_to_ad_plan():
    assert campaign_key("SMM paid", "VK.Ads", "Платный", "149891557", None, "", VK_GROUPS) == ("2780001", "APP: iOs Женщины")


def test_vk_unknown_group_falls_to_channel_level_not_lost():
    assert campaign_key("SMM paid", "VK.Ads", "Платный", "999", None, "", VK_GROUPS) == ("", "")


def test_free_channels_have_no_campaign_key():
    # витрина хранит campaign_id только у платных → рассылка Email садится на строку канала
    assert campaign_key("CRM", "Email", "Бесплатный", "W_Osen2026_14.08.26Rassylka", None, "", None) == ("", "")


def test_vk_ads_engine_is_smm_paid_vk_ads():
    assert ru_channel("ad", "VK Ads")[:2] == ("SMM paid", "VK.Ads")


def test_referral_domains_collapse_to_vitrina_subchannel():
    assert ru_channel("referral", "pay.yandex.ru") == ("Referrals", "Реферал", "Бесплатный")


def test_build_rows_merges_utm_less_direct_visits_into_campaign_row():
    common = {"visits": 10, "users": 8, "new_users": 3, "bounce_rate": 20.0, "page_depth": 3.0,
              "avg_duration": 60.0, "card_view": 1, "look_image": 0, "cart_reaches": 1,
              "checkout_reaches": 0, "orders": 1, "revenue": 1000.0}
    rows = build_rows([
        {"traffic_source": "ad", "source_engine": "Yandex: Direct", "utm_campaign": "117431086",
         "direct_order_id": "117431086", "direct_campaign_name": "CPO 1", **common},
        {"traffic_source": "ad", "source_engine": "Yandex: Direct", "utm_campaign": "",
         "direct_order_id": "117431086", "direct_campaign_name": "CPO 1", **common},
    ], "2026-09-01")
    assert len(rows) == 1
    assert _col(rows[0], "campaign_id") == "117431086"
    assert _col(rows[0], "visits") == 20


def test_build_rows_resolves_vk_group_to_campaign():
    row = {"traffic_source": "ad", "source_engine": "VK Ads", "utm_campaign": "149891557",
           "direct_order_id": None, "direct_campaign_name": "", "visits": 5, "orders": 0}
    rows = build_rows([row], "2026-09-01", VK_GROUPS)
    assert (_col(rows[0], "channel"), _col(rows[0], "subchannel")) == ("SMM paid", "VK.Ads")
    assert _col(rows[0], "campaign_id") == "2780001"
    assert _col(rows[0], "campaign_name") == "APP: iOs Женщины"
