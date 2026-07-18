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
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

KZ_DIRECT_LOGIN = "LIME-KZ1"

# Порог протухания справочника групп объявлений.
# Справочник наполняет Google Ads Script из рекламного кабинета через /api/ingest/google-ads —
# он вне этого репозитория, вне workflow и без расписания, поэтому его остановку никто не
# заметит: синк отработает «успешно», просто расход поисковых кампаний Google станет 0.
# 7 дней: скрипт в кабинете рассчитан на ежедневный прогон, так что свежая запись должна
# появляться минимум раз в сутки; недельное окно переживает разовый сбой, длинные праздники
# и ручную паузу кабинета, но ловит настоящую остановку задолго до того, как протухшая склейка
# испортит месячный отчёт. Совпадает с окном бэкфилла LIME_KZ_METRIKA_DAYS_BACK=30 по духу:
# предупреждение приходит, пока данные ещё в зоне пересчёта.
ADGROUP_MAX_AGE_DAYS = 7


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

# Свежесть справочника — по тому же срезу region='kz', что и сам справочник выше.
SELECT_ADGROUPS_FRESHNESS = """
SELECT COUNT(*)::int AS row_count, MAX(updated_at) AS newest
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


def warn_if_adgroups_stale(row_count: int, newest, now: datetime | None = None) -> bool:
    """Предупредить, если справочник групп объявлений пуст или протух.

    Пустой/протухший справочник не роняет синк, но тихо обнуляет расход поисковых кампаний
    Google KZ: utm_content не резолвится → campaign_id пустой → cost_map не отдаёт расход,
    хотя визиты и заказы на месте (ДРР/CPO/окупаемость выглядят лучше реальности).

    Args:
        row_count: число строк справочника для региона KZ.
        newest: MAX(updated_at) справочника (aware datetime) либо None у пустой таблицы.
        now: точка отсчёта (для тестов); по умолчанию текущий UTC.

    Returns:
        True, если предупреждение напечатано.
    """
    if row_count > 0 and newest is None:
        # Строки есть, а updated_at пуст — колонка NOT NULL DEFAULT now(), так быть не должно.
        print("lime_kz_campaigns: WARN справочник lime_google_ads_ad_groups без updated_at — "
              "проверь схему (миграция 010) и ingest /api/ingest/google-ads")
        return True

    if row_count == 0:
        print("lime_kz_campaigns: WARN справочник групп объявлений lime_google_ads_ad_groups ПУСТ "
              "→ поисковые кампании Google KZ не резолвятся и их расход станет 0 при живых "
              "визитах. Проверь Google Ads Script в кабинете (ingest /api/ingest/google-ads)")
        return True

    now = now or datetime.now(timezone.utc)
    # timestamptz из psycopg2 приходит aware; подстраховка на случай naive-значения.
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    age = now - newest
    if age > timedelta(days=ADGROUP_MAX_AGE_DAYS):
        print(f"lime_kz_campaigns: WARN справочник lime_google_ads_ad_groups не обновлялся "
              f"{age.days} дн. (порог {ADGROUP_MAX_AGE_DAYS}, последняя запись "
              f"{newest.date().isoformat()}) → склейка поисковых кампаний Google KZ идёт по "
              f"устаревшим группам, расход новых кампаний станет 0. Проверь Google Ads Script "
              f"в кабинете (ingest /api/ingest/google-ads)")
        return True
    return False


def load_adgroup_map(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute(SELECT_ADGROUPS_FRESHNESS)
        row_count, newest = cur.fetchone()
        cur.execute(SELECT_ADGROUPS)
        out = {str(ag): (str(cid), name) for ag, cid, name in cur.fetchall()}
    # Проверяем ровно тот срез, что и загружаем (region='kz'), и один раз за прогон синка.
    warn_if_adgroups_stale(int(row_count or 0), newest)
    return out


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
