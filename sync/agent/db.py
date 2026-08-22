# -*- coding: utf-8 -*-
"""
sync/agent/db.py — схема и доступ к БД автопилота Директа.

Все таблицы автопилота имеют префикс edu_agent_ и не пересекаются с существующими.
DDL идемпотентен: ensure_agent_tables() безопасно вызывать на каждом прогоне
(тот же приём, что sync/db.py::ensure_schema).

Бюджет объёма — ориентир ~40 МБ на все таблицы Э0. Держится тремя решениями:
срезы недельные и в трёх двумерных разрезах (не декартово произведение);
структура и настройки версионируются по content_hash (новая строка только при
изменении); поисковые запросы — агрегат за окно без дат и только с кликами.
"""

import json
from datetime import date
from typing import Any, Dict, List, Optional

import psycopg2.extras

from sync.db import get_connection


def normalize_login(value: Any) -> str:
    """Логин кабинета в каноническом виде. Он же — ключ object_id таблиц агента.

    Нормализация обязана быть ОДНА на оба конца, поэтому она живёт здесь, рядом
    с таблицей, чей первичный ключ этот логин образует, а не дублируется в
    разборщиках DIRECT_CLIENTS_JSON расчёта и движка записи.

    Дефект, который она закрывает: расчёт (agent_e0) обрезал пробелы, движок
    записи (agent_e1) клал в список сырое значение. Пробел по краям любого
    логина в переменной окружения — и настройки записаны под "acc-1", а
    прочитаны под "acc-1 ": прогон молча рапортует, что применять нечего.
    Оба куска по отдельности корректны, вместе не сходятся.
    """
    return str(value or "").strip()


