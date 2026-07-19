# -*- coding: utf-8 -*-
"""Склейка визитов Метрики с кампаниями кабинетов + правило расхода."""
from datetime import datetime, timedelta, timezone

from sync.lime_kz_campaigns import (
    ENTITY_MAX_AGE_DAYS,
    NO_CAMPAIGN,
    CampaignRef,
    resolve_campaign,
    warn_if_entities_stale,
)

DIRECT_MAP = {
    "Смарт баннеры CPO": ("117776765", True),            # LIME-KZ1
    "Поиск. Бренд": ("119566511", True),                 # LIME-KZ1
    "ТК муж.": ("706806515", False),                     # performance21lime (RU)
    "Дубль имени": None,                                 # имя ведёт к нескольким id
}
GOOGLE_MAP = {"23952118304": "PMax Retargeting", "23882926743": "Pmax 2"}
# Смешанный справочник: id группы объявлений и id объявления резолвятся одинаково —
# entity_map не различает kind, только формат id (не пересекаются в реальных данных).
ENTITY_MAP = {
    "193954649928": ("23237404958", "Бренд. Поиск 2. Гео: Казахстан #3"),  # ad_group
    "782935363650": ("23237404958", "Бренд. Поиск 2. Гео: Казахстан #3"),  # ad
}


def _row(**kw):
    base = {"direct_campaign_name": None, "utm_campaign": None, "utm_content": None}
    base.update(kw)
    return base


def test_direct_campaign_resolved_by_name_kz_cabinet():
    ref = resolve_campaign(_row(direct_campaign_name="Смарт баннеры CPO"),
                           DIRECT_MAP, GOOGLE_MAP, ENTITY_MAP)
    assert ref == CampaignRef("117776765", "Смарт баннеры CPO", True)


def test_direct_campaign_from_ru_cabinet_is_not_kz():
    """Пролив RU-кабинета на KZ-гео: кампания известна, но расход в KZ не переносим."""
    ref = resolve_campaign(_row(direct_campaign_name="ТК муж."),
                           DIRECT_MAP, GOOGLE_MAP, ENTITY_MAP)
    assert ref == CampaignRef("706806515", "ТК муж.", False)


def test_ambiguous_direct_name_is_unresolved():
    """Одно имя → несколько id: не приписываем наугад."""
    ref = resolve_campaign(_row(direct_campaign_name="Дубль имени"),
                           DIRECT_MAP, GOOGLE_MAP, ENTITY_MAP)
    assert ref == NO_CAMPAIGN


def test_google_pmax_resolved_by_numeric_utm_campaign():
    ref = resolve_campaign(_row(utm_campaign="23952118304"),
                           DIRECT_MAP, GOOGLE_MAP, ENTITY_MAP)
    assert ref == CampaignRef("23952118304", "PMax Retargeting", True)


def test_google_search_resolved_via_ad_group_entity():
    """utm_campaign='g' у поиска — разрешаем через справочник сущностей по utm_content
    (id принадлежит группе объявлений)."""
    ref = resolve_campaign(_row(utm_campaign="g", utm_content="193954649928"),
                           DIRECT_MAP, GOOGLE_MAP, ENTITY_MAP)
    assert ref == CampaignRef("23237404958", "Бренд. Поиск 2. Гео: Казахстан #3", True)


def test_google_search_resolved_via_ad_entity():
    """Тот же путь резолвится и когда utm_content — id объявления (kind='ad'): справочник
    общий, resolve_campaign не различает вид сущности, только сам id."""
    ref = resolve_campaign(_row(utm_campaign="g", utm_content="782935363650"),
                           DIRECT_MAP, GOOGLE_MAP, ENTITY_MAP)
    assert ref == CampaignRef("23237404958", "Бренд. Поиск 2. Гео: Казахстан #3", True)


def test_unknown_entity_is_unresolved():
    ref = resolve_campaign(_row(utm_campaign="g", utm_content="999999999999"),
                           DIRECT_MAP, GOOGLE_MAP, ENTITY_MAP)
    assert ref == NO_CAMPAIGN


def test_direct_wins_over_utm_when_both_present():
    """У Директа utm_campaign иногда содержит номер кампании — имя приоритетнее."""
    ref = resolve_campaign(_row(direct_campaign_name="Поиск. Бренд", utm_campaign="23952118304"),
                           DIRECT_MAP, GOOGLE_MAP, ENTITY_MAP)
    assert ref == CampaignRef("119566511", "Поиск. Бренд", True)


def test_organic_row_has_no_campaign():
    assert resolve_campaign(_row(), DIRECT_MAP, GOOGLE_MAP, ENTITY_MAP) == NO_CAMPAIGN


def test_utm_campaign_wins_over_utm_content_when_both_present():
    """PMax utm_campaign проверяется раньше пути через справочник сущностей по utm_content:
    если оба валидны одновременно, до entity_map дело не доходит."""
    ref = resolve_campaign(_row(utm_campaign="23952118304", utm_content="782935363650"),
                           DIRECT_MAP, GOOGLE_MAP, ENTITY_MAP)
    assert ref == CampaignRef("23952118304", "PMax Retargeting", True)


