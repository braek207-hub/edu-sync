# -*- coding: utf-8 -*-
"""sync/lime_kz_metrika.py — KZ-срез Яндекс.Метрики → lime_stats (region='kz_metrika').

Временная параллельная поверхность рядом с неполными данными витрины MySQL (region='kz'):
на сверке 11–17.07.2026 MySQL недосчитывал 37% заказов. Спека:
docs/superpowers/specs/2026-07-18-lime-kz-metrika-design.md (в репо приложения).

Владение срезом: только этот синк удаляет и пишет region='kz_metrika'; sync/lime.py и
sync/lime_gcc.py его не трогают — три ingest'а в одну таблицу не затирают друг друга.

Выручка Метрики по KZ-витрине приходит в тенге (сверка среднего чека: 29 067 против 4 721 ₽
в MySQL = ×6.16 ≈ курс) → конвертируем по курсу ЦБ на дату строки.

ENV: DATABASE_URL, LIME_METRIKA_TOKEN, LIME_METRIKA_COUNTER_ID (default 23504302),
LIME_KZ_METRIKA_DAYS_BACK (default 30), LIME_KZ_METRIKA_FROM/LIME_KZ_METRIKA_TO (бэкфилл),
LIME_KZ_METRIKA_DRY_RUN (не писать в БД, только сводка).
Запуск: python -m sync.lime_kz_metrika
"""
import os
from datetime import date, timedelta

import psycopg2
import psycopg2.extras

from sync.fx import to_rub as fx_to_rub
from sync.lime_kz_campaigns import (
    load_cost_map,
    load_direct_map,
    load_entity_map,
    load_google_map,
    resolve_campaign,
)
from sync.lime_kz_metrika_api import fetch_kz_traffic
from sync.metrika_channels import map_metrika_channel

REGION = "kz_metrika"
COUNTER_ID = os.environ.get("LIME_METRIKA_COUNTER_ID") or "23504302"
DAYS_BACK = int(os.environ.get("LIME_KZ_METRIKA_DAYS_BACK") or "30")

COLUMNS = (
    "date", "data_source", "region", "channel", "subchannel", "traffic_type",
    "campaign_id", "campaign_name",
    "cost", "clicks", "impressions", "sessions", "users", "clients",
    "purchases_count", "purchases_revenue", "customers",
    "new_users", "new_customers", "new_customers_revenue",
    "bounce_rate", "page_depth", "cart_reaches", "checkout_reaches",
)

INSERT_SQL = f"INSERT INTO lime_stats ({', '.join(COLUMNS)}) VALUES %s"
DELETE_SQL = f"DELETE FROM lime_stats WHERE region = '{REGION}' AND date >= %s AND date <= %s"