AGENT_DDL: List[str] = [
    # Ежедневный снимок фактов: грань campaign × date, как расход Директа.
    """
    CREATE TABLE IF NOT EXISTS edu_agent_facts (
      fact_date        DATE NOT NULL,
      campaign_id      TEXT NOT NULL,
      campaign_name    TEXT,
      project          TEXT,
      direction        TEXT,
      cost             DOUBLE PRECISION NOT NULL DEFAULT 0,
      clicks           INTEGER NOT NULL DEFAULT 0,
      impressions      INTEGER NOT NULL DEFAULT 0,
      leads            INTEGER NOT NULL DEFAULT 0,
      eff_leads        INTEGER NOT NULL DEFAULT 0,
      sum_p_pay        DOUBLE PRECISION NOT NULL DEFAULT 0,
      payments_fact    INTEGER NOT NULL DEFAULT 0,
      avg_impr_pos     DOUBLE PRECISION NOT NULL DEFAULT 0,
      auction_win_share DOUBLE PRECISION NOT NULL DEFAULT 0,
      connected_leads          INTEGER NOT NULL DEFAULT 0,
      deals                    INTEGER NOT NULL DEFAULT 0,
      mins_to_connection_sum   DOUBLE PRECISION NOT NULL DEFAULT 0,
      mins_to_connection_count INTEGER NOT NULL DEFAULT 0,
      days_to_pay_sum          DOUBLE PRECISION NOT NULL DEFAULT 0,
      days_to_pay_count        INTEGER NOT NULL DEFAULT 0,
      revenue                  DOUBLE PRECISION NOT NULL DEFAULT 0,
      collected_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
      PRIMARY KEY (fact_date, campaign_id)
    )
    """,
    # Срезы: НЕДЕЛЬНАЯ грань и три отдельных двумерных разреза вместо декартова
    # произведения. slice_kind ∈ region|network|device|gender|age|hour.
    # Хвост (мало кликов) сворачивается в slice_key='other' — иначе длинный хвост
    # регионов раздувает таблицу и ничего не добавляет: сжатие всё равно обнулит
    # корректировку на малом объёме.
    """
    CREATE TABLE IF NOT EXISTS edu_agent_facts_sliced (
      week_start    DATE NOT NULL,
      campaign_id   TEXT NOT NULL,
      slice_kind    TEXT NOT NULL,
      slice_key     TEXT NOT NULL,
      cost          DOUBLE PRECISION NOT NULL DEFAULT 0,
      clicks        INTEGER NOT NULL DEFAULT 0,
      impressions   INTEGER NOT NULL DEFAULT 0,
      conversions   INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY (week_start, campaign_id, slice_kind, slice_key)
    )
    """,
    # Структура кабинета: группы, фразы, объявления. Новая версия пишется ТОЛЬКО
    # при изменении содержимого (content_hash), а не ежедневно.
    """
    CREATE TABLE IF NOT EXISTS edu_agent_objects (
      object_level  TEXT NOT NULL,
      object_id     TEXT NOT NULL,
      parent_id     TEXT,
      campaign_id   TEXT NOT NULL,
      content_hash  TEXT NOT NULL,
      payload       JSONB NOT NULL DEFAULT '{}'::jsonb,
      first_seen    DATE NOT NULL,
      last_seen     DATE NOT NULL,
      PRIMARY KEY (object_level, object_id, content_hash)
    )
    """,
    # Поисковые запросы: агрегат за окно без дат, только с кликами.
    """
    CREATE TABLE IF NOT EXISTS edu_agent_search_queries (
      window_from   DATE NOT NULL,
      window_to     DATE NOT NULL,
      campaign_id   TEXT NOT NULL,
      query         TEXT NOT NULL,
      matched_key   TEXT,
      cost          DOUBLE PRECISION NOT NULL DEFAULT 0,
      clicks        INTEGER NOT NULL DEFAULT 0,
      conversions   INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY (window_from, campaign_id, query)
    )
    """,
    # Полный снимок настроек кампании. Версионируется хешем, как и структура.
    """
    CREATE TABLE IF NOT EXISTS edu_agent_settings_snapshot (
      campaign_id   TEXT NOT NULL,
      content_hash  TEXT NOT NULL,
      settings      JSONB NOT NULL DEFAULT '{}'::jsonb,
      first_seen    DATE NOT NULL,
      last_seen     DATE NOT NULL,
      PRIMARY KEY (campaign_id, content_hash)
    )
    """,
    # Поведение из Метрики по кампаниям: ранний сигнал качества трафика.
    # Суммы и счётчики, не проценты — среднее по среднему не складывается.
    """
    CREATE TABLE IF NOT EXISTS edu_agent_behavior (
      window_from   DATE NOT NULL,
      window_to     DATE NOT NULL,
      campaign_id   TEXT NOT NULL,
      visits        INTEGER NOT NULL DEFAULT 0,
      bounces       INTEGER NOT NULL DEFAULT 0,
      pageviews     INTEGER NOT NULL DEFAULT 0,
      visit_seconds BIGINT  NOT NULL DEFAULT 0,
      PRIMARY KEY (window_from, campaign_id)
    )
    """,
    # Журнал гейта качества данных: почему агент работал или спал.
    """
    CREATE TABLE IF NOT EXISTS edu_agent_guard (
      run_ts       TIMESTAMPTZ NOT NULL DEFAULT now(),
      check_name   TEXT NOT NULL,
      status       TEXT NOT NULL,
      detail       JSONB NOT NULL DEFAULT '{}'::jsonb,
      PRIMARY KEY (run_ts, check_name)
    )
    """,
    # Заповедник: кампании, которых агент не касается.
    """
    CREATE TABLE IF NOT EXISTS edu_agent_holdout (
      campaign_id   TEXT PRIMARY KEY,
      direction     TEXT,
      stratum       TEXT NOT NULL,
      included_at   DATE NOT NULL,
      excluded_at   DATE,
      reason        TEXT NOT NULL
    )
    """,
    # Блокнот: единственный носитель накопленного опыта.
    """
    CREATE TABLE IF NOT EXISTS edu_agent_experiments (
      experiment_id     TEXT PRIMARY KEY,
      hypothesis_type   TEXT NOT NULL,
      object_level      TEXT NOT NULL,
      object_id         TEXT NOT NULL,
      params            JSONB NOT NULL DEFAULT '{}'::jsonb,
      mechanism         TEXT NOT NULL,
      started_on        DATE,
      measured_on       DATE,
      effect            DOUBLE PRECISION,
      effect_lo         DOUBLE PRECISION,
      effect_hi         DOUBLE PRECISION,
      metric            TEXT NOT NULL,
      verdict           TEXT,
      reliability_class TEXT NOT NULL,
      source            TEXT NOT NULL,
      created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # Вычисляемые настройки: посчитаны на Э0, применяются в Э1.
    """
    CREATE TABLE IF NOT EXISTS edu_agent_computed_settings (
      calc_date     DATE NOT NULL,
      object_level  TEXT NOT NULL,
      object_id     TEXT NOT NULL,
      setting_kind  TEXT NOT NULL,
      setting_key   TEXT NOT NULL,
      value         DOUBLE PRECISION NOT NULL,
      support_n     INTEGER NOT NULL,
      raw_value     DOUBLE PRECISION NOT NULL,
      PRIMARY KEY (calc_date, object_level, object_id, setting_kind, setting_key)
    )
    """,
    # Профиль успеха и дистанция каждой кампании до него.
    """
    CREATE TABLE IF NOT EXISTS edu_agent_profile (
      calc_date    DATE NOT NULL,
      campaign_id  TEXT NOT NULL,
      distance     DOUBLE PRECISION NOT NULL,
      gaps         JSONB NOT NULL DEFAULT '[]'::jsonb,
      quartile     INTEGER NOT NULL,
      PRIMARY KEY (calc_date, campaign_id)
    )
    """,
]


def ensure_agent_tables() -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            for statement in AGENT_DDL:
                cur.execute(statement)
        conn.commit()


# ---------------------------------------------------------------- загрузчики


def _fetch_dicts(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


def load_direct_rows(date_from: str, date_to: str) -> List[Dict[str, Any]]:
    return _fetch_dicts(
        """
        SELECT date, campaign_id, campaign_name, project, direction,
               cost, clicks, impressions, w_avg_impr_pos, w_auction_win_share
        FROM direct_stats
        WHERE date BETWEEN %s AND %s
        """,
        (date_from, date_to),
    )


def load_lead_rows(date_from: str, date_to: str) -> List[Dict[str, Any]]:
    # revenue в crm_lead_details нет — суммы лежат в amount.
    return _fetch_dicts(
        """
        SELECT lead_id, campaign_id, created_date, is_eff, is_paid,
               is_connected, is_deal, created_ts, connected_ts, payment_date,
               amount AS revenue, direction, project, audience
        FROM crm_lead_details
        WHERE created_date BETWEEN %s AND %s
        """,
        (date_from, date_to),
    )


def load_score_rows(date_from: str, date_to: str) -> List[Dict[str, Any]]:
    return _fetch_dicts(
        """
        SELECT s.lead_id, s.scoring_point, s.p_pay
        FROM edu_lead_scores s
        JOIN crm_lead_details d ON d.lead_id = s.lead_id
        WHERE d.created_date BETWEEN %s AND %s
        """,
        (date_from, date_to),
    )


def load_campaign_settings_raw() -> Dict[str, Dict[str, Any]]:
    """Текущие настройки из витрины edu_campaign_settings (колонка settings, JSONB)."""
    rows = _fetch_dicts("SELECT campaign_id, settings FROM edu_campaign_settings")
    return {str(r["campaign_id"]): (r["settings"] or {}) for r in rows}


def load_campaign_features(date_from: str, date_to: str) -> List[Dict[str, Any]]:
    """Признаки кампаний для профиля успеха.

    Структурные признаки считаем из собственной таблицы edu_agent_objects, а не из
    JSONB настроек: там их формат не гарантирован, а объекты мы сняли сами.
    Берётся последняя версия каждого объекта (last_seen).
    """
    return _fetch_dicts(
        """
        WITH latest AS (
            SELECT DISTINCT ON (object_level, object_id)
                   object_level, object_id, campaign_id, payload
            FROM edu_agent_objects
            ORDER BY object_level, object_id, last_seen DESC
        ),
        structure AS (
            SELECT campaign_id,
                   COUNT(*) FILTER (WHERE object_level = 'adgroup')  AS groups_count,
                   COUNT(*) FILTER (WHERE object_level = 'keyword')  AS keywords_count,
                   COUNT(*) FILTER (WHERE object_level = 'ad')       AS ads_count,
                   COUNT(*) FILTER (
                       WHERE object_level = 'ad'
                         AND COALESCE(payload->'TextAd'->>'Title2', '') <> ''
                   ) AS ads_with_title2
            FROM latest
            GROUP BY campaign_id
        ),
        perf AS (
            SELECT campaign_id, SUM(cost) AS cost, SUM(sum_p_pay) AS sum_p_pay
            FROM edu_agent_facts
            WHERE fact_date BETWEEN %s AND %s
            GROUP BY campaign_id
        )
        SELECT p.campaign_id,
               p.cost,
               p.sum_p_pay,
               COALESCE(s.groups_count, 0) AS groups_count,
               CASE WHEN COALESCE(s.groups_count, 0) > 0
                    THEN s.keywords_count::float / s.groups_count
                    ELSE 0 END AS phrases_per_group,
               CASE WHEN COALESCE(s.ads_count, 0) > 0
                    THEN s.ads_with_title2::float / s.ads_count
                    ELSE 0 END AS title2_fill_share
        FROM perf p
        LEFT JOIN structure s ON s.campaign_id = p.campaign_id
        """,
        (date_from, date_to),
    )


def load_latest_computed_settings(
    object_id: str, object_level: str = "account",
) -> List[Dict[str, Any]]:
    """Последний расчёт вычисляемых настроек ОДНОГО кабинета.

    Фильтр по кабинету обязателен и не имеет значения по умолчанию: таблица
    копит расчёты всех кабинетов сразу, и выборка без фильтра раскатывала бы
    набор одного кабинета на кампании всех остальных — корректировка,
    посчитанная по аудитории одного клиента, уезжала бы чужому.

    MAX(calc_date) тоже считается ВНУТРИ кабинета: кабинеты считаются
    независимо, и один отставший на день кабинет не должен обнулять выборку
    остальных (и наоборот — свежий расчёт одного кабинета не должен прятать
    вчерашний расчёт другого).

    object_id нормализуется той же функцией, что и на записи
    (upsert_computed_settings): пробел по краям логина не должен превращать
    один кабинет в два разных текстовых ключа.

    calc_date возвращается в каждой строке и не является служебным полем:
    «последний расчёт» не значит «свежий». Если Э0 не запускался месяц,
    выборка честно отдаёт месячные коэффициенты, и без даты вызывающий код
    не может отличить их от вчерашних — он обязан ограничить возраст сам
    (sync/agent_e1.py::MAX_COMPUTED_AGE_DAYS).
    """
    object_id = normalize_login(object_id)
    return _fetch_dicts(
        """
        SELECT calc_date, setting_kind, setting_key, value, support_n, raw_value
        FROM edu_agent_computed_settings
        WHERE object_level = %s AND object_id = %s
          AND calc_date = (
              SELECT MAX(calc_date) FROM edu_agent_computed_settings
              WHERE object_level = %s AND object_id = %s
          )
        """,
        (object_level, object_id, object_level, object_id),
    )


def load_holdout_ids() -> List[str]:
    rows = _fetch_dicts(
        "SELECT campaign_id FROM edu_agent_holdout WHERE excluded_at IS NULL"
    )
    return [str(r["campaign_id"]) for r in rows]


def load_baseline_cpa(date_from: str, date_to: str) -> Dict[str, float]:
    """Базовый CPA кампании за окно — точка отсчёта для красной линии.

    Знаменатель — эффективные лиды: по ним ставится порог отката, потому что
    оплаты созревают дольше, чем длится наблюдение за изменением.
    """
    rows = _fetch_dicts(
        """
        SELECT campaign_id,
               SUM(cost) / NULLIF(SUM(eff_leads), 0) AS cpa
        FROM edu_agent_facts
        WHERE fact_date BETWEEN %s AND %s
        GROUP BY campaign_id
        HAVING SUM(eff_leads) > 0
        """,
        (date_from, date_to),
    )
    return {str(r["campaign_id"]): float(r["cpa"]) for r in rows}


def load_daily_facts(
    campaign_ids: List[str], date_from: str, date_to: str
) -> List[Dict[str, Any]]:
    """Дневные факты названных кампаний за окно — сырьё сторожа красных линий.

    Намеренно БЕЗ агрегата, в отличие от load_baseline_cpa: у каждого
    применённого действия своё окно наблюдения, отсчитанное от собственного
    момента применения, и свернуть их в один SUM по кампании нельзя — два
    действия по одной кампании, применённые в разные дни, судятся по разным
    отрезкам. Агрегат считает вызывающий код, по дням.

    Знаменатель наблюдения — eff_leads, тот же, что у базового CPA
    (load_baseline_cpa): красная линия сравнивает наблюдаемый CPA с базовым,
    и считать их по разным знаменателям значит сравнивать разные величины.
    """
    ids = sorted({str(c) for c in campaign_ids})
    if not ids:
        return []
    return _fetch_dicts(
        """
        SELECT campaign_id, fact_date, cost, eff_leads
        FROM edu_agent_facts
        WHERE campaign_id = ANY(%s) AND fact_date BETWEEN %s AND %s
        """,
        (ids, date_from, date_to),
    )


def mart_cost_total(date_from: str, date_to: str) -> float:
    """Суммарный расход, лежащий в витрине фактов за окно.

    Сверяется с суммой по источнику (гейт check_sum_reconciliation). Расхождение
    значит, что витрина собрана не из тех строк, которые сейчас отдаёт источник:
    часть кампаний потерялась при сборке, или прогон писал по другому окну.
    Свежесть и непрерывность такого не видят — даты у битой витрины в порядке.
    """
    rows = _fetch_dicts(
        "SELECT COALESCE(SUM(cost), 0) AS total FROM edu_agent_facts "
        "WHERE fact_date BETWEEN %s AND %s",
        (date_from, date_to),
    )
    return float(rows[0]["total"] or 0.0) if rows else 0.0


def crm_maturity_date() -> Optional[date]:
    """Последний день, за который лиды в CRM РЕАЛЬНО есть. Граница зрелости.

    CRM EDU штатно отстаёт на 2-4 дня, и отставание плавает. Ключевое свойство,
    измеренное 21.08.2026 (сравнение снимка edu_agent_facts с текущей CRM по
    одним и тем же дням): дни приходят ЦЕЛИКОМ и не дозаполняются — все 45 дней
    совпали точно, leads_added = 0 везде. То есть дня либо нет вовсе, либо он
    полон.

    Отсюда защита. Расход Директа приезжает вовремя, лиды — нет, и в витрине
    появляется день с почти миллионом расхода и нулём лидов (19.08.2026:
    927 945 рублей, 0 лидов). CPA такого дня бесконечен: попади он в окно
    наблюдения — красная линия пробита, и сторож откатит ЗДОРОВОЕ изменение.

    Раньше от этого защищал фиксированный отступ (LEADS_LAG_DAYS = 2, «заведомый
    запас»). Он переживает лаг в два-три дня и ломается на четырёх — то есть
    именно тогда, когда защита нужна. Константу заменяет факт: граница окна
    берётся из данных и двигается вместе с ними.

    None — лидов нет вовсе. Это не «граница сегодня», а «наблюдать не по чему»:
    подставлять здесь сегодняшний день значило бы разрешить вердикт по пустоте.
    """
    rows = _fetch_dicts("SELECT MAX(created_date) AS d FROM crm_lead_details")
    raw = rows[0]["d"] if rows else None
    if raw is None:
        return None
    return raw if isinstance(raw, date) else date.fromisoformat(str(raw))


def load_mart_day_breadth(date_from: str, date_to: str) -> Dict[str, Any]:
    """ШИРИНА ВИТРИНЫ по дням окна — по ВСЕЙ витрине, без фильтра по кампаниям.

    Отдаёт {"days": {дата: сколько кампаний наполнено}, "campaigns_total": N}.

    Зачем отдельный запрос, а не подсчёт по уже загруженным фактам. Факты
    сторож загружает только по кампаниям ОТКРЫТЫХ действий (load_daily_facts),
    и на тех же строках считались две вещи, которые к наблюдаемым кампаниям
    отношения не имеют: доля дней, за которые НАПОЛНЕНА ВИТРИНА, и ширина
    гейта свежести. При двух открытых действиях «витрина наполнена» незаметно
    превращалось в «была открутка у этих двух кампаний».

    Это тот же дефект, который уже чинили внутри сторожа (знаменатель
    покрытия перевели с дней кампании на дни витрины), вернувшийся через
    границу загрузки данных: кампанию, которую правка агента придушила,
    витрина теряет, покрытие падает, вердикта не будет — защита слабеет ровно
    там, где вред сильнее. И вероятнее всего это случится на первом боевом
    прогоне, когда открытых действий единицы.

    Один проход по витрине: GROUPING SETS даёт и строки по дням, и итоговую
    строку (fact_date = NULL) с числом РАЗНЫХ кампаний за всё окно. Считать
    итог максимумом по дням нельзя — кампании в разные дни разные.
    """
    rows = _fetch_dicts(
        """
        SELECT fact_date, COUNT(DISTINCT campaign_id) AS campaigns
        FROM edu_agent_facts
        WHERE fact_date BETWEEN %s AND %s
        GROUP BY GROUPING SETS ((fact_date), ())
        """,
        (date_from, date_to),
    )
    days: Dict[Any, int] = {}
    total = 0
    for row in rows:
        if row.get("fact_date") is None:
            total = int(row.get("campaigns") or 0)
            continue
        days[row["fact_date"]] = int(row.get("campaigns") or 0)
    return {"days": days, "campaigns_total": total}


def load_daily_cost_by_campaign(date_from: str, date_to: str) -> Dict[str, float]:
    """Средний дневной расход кампании за окно — множитель цены ошибки."""
    rows = _fetch_dicts(
        """
        SELECT campaign_id, AVG(cost) AS daily_cost
        FROM edu_agent_facts
        WHERE fact_date BETWEEN %s AND %s
        GROUP BY campaign_id
        HAVING AVG(cost) > 0
        """,
        (date_from, date_to),
    )
    return {str(r["campaign_id"]): float(r["daily_cost"]) for r in rows}


# ---------------------------------------------------------------- запись


def _batch(sql: str, rows: List[Dict[str, Any]], page_size: int = 500) -> int:
    if not rows:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, rows, page_size=page_size)
        conn.commit()
    return len(rows)


def upsert_facts(rows: List[Dict[str, Any]]) -> int:
    return _batch(
        """
        INSERT INTO edu_agent_facts (
            fact_date, campaign_id, campaign_name, project, direction,
            cost, clicks, impressions, leads, eff_leads, sum_p_pay,
            payments_fact, avg_impr_pos, auction_win_share,
            connected_leads, deals, mins_to_connection_sum, mins_to_connection_count,
            days_to_pay_sum, days_to_pay_count, revenue
        ) VALUES (
            %(fact_date)s, %(campaign_id)s, %(campaign_name)s, %(project)s, %(direction)s,
            %(cost)s, %(clicks)s, %(impressions)s, %(leads)s, %(eff_leads)s, %(sum_p_pay)s,
            %(payments_fact)s, %(avg_impr_pos)s, %(auction_win_share)s,
            %(connected_leads)s, %(deals)s, %(mins_to_connection_sum)s,
            %(mins_to_connection_count)s, %(days_to_pay_sum)s, %(days_to_pay_count)s,
            %(revenue)s
        )
        ON CONFLICT (fact_date, campaign_id) DO UPDATE SET
            campaign_name = EXCLUDED.campaign_name,
            project = EXCLUDED.project,
            direction = EXCLUDED.direction,
            cost = EXCLUDED.cost,
            clicks = EXCLUDED.clicks,
            impressions = EXCLUDED.impressions,
            leads = EXCLUDED.leads,
            eff_leads = EXCLUDED.eff_leads,
            sum_p_pay = EXCLUDED.sum_p_pay,
            payments_fact = EXCLUDED.payments_fact,
            avg_impr_pos = EXCLUDED.avg_impr_pos,
            auction_win_share = EXCLUDED.auction_win_share,
            connected_leads = EXCLUDED.connected_leads,
            deals = EXCLUDED.deals,
            mins_to_connection_sum = EXCLUDED.mins_to_connection_sum,
            mins_to_connection_count = EXCLUDED.mins_to_connection_count,
            days_to_pay_sum = EXCLUDED.days_to_pay_sum,
            days_to_pay_count = EXCLUDED.days_to_pay_count,
            revenue = EXCLUDED.revenue,
            collected_at = now()
        """,
        rows,
        page_size=1000,
    )


def upsert_sliced_facts(rows: List[Dict[str, Any]]) -> int:
    return _batch(
        """
        INSERT INTO edu_agent_facts_sliced (
            week_start, campaign_id, slice_kind, slice_key,
            cost, clicks, impressions, conversions
        ) VALUES (
            %(week_start)s, %(campaign_id)s, %(slice_kind)s, %(slice_key)s,
            %(cost)s, %(clicks)s, %(impressions)s, %(conversions)s
        )
        ON CONFLICT (week_start, campaign_id, slice_kind, slice_key) DO UPDATE SET
            cost = EXCLUDED.cost,
            clicks = EXCLUDED.clicks,
            impressions = EXCLUDED.impressions,
            conversions = EXCLUDED.conversions
        """,
        rows,
        page_size=1000,
    )


def upsert_objects(rows: List[Dict[str, Any]]) -> int:
    """Новая версия только при смене content_hash; иначе двигаем last_seen."""
    payload = [{**r, "payload": json.dumps(r.get("payload", {}), ensure_ascii=False)} for r in rows]
    return _batch(
        """
        INSERT INTO edu_agent_objects (
            object_level, object_id, parent_id, campaign_id, content_hash,
            payload, first_seen, last_seen
        ) VALUES (
            %(object_level)s, %(object_id)s, %(parent_id)s, %(campaign_id)s,
            %(content_hash)s, %(payload)s, %(first_seen)s, %(last_seen)s
        )
        ON CONFLICT (object_level, object_id, content_hash)
        DO UPDATE SET last_seen = EXCLUDED.last_seen
        """,
        payload,
        page_size=1000,
    )


def upsert_search_queries(rows: List[Dict[str, Any]]) -> int:
    return _batch(
        """
        INSERT INTO edu_agent_search_queries (
            window_from, window_to, campaign_id, query, matched_key,
            cost, clicks, conversions
        ) VALUES (
            %(window_from)s, %(window_to)s, %(campaign_id)s, %(query)s, %(matched_key)s,
            %(cost)s, %(clicks)s, %(conversions)s
        )
        ON CONFLICT (window_from, campaign_id, query) DO UPDATE SET
            cost = EXCLUDED.cost,
            clicks = EXCLUDED.clicks,
            conversions = EXCLUDED.conversions
        """,
        rows,
        page_size=1000,
    )


def upsert_settings_snapshot(rows: List[Dict[str, Any]]) -> int:
    payload = [{**r, "settings": json.dumps(r.get("settings", {}), ensure_ascii=False)} for r in rows]
    return _batch(
        """
        INSERT INTO edu_agent_settings_snapshot (
            campaign_id, content_hash, settings, first_seen, last_seen
        ) VALUES (
            %(campaign_id)s, %(content_hash)s, %(settings)s, %(first_seen)s, %(last_seen)s
        )
        ON CONFLICT (campaign_id, content_hash)
        DO UPDATE SET last_seen = EXCLUDED.last_seen
        """,
        payload,
    )


def clear_holdout() -> int:
    """Сброс заповедника. Только по явному флагу: состав держится весь сезон,
    случайная пересборка ломает базу сравнения для всех замеров."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM edu_agent_holdout WHERE excluded_at IS NULL")
            removed = cur.rowcount
        conn.commit()
    return removed


