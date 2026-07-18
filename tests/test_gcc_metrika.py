import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.gcc_metrika import parse_metrika_traffic, residual_rows, resolve_engine


def test_parse_metrika_traffic():
    p = os.path.join(os.path.dirname(__file__), "fixtures", "metrika_traffic_sample.json")
    with open(p, encoding="utf-8") as f:
        resp = json.load(f)
    rows = parse_metrika_traffic(resp)
    assert len(rows) == len(resp["data"])
    first = rows[0]
    assert first["date"] == "2026-07-17"
    assert first["traffic_source"] == "ad"
    # Фикстура записана старым запросом (движок читался напрямую). Теперь площадка
    # восстанавливается из utm/searchEngine, которых в этом ответе нет → None.
    # Площадку проверяют тесты resolve_engine ниже.
    assert first["source_engine"] is None
    assert first["visits"] == 1392.0 and first["users"] == 1024.0
    # строка direct: engine None
    direct = [r for r in rows if r["traffic_source"] == "direct"][0]
    assert direct["source_engine"] is None
    # без dimension домена страна не определяется (паритет RU/KZ: country=NULL)
    assert all(r["country"] is None for r in rows)


def test_parse_metrika_traffic_with_domain():
    """Фикстура зонда P1: dimensions = date, startURLDomain, trafficSource, sourceEngine."""
    p = os.path.join(os.path.dirname(__file__), "fixtures", "metrika_domain_sample.json")
    with open(p, encoding="utf-8") as f:
        resp = json.load(f)
    rows = parse_metrika_traffic(resp)
    assert len(rows) == len(resp["data"])
    # порядок полей не съехал: дата/источник/движок читаются по имени dimension, не по позиции
    first = rows[0]
    assert first["date"] == "2026-07-17"
    assert first["traffic_source"] == "ad"
    assert first["source_engine"] is None   # см. комментарий выше: в фикстуре нет utm
    assert first["country"] == "ОАЭ"
    # в фикстуре есть несколько стран, все распознаны
    assert {r["country"] for r in rows} == {
        "ОАЭ", "Саудовская Аравия", "Кувейт", "Катар", "Оман"
    }

# === Остаток: визиты, не разнесённые по доменам (T5) ===
#
# Метрика при кроссе ym:s:startURLDomain с lastsignTrafficSource+lastsignSourceEngine
# теряет ~2% визитов (4496 → 4396 на 2026-07-17), причём потеря есть и в per-domain
# запросе с фильтром. Чтобы GCC-тотал не просел, разницу пишем строкой country=None.


def test_residual_adds_unattributed_visits():
    totals = [{"date": "2026-07-17", "country": None, "traffic_source": "ad",
               "source_engine": "Google Ads", "visits": 1200, "users": 900}]
    by_country = [
        {"date": "2026-07-17", "country": "ОАЭ", "traffic_source": "ad",
         "source_engine": "Google Ads", "visits": 1125, "users": 850},
        {"date": "2026-07-17", "country": "Катар", "traffic_source": "ad",
         "source_engine": "Google Ads", "visits": 42, "users": 30},
    ]
    rows = residual_rows(totals, by_country)
    assert len(rows) == 1
    r = rows[0]
    assert r["country"] is None
    assert r["traffic_source"] == "ad" and r["source_engine"] == "Google Ads"
    assert r["visits"] == 1200 - 1125 - 42
    assert r["users"] == 900 - 850 - 30


def test_residual_skips_fully_attributed_channels():
    totals = [{"date": "2026-07-17", "country": None, "traffic_source": "direct",
               "source_engine": None, "visits": 100, "users": 90}]
    by_country = [{"date": "2026-07-17", "country": "ОАЭ", "traffic_source": "direct",
                   "source_engine": None, "visits": 100, "users": 90}]
    assert residual_rows(totals, by_country) == []


def test_residual_never_negative():
    """Если разбивка дала больше тотала (расхождение округлений) — строки не создаём."""
    totals = [{"date": "2026-07-17", "country": None, "traffic_source": "ad",
               "source_engine": "Instagram", "visits": 10, "users": 8}]
    by_country = [{"date": "2026-07-17", "country": "ОАЭ", "traffic_source": "ad",
                   "source_engine": "Instagram", "visits": 12, "users": 11}]
    assert residual_rows(totals, by_country) == []