def build_rows(metrika_rows, campaign_maps, cost_map, fx_rate: float, date_s: str) -> list[tuple]:
    """Свернуть строки Метрики в кортежи lime_stats порядка COLUMNS.

    Ключ свёртки — (channel, subchannel, campaign_id). Нераспознанные кампании дают
    campaign_id='' и схлопываются до уровня канала. Расход берётся из cost_map один раз
    на кампанию за день (её визиты могут прийти несколькими строками каналов).

    Args:
        metrika_rows: строки sync.lime_kz_metrika_api.parse_metrika_kz за date_s.
        campaign_maps: кортеж (direct_map, google_map, adgroup_map) из sync.lime_kz_campaigns.
        cost_map: (дата, campaign_id) → расход в рублях (только KZ-кабинеты).
        fx_rate: курс тенге к рублю на date_s.
        date_s: дата строк YYYY-MM-DD.

    Returns:
        Список кортежей в порядке COLUMNS.
    """
    direct_map, google_map, adgroup_map = campaign_maps
    agg: dict[tuple[str, str, str], dict] = {}

    for m in metrika_rows:
        channel, subchannel, traffic_type = map_metrika_channel(
            m.get("traffic_source"), m.get("source_engine")
        )
        ref = resolve_campaign(m, direct_map, google_map, adgroup_map)
        key = (channel, subchannel, ref.campaign_id)
        acc = agg.get(key)
        if acc is None:
            acc = {
                "traffic_type": traffic_type,
                "campaign_name": ref.campaign_name,
                "kz_cabinet": ref.kz_cabinet,
                "visits": 0.0, "users": 0.0, "new_users": 0.0,
                "bounce_w": 0.0, "depth_w": 0.0,
                "cart": 0.0, "checkout": 0.0,
                "orders": 0.0, "revenue": 0.0,
            }
            agg[key] = acc

        visits = float(m.get("visits") or 0)
        acc["visits"] += visits
        acc["users"] += float(m.get("users") or 0)
        acc["new_users"] += float(m.get("new_users") or 0)
        acc["bounce_w"] += float(m.get("bounce_rate") or 0) * visits
        acc["depth_w"] += float(m.get("page_depth") or 0) * visits
        acc["cart"] += float(m.get("cart_reaches") or 0)
        acc["checkout"] += float(m.get("checkout_reaches") or 0)
        acc["orders"] += float(m.get("orders") or 0)
        acc["revenue"] += float(m.get("revenue") or 0)

    # Расход — один раз на кампанию за день, даже если её визиты разложились по каналам.
    spent: set[str] = set()
    out: list[tuple] = []
    for (channel, subchannel, campaign_id), acc in agg.items():
        cost = 0.0
        if campaign_id and acc["kz_cabinet"] and campaign_id not in spent:
            cost = float(cost_map.get((date_s, campaign_id), 0.0))
            spent.add(campaign_id)

        visits = acc["visits"]
        out.append((
            date_s, "web", REGION, channel, subchannel, acc["traffic_type"],
            campaign_id, acc["campaign_name"],
            round(cost, 2), 0.0, 0.0, int(visits), int(acc["users"]), 0,
            int(acc["orders"]), round(acc["revenue"] * fx_rate, 2), 0,
            int(acc["new_users"]), 0, 0.0,
            round(acc["bounce_w"] / visits, 2) if visits else 0.0,
            round(acc["depth_w"] / visits, 2) if visits else 0.0,
            int(acc["cart"]), int(acc["checkout"]),
        ))

    # Защита от тихого нуля по расходу Google: расход казахстанских Google-кампаний
    # приходит НЕ из этого синка, а из cost_map (sync/lime_kz_campaigns.load_cost_map ←
    # lime_google_ads_stats.cost_rub, который считает отдельный синк google_ads_fx по курсу
    # ЦБ). Если тот синк отвалится или не досчитает курс за день, cost_rub останется NULL,
    # строка расхода выпадет из cost_map, и расход конкретной google-кампании молча станет
    # 0 — при этом визиты и заказы по ней в срезе останутся. Google — крупнейший канал KZ,
    # такой 0 не бросается в глаза на дашборде (не отличить от «сегодня правда 0 расхода»).
    # Не бросаем исключение: сам трафик Метрики собран корректно, деньги можно долить
    # бэкфиллом позже, когда fx-синк починят — падать из-за чужого синка не нужно. Только
    # громкое предупреждение, чтобы кто-то заметил разрыв и перезапустил google_ads_fx.
    # Считаем уникальные campaign_id, а не строки-группы свёртки: одна кампания, чьи визиты
    # разложились по нескольким (channel, subchannel), иначе задвоила бы число в предупреждении.
    zero_cost_google_campaigns = {
        campaign_id
        for (channel, subchannel, campaign_id), acc in agg.items()
        if campaign_id
        and acc["kz_cabinet"]
        and channel == "SEM"
        and "google" in subchannel.lower()
        and float(cost_map.get((date_s, campaign_id), 0.0)) == 0.0
    }
    if zero_cost_google_campaigns:
        print(f"lime_kz_metrika: WARN {date_s} — {len(zero_cost_google_campaigns)} кампани(я/й) "
              f"платного Google с нулевым расходом (проверь синк google_ads_fx / lime_google_ads_stats.cost_rub)")

    # Общий признак ВСЕХ отказов склейки: платный визит, чью кампанию распознать не удалось.
    # Гейт выше требует непустой campaign_id и потому ловит лишь один сценарий (кампания
    # известна, расход не доехал). Реальные отказы соседей дают как раз ПУСТОЙ campaign_id
    # и проходили молча:
    #   • протух справочник lime_google_ads_entities (его пишет скрипт в кабинете Google Ads —
    #     вне workflow и без расписания) → id сущности не резолвится → расход Google = 0;
    #   • имя кампании Директа стало неоднозначным (кампанию продублировали в кабинете с тем же
    #     именем) → load_direct_map кладёт None → строка нераспознана → расход этой кампании
    #     исчезает целиком, и предупреждения про Директ не было вообще;
    #   • пустая статистика Google за дату → google_map пуст → PMax-визиты не резолвятся.
    # Во всех трёх случаях визиты и заказы на месте, а расход падает — ДРР, CPO и окупаемость
    # выглядят ЛУЧШЕ реальности. Такую ошибку нельзя оставлять тихой.
    # Не падаем: сам трафик Метрики собран корректно, деньги доливаются бэкфиллом после починки
    # соседа. Считаем строки свёртки (а не уникальные кампании): кампания здесь неизвестна
    # по определению, поэтому единица счёта — группа (channel, subchannel).
    unresolved_paid_rows = 0
    unresolved_paid_visits = 0.0
    for (channel, subchannel, campaign_id), acc in agg.items():
        if campaign_id or acc["traffic_type"] != "Платный":
            continue
        unresolved_paid_rows += 1
        unresolved_paid_visits += acc["visits"]
    if unresolved_paid_rows:
        print(f"lime_kz_metrika: WARN {date_s} — {unresolved_paid_rows} строк(и) платного трафика "
              f"без распознанной кампании ({int(unresolved_paid_visits)} визитов): расход по ним "
              f"не проставлен (проверь справочник lime_google_ads_entities, "
              f"неоднозначные имена кампаний Директа и статистику Google Ads за дату)")

    return out