def upsert_holdout(rows: List[Dict[str, Any]], included_on: str) -> int:
    payload = [{**r, "included_at": included_on} for r in rows]
    return _batch(
        """
        INSERT INTO edu_agent_holdout (campaign_id, direction, stratum, included_at, reason)
        VALUES (%(campaign_id)s, %(direction)s, %(stratum)s, %(included_at)s, %(reason)s)
        ON CONFLICT (campaign_id) DO NOTHING
        """,
        payload,
    )


def upsert_experiments(rows: List[Dict[str, Any]]) -> int:
    payload = [{**r, "params": json.dumps(r.get("params", {}), ensure_ascii=False)} for r in rows]
    return _batch(
        """
        INSERT INTO edu_agent_experiments (
            experiment_id, hypothesis_type, object_level, object_id, params, mechanism,
            started_on, measured_on, effect, effect_lo, effect_hi, metric, verdict,
            reliability_class, source
        ) VALUES (
            %(experiment_id)s, %(hypothesis_type)s, %(object_level)s, %(object_id)s,
            %(params)s, %(mechanism)s, %(started_on)s, %(measured_on)s, %(effect)s,
            %(effect_lo)s, %(effect_hi)s, %(metric)s, %(verdict)s,
            %(reliability_class)s, %(source)s
        )
        ON CONFLICT (experiment_id) DO UPDATE SET
            effect = EXCLUDED.effect,
            effect_lo = EXCLUDED.effect_lo,
            effect_hi = EXCLUDED.effect_hi,
            verdict = EXCLUDED.verdict
        """,
        payload,
    )


