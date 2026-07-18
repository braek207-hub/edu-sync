# -*- coding: utf-8 -*-
"""Склейка KZ-визитов Метрики с кампаниями кабинетов + расход по правилу KZ-кабинетов.

Три пути разрешения кампании (спека 2026-07-18-lime-kz-metrika-design.md):
  1. Директ — по имени из ym:s:lastsignDirectClickOrderName (id у измерения нет);
     имена в кабинетах уникальны, неоднозначное имя считаем нераспознанным.
  2. Google PMax — utm_campaign содержит реальный campaign_id.
  3. Google поиск — utm_campaign статичный 'g', id группы объявлений в utm_content
     разрешается через справочник lime_google_ads_ad_groups.

Расход берём ТОЛЬКО по кампаниям KZ-кабинетов: в гео-срез попадает пролив RU-кабинета,
чей расход принадлежит региону RU и там уже учтён.
"""
from typing import NamedTuple

KZ_DIRECT_LOGIN = "LIME-KZ1"


class CampaignRef(NamedTuple):
    campaign_id: str
    campaign_name: str
    kz_cabinet: bool


NO_CAMPAIGN = CampaignRef("", "", False)


def resolve_campaign(row: dict, direct_map: dict, google_map: dict, adgroup_map: dict) -> CampaignRef:
    """Определить кампанию строки Метрики.

    Args:
        row: строка sync.lime_kz_metrika_api.parse_metrika_kz.
        direct_map: имя кампании → (campaign_id, kz_cabinet) либо None при неоднозначности.
        google_map: campaign_id → имя (только кампании Google KZ).
        adgroup_map: ad_group_id → (campaign_id, campaign_name).

    Returns:
        CampaignRef; NO_CAMPAIGN если кампанию распознать нельзя.
    """
    name = (row.get("direct_campaign_name") or "").strip()
    if name:
        hit = direct_map.get(name)
        if hit is None:
            return NO_CAMPAIGN  # имени нет в кабинетах или оно неоднозначно
        campaign_id, kz_cabinet = hit
        return CampaignRef(campaign_id, name, kz_cabinet)

    utm_campaign = (row.get("utm_campaign") or "").strip()
    if utm_campaign in google_map:
        return CampaignRef(utm_campaign, google_map[utm_campaign], True)

    utm_content = (row.get("utm_content") or "").strip()
    hit = adgroup_map.get(utm_content)
    if hit:
        campaign_id, campaign_name = hit
        return CampaignRef(campaign_id, campaign_name, True)

    return NO_CAMPAIGN


SELECT_DIRECT = """
SELECT campaign_name, campaign_id, client_login
FROM lime_direct_stats
WHERE date >= %s AND date <= %s AND campaign_name IS NOT NULL AND campaign_name <> ''
GROUP BY campaign_name, campaign_id, client_login
"""

SELECT_GOOGLE = """
SELECT campaign_id, MAX(campaign_name) AS campaign_name
FROM lime_google_ads_stats
WHERE region = 'kz' AND date >= %s AND date <= %s
GROUP BY campaign_id
"""

# Таблица мультирегиональна (ingest пишет kz/ru/gcc) — без фильтра региона чужая
# группа объявлений может отдать id RU/GCC-кампании с флагом kz_cabinet=True.
SELECT_ADGROUPS = """
SELECT ad_group_id, campaign_id, COALESCE(campaign_name, '') AS campaign_name
FROM lime_google_ads_ad_groups
WHERE region = 'kz'
"""

# Расход KZ-кабинетов: Директ LIME-KZ1 (cost уже с НДС) + Google KZ (cost_rub посчитан
# sync/google_ads_fx.py по курсу ЦБ; если его ещё нет — строка расхода пропускается,
# чтобы не подмешать доллары в рубли).
SELECT_COST_DIRECT = """
SELECT date::text AS date, campaign_id, SUM(COALESCE(cost, 0))::float AS cost
FROM lime_direct_stats
WHERE client_login = %s AND date >= %s AND date <= %s
GROUP BY date, campaign_id
"""

SELECT_COST_GOOGLE = """
SELECT date::text AS date, campaign_id, SUM(COALESCE(cost_rub, 0))::float AS cost
FROM lime_google_ads_stats
WHERE region = 'kz' AND date >= %s AND date <= %s AND cost_rub IS NOT NULL
GROUP BY date, campaign_id
"""


def load_direct_map(conn, frm: str, to: str) -> dict:
    """Имя кампании Директа → (campaign_id, kz_cabinet). Неоднозначное имя → None."""
    with conn.cursor() as cur:
        cur.execute(SELECT_DIRECT, (frm, to))
        rows = cur.fetchall()

    seen: dict[str, set] = {}
    logins: dict[tuple[str, str], str] = {}
    for name, campaign_id, client_login in rows:
        seen.setdefault(name, set()).add(str(campaign_id))
        logins[(name, str(campaign_id))] = client_login or ""

    out: dict = {}
    for name, ids in seen.items():
        if len(ids) != 1:
            out[name] = None  # одно имя на несколько кампаний — не гадаем
            continue
        campaign_id = next(iter(ids))
        out[name] = (campaign_id, logins.get((name, campaign_id)) == KZ_DIRECT_LOGIN)
    return out


def load_google_map(conn, frm: str, to: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(SELECT_GOOGLE, (frm, to))
        return {str(cid): (name or "") for cid, name in cur.fetchall()}


def load_adgroup_map(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute(SELECT_ADGROUPS)
        return {str(ag): (str(cid), name) for ag, cid, name in cur.fetchall()}


def load_cost_map(conn, frm: str, to: str) -> dict:
    """(дата, campaign_id) → расход в рублях. Только KZ-кабинеты."""
    out: dict[tuple[str, str], float] = {}
    direct_ids: set = set()
    google_ids: set = set()
    with conn.cursor() as cur:
        cur.execute(SELECT_COST_DIRECT, (KZ_DIRECT_LOGIN, frm, to))
        for date_s, campaign_id, cost in cur.fetchall():
            cid = str(campaign_id)
            direct_ids.add(cid)
            out[(date_s, cid)] = out.get((date_s, cid), 0.0) + float(cost or 0)
        cur.execute(SELECT_COST_GOOGLE, (frm, to))
        for date_s, campaign_id, cost in cur.fetchall():
            cid = str(campaign_id)
            google_ids.add(cid)
            out[(date_s, cid)] = out.get((date_s, cid), 0.0) + float(cost or 0)

    # Ключ (дата, campaign_id) общий для Директа и Google — площадка в нём не закодирована.
    # Если id численно совпадут, расход двух разных кампаний тихо сольётся в одну сумму.
    # Не падаем: совпадение id между площадками не блокирует остальной синк и само по себе
    # не значит порчу данных (может быть безобидным совпадением) — но должно быть замечено
    # и разобрано вручную, поэтому громкое предупреждение вместо тихой неверной суммы.
    collisions = direct_ids & google_ids
    if collisions:
        print(f"lime_kz_campaigns: WARN campaign_id пересекается между Директ и Google KZ, "
              f"расход мог слиться: {sorted(collisions)}")

    return out