def _sync_range(frm: date, to: date, conn) -> int:
    token = os.environ["LIME_METRIKA_TOKEN"]
    frm_s, to_s = frm.isoformat(), to.isoformat()

    if conn is not None:
        campaign_maps = (
            load_direct_map(conn, frm_s, to_s),
            load_google_map(conn, frm_s, to_s),
            load_entity_map(conn),
        )
        cost_map = load_cost_map(conn, frm_s, to_s)
    else:
        campaign_maps, cost_map = ({}, {}, {}), {}

    total = 0
    day = frm
    while day <= to:
        day_s = day.isoformat()
        metrika = fetch_kz_traffic(COUNTER_ID, token, day_s, day_s)
        fx_rate = fx_to_rub("KZT", day_s)
        rows = build_rows(metrika, campaign_maps, cost_map, fx_rate, day_s)

        if conn is None:
            i_sessions, i_orders = COLUMNS.index("sessions"), COLUMNS.index("purchases_count")
            i_revenue, i_cost = COLUMNS.index("purchases_revenue"), COLUMNS.index("cost")
            # Без conn campaign_maps/cost_map пусты (см. ветку выше) — ни одна кампания не
            # резолвится и расход всегда 0, даже если он реально есть. Не путать с «расхода нет».
            print(f"lime_kz_metrika: [DRY-RUN, БЕЗ БД: кампании не резолвятся, расход не считается] "
                  f"{day_s} → {len(rows)} строк "
                  f"(визиты={sum(r[i_sessions] for r in rows)}, "
                  f"заказы={sum(r[i_orders] for r in rows)}, "
                  f"выручка={sum(r[i_revenue] for r in rows):.0f}₽, "
                  f"расход={sum(r[i_cost] for r in rows):.0f}₽)")
        else:
            with conn.cursor() as cur:
                cur.execute(DELETE_SQL, (day_s, day_s))
                if rows:
                    psycopg2.extras.execute_values(cur, INSERT_SQL, rows, page_size=500)
            conn.commit()
            print(f"lime_kz_metrika: {day_s} → {len(rows)} строк")

        total += len(rows)
        day += timedelta(days=1)
    return total


def sync_lime_kz_metrika() -> int:
    frm_env = os.environ.get("LIME_KZ_METRIKA_FROM")
    to_env = os.environ.get("LIME_KZ_METRIKA_TO")
    if frm_env and to_env:
        frm, to = date.fromisoformat(frm_env), date.fromisoformat(to_env)
    else:
        to = date.today()
        frm = to - timedelta(days=DAYS_BACK - 1)

    if os.environ.get("LIME_KZ_METRIKA_DRY_RUN") or not os.environ.get("DATABASE_URL"):
        return _sync_range(frm, to, None)

    conn = psycopg2.connect(os.environ["DATABASE_URL"].split("?")[0], connect_timeout=30)
    try:
        return _sync_range(frm, to, conn)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    sync_lime_kz_metrika()