def upsert_computed_settings(
    rows: List[Dict[str, Any]], calc_date: str, object_id: str,
    object_level: str = "account",
) -> int:
    """Вычисленные настройки ОДНОГО кабинета.

    object_id обязателен и без значения по умолчанию: первичный ключ включает
    (object_level, object_id), а расчёт идёт в цикле по кабинетам. С общим
    захардкоженным идентификатором строки четырёх кабинетов ложились в один и
    тот же ключ и тихо перетирали друг друга — в таблице выживали числа того
    кабинета, который дописался последним. Ошибки при этом не было: запись
    построчная, ON CONFLICT DO UPDATE отрабатывал штатно.

    object_id нормализуется ЗДЕСЬ, а не только у вызывающего: ключ пишется и
    читается через эту пару функций, и симметрия обязана держаться на границе
    таблицы, а не на дисциплине каждого разборщика конфигурации.
    """
    object_id = normalize_login(object_id)
    payload = [{**r, "calc_date": calc_date, "object_level": object_level, "object_id": object_id}
               for r in rows]
    return _batch(
        """
        INSERT INTO edu_agent_computed_settings (
            calc_date, object_level, object_id, setting_kind, setting_key,
            value, support_n, raw_value
        ) VALUES (
            %(calc_date)s, %(object_level)s, %(object_id)s, %(setting_kind)s,
            %(setting_key)s, %(value)s, %(support_n)s, %(raw_value)s
        )
        ON CONFLICT (calc_date, object_level, object_id, setting_kind, setting_key)
        DO UPDATE SET value = EXCLUDED.value,
                      support_n = EXCLUDED.support_n,
                      raw_value = EXCLUDED.raw_value
        """,
        payload,
    )


