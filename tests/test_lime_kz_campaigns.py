# -*- coding: utf-8 -*-
"""Склейка визитов Метрики с кампаниями кабинетов + правило расхода."""
from sync.lime_kz_campaigns import NO_CAMPAIGN, CampaignRef, resolve_campaign

DIRECT_MAP = {
    "Смарт баннеры CPO": ("117776765", True),            # LIME-KZ1
    "Поиск. Бренд": ("119566511", True),                 # LIME-KZ1
    "ТК муж.": ("706806515", False),                     # performance21lime (RU)
    "Дубль имени": None,                                 # имя ведёт к нескольким id
}
GOOGLE_MAP = {"23952118304": "PMax Retargeting", "23882926743": "Pmax 2"}
ADGROUP_MAP = {"782935363650": ("23237404958", "Бренд. Поиск 2. Гео: Казахстан #3")}


def _row(**kw):
    base = {"direct_campaign_name": None, "utm_campaign": None, "utm_content": None}
    base.update(kw)
    return base


def test_direct_campaign_resolved_by_name_kz_cabinet():
    ref = resolve_campaign(_row(direct_campaign_name="Смарт баннеры CPO"),
                           DIRECT_MAP, GOOGLE_MAP, ADGROUP_MAP)
    assert ref == CampaignRef("117776765", "Смарт баннеры CPO", True)


def test_direct_campaign_from_ru_cabinet_is_not_kz():
    """Пролив RU-кабинета на KZ-гео: кампания известна, но расход в KZ не переносим."""
    ref = resolve_campaign(_row(direct_campaign_name="ТК муж."),
                           DIRECT_MAP, GOOGLE_MAP, ADGROUP_MAP)
    assert ref == CampaignRef("706806515", "ТК муж.", False)


def test_ambiguous_direct_name_is_unresolved():
    """Одно имя → несколько id: не приписываем наугад."""
    ref = resolve_campaign(_row(direct_campaign_name="Дубль имени"),
                           DIRECT_MAP, GOOGLE_MAP, ADGROUP_MAP)
    assert ref == NO_CAMPAIGN


def test_google_pmax_resolved_by_numeric_utm_campaign():
    ref = resolve_campaign(_row(utm_campaign="23952118304"),
                           DIRECT_MAP, GOOGLE_MAP, ADGROUP_MAP)
    assert ref == CampaignRef("23952118304", "PMax Retargeting", True)


def test_google_search_resolved_via_ad_group():
    """utm_campaign='g' у поиска — разрешаем через справочник групп по utm_content."""
    ref = resolve_campaign(_row(utm_campaign="g", utm_content="782935363650"),
                           DIRECT_MAP, GOOGLE_MAP, ADGROUP_MAP)
    assert ref == CampaignRef("23237404958", "Бренд. Поиск 2. Гео: Казахстан #3", True)


def test_unknown_ad_group_is_unresolved():
    ref = resolve_campaign(_row(utm_campaign="g", utm_content="999999999999"),
                           DIRECT_MAP, GOOGLE_MAP, ADGROUP_MAP)
    assert ref == NO_CAMPAIGN


def test_direct_wins_over_utm_when_both_present():
    """У Директа utm_campaign иногда содержит номер кампании — имя приоритетнее."""
    ref = resolve_campaign(_row(direct_campaign_name="Поиск. Бренд", utm_campaign="23952118304"),
                           DIRECT_MAP, GOOGLE_MAP, ADGROUP_MAP)
    assert ref == CampaignRef("119566511", "Поиск. Бренд", True)


def test_organic_row_has_no_campaign():
    assert resolve_campaign(_row(), DIRECT_MAP, GOOGLE_MAP, ADGROUP_MAP) == NO_CAMPAIGN


def test_utm_campaign_wins_over_utm_content_when_both_present():
    """PMax utm_campaign проверяется раньше пути через справочник групп по utm_content:
    если оба валидны одновременно, до adgroup_map дело не доходит."""
    ref = resolve_campaign(_row(utm_campaign="23952118304", utm_content="782935363650"),
                           DIRECT_MAP, GOOGLE_MAP, ADGROUP_MAP)
    assert ref == CampaignRef("23952118304", "PMax Retargeting", True)