def test_residual_channel_missing_in_country_split():
    """Канал есть в тотале, но целиком выпал из разбивки → весь его объём в остаток."""
    totals = [{"date": "2026-07-17", "country": None, "traffic_source": "referral",
               "source_engine": "shop.app", "visits": 25, "users": 20}]
    rows = residual_rows(totals, [])
    assert len(rows) == 1 and rows[0]["visits"] == 25 and rows[0]["country"] is None


def test_residual_users_clamped_at_zero():
    """Визиты просели, а юзеры нет — отрицательных юзеров не пишем."""
    totals = [{"date": "2026-07-17", "country": None, "traffic_source": "ad",
               "source_engine": "Google Ads", "visits": 100, "users": 50}]
    by_country = [{"date": "2026-07-17", "country": "ОАЭ", "traffic_source": "ad",
                   "source_engine": "Google Ads", "visits": 90, "users": 55}]
    rows = residual_rows(totals, by_country)
    assert len(rows) == 1 and rows[0]["visits"] == 10 and rows[0]["users"] == 0


# === Площадка по utm вместо режущего движка ===
# Зонд 2026-07-18: lastsignSourceEngine/Name/AdvEngine выбрасывают мелкие комбинации
# (Бахрейн: 505 визитов и 7 источников → 480 и 3). utm-метки и searchEngine — не режут.


def test_engine_google_by_utm_source():
    assert resolve_engine("ad", "google", None, None) == "Google Ads"
    assert resolve_engine("ad", "GOOGLE", None, None) == "Google Ads"


def test_engine_meta_by_utm_source():
    for src in ("ig", "instagram", "facebook", "fb", "meta"):
        assert resolve_engine("ad", src, None, None) in ("Instagram", "Facebook")


def test_engine_google_by_campaign_id_shape():
    """Google Ads пишет в utm_campaign свой id (ValueTrack) — 10-12 цифр."""
    assert resolve_engine("ad", None, "21087796023", None) == "Google Ads"


def test_engine_meta_by_campaign_id_shape():
    """У Meta id заметно длиннее (17-19 цифр)."""
    assert resolve_engine("ad", None, "120239706697970017", None) == "Instagram"


def test_engine_meta_by_campaign_text_label():
    assert resolve_engine("ad", None, "Instagram_Stories-CPO_SALE70_UAE", None) == "Instagram"


def test_engine_search_from_search_engine_dim():
    assert resolve_engine("organic", None, None, "Google") == "Google"
    assert resolve_engine("organic", None, None, "Bing") == "Bing"


def test_engine_none_when_nothing_known():
    """Нет меток (у Бахрейна реклама идёт без utm) → generic-подканал, но визит не теряется."""
    assert resolve_engine("ad", None, None, None) is None
    assert resolve_engine("internal", None, None, None) is None


def test_engine_feeds_existing_channel_map():
    """Синтетический движок совместим с map_metrika_channel — мерж не трогаем."""
    from sync.gcc_channels import map_metrika_channel
    assert map_metrika_channel("ad", resolve_engine("ad", "google", None, None)) == (
        "SEM", "Google.Adwords", "Платный")
    assert map_metrika_channel("ad", resolve_engine("ad", "ig", None, None)) == (
        "SMM paid", "Meta Ads", "Платный")
    ch, sub, tt = map_metrika_channel("ad", resolve_engine("ad", None, None, None))
    assert ch == "SEM" and tt == "Платный"


def test_residual_matches_channel_only_reference():
    """Эталон запрашивает только (дата, источник) — площадка в ключ сверки не входит.

    Регрессия dry-run 2026-07-17: с площадкой в ключе строки не матчились и остаток
    раздувался со 100 до 2523 визитов, задваивая трафик.
    """
    totals = [{"date": "2026-07-17", "country": None, "campaign": None,
               "traffic_source": "ad", "source_engine": None, "visits": 1000, "users": 800}]
    by_country = [
        {"date": "2026-07-17", "country": "ОАЭ", "campaign": "21087796023",
         "traffic_source": "ad", "source_engine": "Google Ads", "visits": 700, "users": 600},
        {"date": "2026-07-17", "country": "Катар", "campaign": None,
         "traffic_source": "ad", "source_engine": "Instagram", "visits": 250, "users": 180},
    ]
    rows = residual_rows(totals, by_country)
    assert len(rows) == 1 and rows[0]["visits"] == 50