def upsert_profile(rows: List[Dict[str, Any]], calc_date: str) -> int:
    payload = [{**r, "calc_date": calc_date, "gaps": json.dumps(r.get("gaps", []), ensure_ascii=False)}
               for r in rows]
    return _batch(
        """
        INSERT INTO edu_agent_profile (calc_date, campaign_id, distance, gaps, quartile)
        VALUES (%(calc_date)s, %(campaign_id)s, %(distance)s, %(gaps)s, %(quartile)s)
        ON CONFLICT (calc_date, campaign_id) DO UPDATE SET
            distance = EXCLUDED.distance,
            gaps = EXCLUDED.gaps,
            quartile = EXCLUDED.quartile
        """,
        payload,
    )


def upsert_behavior(rows: List[Dict[str, Any]], window_from: str, window_to: str) -> int:
    payload = [{**r, "window_from": window_from, "window_to": window_to} for r in rows]
    return _batch(
        """
        INSERT INTO edu_agent_behavior (
            window_from, window_to, campaign_id, visits, bounces, pageviews, visit_seconds
        ) VALUES (
            %(window_from)s, %(window_to)s, %(campaign_id)s, %(visits)s,
            %(bounces)s, %(pageviews)s, %(visit_seconds)s
        )
        ON CONFLICT (window_from, campaign_id) DO UPDATE SET
            visits = EXCLUDED.visits,
            bounces = EXCLUDED.bounces,
            pageviews = EXCLUDED.pageviews,
            visit_seconds = EXCLUDED.visit_seconds
        """,
        payload,
    )


