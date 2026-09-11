# -*- coding: utf-8 -*-
"""Ключ кампании и подканал Метрики RU в канон витрины PROCONTEXT (lime_stats, region=ru).

Дашборд обогащает строки витрины данными Метрики по (date, campaign_id) для рекламы и по
(date, channel, subchannel) для остального. Аудит 12.08–10.09.2026 показал, где ключи
не сходятся и данные Метрики либо теряются, либо садятся не на ту строку:

  • Директ: campaign_id брался из utm_campaign. У 114 тыс. визитов utm пуст, у 29 тыс.
    там имя рассылки или литеральный `{campaign_id}` — а кампания Директа по клику
    (yclid → ym:s:lastsignDirectClickOrder) Метрике известна. Берём её.
  • VK Ads: в utm_campaign макрос VK кладёт id ГРУППЫ (наследие myTarget), витрина живёт
    по id кампании (ad_plan). Резолвим через lime_vk_entities — тот же справочник, что
    sync/lime.py применяет к витрине.
  • Бесплатные каналы: витрина хранит campaign_id только у платных (sync/lime.py
    aggregate), поэтому рассылки Email/Telegram с utm_campaign не встречали свою
    строку и терялись (19 тыс. визитов / 370 заказов). Ключ — уровень канала.
  • Рефералы: Метрика отдаёт домен, витрина — единый «Реферал» (classify: medium
    referral). ~70 тыс. визитов не встречали строку.

Общий модуль sync/metrika_channels.py не трогаем: для KZ и GCC витрина — сама Метрика,
там домены рефералов и Internal — канон.
"""
import re

from sync.metrika_channels import map_metrika_channel

_DIGITS = re.compile(r"^\d+$")
PAID_TRAFFIC = "Платный"


def ru_channel(traffic_source_id, source_engine, utm_source=None) -> tuple[str, str, str]:
    """map_metrika_channel + сведение подканалов к канону витрины RU."""
    channel, subchannel, traffic_type = map_metrika_channel(
        traffic_source_id, source_engine, utm_source)
    if channel == "Referrals":
        subchannel = "Реферал"
    return channel, subchannel, traffic_type


def campaign_key(channel: str, subchannel: str, traffic_type: str, utm_campaign,
                 direct_order_id, direct_order_name, vk_groups: dict | None) -> tuple[str, str]:
    """(campaign_id, campaign_name) в терминах витрины; ('', '') = уровень канала."""
    utm = (utm_campaign or "").strip()
    if traffic_type != PAID_TRAFFIC:
        return "", ""
    if subchannel == "Яндекс.Директ":
        oid = str(direct_order_id or "").strip()
        name = (direct_order_name or "").strip()
        if oid and _DIGITS.match(oid):
            return oid, name
        if _DIGITS.match(utm):
            return utm, name
        return "", ""
    if subchannel == "VK.Ads":
        # Группы вне справочника — на уровень канала (строка VK.Ads без кампании есть
        # в витрине), а не под несуществующий ключ, где запись потеряется.
        hit = (vk_groups or {}).get(utm)
        return (hit[0], hit[1]) if hit else ("", "")
    return utm, (direct_order_name or "").strip()