# ── Свежесть справочника сущностей кампании ───────────────────────────────────
# Справочник наполняет Google Ads Script в рекламном кабинете — вне workflow и без
# расписания. Его остановка не роняет синк: расход поисковых кампаний Google KZ просто
# станет 0 при живых визитах (ДРР/CPO/окупаемость выглядят лучше реальности). Значит
# протухание обязано быть громким.

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)


def test_warns_when_entity_dictionary_is_empty(capsys):
    """Пустой справочник — ровно то состояние, в котором ни одна сущность не резолвится."""
    assert warn_if_entities_stale(0, None, now=NOW) is True
    out = capsys.readouterr().out
    assert "lime_kz_campaigns: WARN" in out
    assert "lime_google_ads_entities" in out
    assert "ПУСТ" in out


def test_warns_when_entity_dictionary_is_stale(capsys):
    """Записи есть, но скрипт в кабинете давно не отрабатывал → склейка по устаревшим сущностям."""
    stale = NOW - timedelta(days=ENTITY_MAX_AGE_DAYS + 1)
    assert warn_if_entities_stale(120, stale, now=NOW) is True
    out = capsys.readouterr().out
    assert "lime_kz_campaigns: WARN" in out
    assert str(ENTITY_MAX_AGE_DAYS + 1) in out            # возраст в днях
    assert stale.date().isoformat() in out                # дата последней записи


def test_no_warning_when_entity_dictionary_is_fresh(capsys):
    """Свежий справочник — тишина, иначе предупреждение обесценится шумом."""
    fresh = NOW - timedelta(days=1)
    assert warn_if_entities_stale(120, fresh, now=NOW) is False
    assert "WARN" not in capsys.readouterr().out


def test_freshness_threshold_boundary_is_not_noisy(capsys):
    """Ровно на пороге ещё молчим — предупреждаем строго при превышении."""
    edge = NOW - timedelta(days=ENTITY_MAX_AGE_DAYS)
    assert warn_if_entities_stale(5, edge, now=NOW) is False
    assert "WARN" not in capsys.readouterr().out


def test_naive_timestamp_does_not_crash_the_check(capsys):
    """updated_at timestamptz приходит aware, но naive-значение не должно ронять синк."""
    naive = (NOW - timedelta(days=ENTITY_MAX_AGE_DAYS + 2)).replace(tzinfo=None)
    assert warn_if_entities_stale(7, naive, now=NOW) is True
    assert "lime_kz_campaigns: WARN" in capsys.readouterr().out


# ── Meta: четвёртый путь резолвинга ──────────────────────────────────────────
# Замер июня 2026: 54 429 визитов Meta в kz_metrika лежали БЕЗ кампании — resolve_campaign
# знал только Директ, Google PMax и справочник сущностей. Сравнивать по кампаниям Meta
# с Роистатом было нечем.

def _meta_row(utm_campaign, engine="Instagram"):
    return {"direct_campaign_name": None, "utm_campaign": utm_campaign,
            "utm_content": None, "source_engine": engine}


def test_meta_campaign_from_utm_strips_placement_prefix():
    """utm_campaign = «{Плейсмент}-{Имя кампании}» → имя после первого дефиса.

    Сверено с Роистатом на июне: отрезав префикс, получаем ровно marker_level_2.title
    (12 из 20 кампаний, 99.7% визитов Роистата и 98.3% Метрики по Meta).
    """
    ref = resolve_campaign(_meta_row("Instagram_Stories-CPO: ЛЕТНИЙ SALE_ЖЕНЩИНЫ"), {}, {}, {})
    assert ref.campaign_name == "CPO: ЛЕТНИЙ SALE_ЖЕНЩИНЫ"
    assert ref.campaign_id == ""      # у Метрики id кампаний Meta нет
    assert ref.kz_cabinet is False    # кабинета Meta у нас нет — расход не проставляем


def test_meta_campaign_keeps_hyphens_inside_name():
    """Режем только ПЕРВЫЙ дефис: в именах кампаний дефисы встречаются («ЛЕН – Copy»)."""
    ref = resolve_campaign(_meta_row("Instagram_Feed-CPO: SALE -70% Ж"), {}, {}, {})
    assert ref.campaign_name == "CPO: SALE -70% Ж"


def test_meta_campaign_without_placement_prefix_kept_whole():
    ref = resolve_campaign(_meta_row("CPO: НОВИНКИ_24"), {}, {}, {})
    assert ref.campaign_name == "CPO: НОВИНКИ_24"


def test_meta_path_ignores_non_social_engines():
    """Дефис в utm у поисковой кампании не должен приниматься за плейсмент Meta."""
    ref = resolve_campaign(_meta_row("brand-search", engine="Google Ads"), {}, {}, {})
    assert ref == NO_CAMPAIGN


def test_meta_empty_utm_stays_unresolved():
    assert resolve_campaign(_meta_row(""), {}, {}, {}) == NO_CAMPAIGN
    assert resolve_campaign(_meta_row(None), {}, {}, {}) == NO_CAMPAIGN


def test_meta_path_does_not_shadow_google_pmax():
    """utm_campaign = числовой id PMax резолвится Google-путём, а не Meta."""
    ref = resolve_campaign(_meta_row("23952118304", engine="Instagram"),
                           {}, {"23952118304": "PMax Retargeting"}, {})
    assert ref.campaign_id == "23952118304"
    assert ref.campaign_name == "PMax Retargeting"