def clear_bulk_tables() -> Dict[str, int]:
    """Сброс объёмных витрин перед пересбором.

    Нужен, когда меняются правила отбора: старые строки не перезаписываются
    upsert-ом и остаются мёртвым грузом. На прогоне 31785888375 объекты и
    поисковые запросы заняли 828 МБ из 838 — при квоте Supabase это риск,
    а история проекта уже знает инцидент с исчерпанием диска.
    """
    removed: Dict[str, int] = {}
    tables = ("edu_agent_search_queries", "edu_agent_objects")
    with get_connection() as conn:
        with conn.cursor() as cur:
            for table in tables:
                cur.execute(f"DELETE FROM {table} WHERE true")
                removed[table] = cur.rowcount
        conn.commit()

    # DELETE не возвращает место на диск: мёртвые строки лежат в файлах до VACUUM FULL.
    # Без этого после чистки 1,2 млн строк таблица продолжала занимать сотни мегабайт.
    # VACUUM нельзя выполнять внутри транзакции — нужен autocommit.
    with get_connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for table in tables:
                cur.execute(f"VACUUM FULL {table}")
        conn.autocommit = False
    return removed


def insert_guard_checks(checks: List[Dict[str, Any]]) -> int:
    payload = [{**c, "detail": json.dumps(c.get("detail", {}), ensure_ascii=False)} for c in checks]
    return _batch(
        """
        INSERT INTO edu_agent_guard (check_name, status, detail)
        VALUES (%(check_name)s, %(status)s, %(detail)s)
        ON CONFLICT DO NOTHING
        """,
        payload,
    )


def table_sizes() -> List[Dict[str, Any]]:
    """Фактический объём таблиц агента — сверка с бюджетом ~40 МБ."""
    return _fetch_dicts(
        """
        SELECT c.relname AS table_name,
               pg_size_pretty(pg_total_relation_size(c.oid)) AS size,
               pg_total_relation_size(c.oid) AS size_bytes,
               COALESCE(s.n_live_tup, 0) AS approx_rows
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
        WHERE c.relname LIKE 'edu_agent_%%' AND c.relkind = 'r'
        ORDER BY pg_total_relation_size(c.oid) DESC
        """
    )
