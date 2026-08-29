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
from collections import Counter, defaultdict
from datetime import date
from statistics import median
from typing import Any, Dict, List, Optional

import psycopg2.extras

from sync.db import enable_rls_for_ddl, get_connection


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
      conversions              INTEGER NOT NULL DEFAULT 0,
      collected_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
      PRIMARY KEY (fact_date, campaign_id)
    )
    """,
    # Конверсии ЦЕЛЕЙ Директа — отдельно от лидов CRM. Цель CPA в кабинете
    # назначается за конверсию цели (страница «Спасибо»), а не за
    # эффективный лид: рычаг целевого CPA (Э3.5) без этого счётчика не
    # построить. Отдельным ALTER — таблица давно создана в бою.
    """
    ALTER TABLE edu_agent_facts
      ADD COLUMN IF NOT EXISTS conversions INTEGER NOT NULL DEFAULT 0
    """,
    # Средний объём трафика Директа (0–100) — мера того, сколько показов
    # кампания недобирает на своей ставке. Поля «доля выкупа» в Reports API
    # нет (probe_traffic_headroom, docs/AGENT-DATA-SOURCES.md), а колонка
    # auction_win_share наполнялась только выгрузкой интерфейса времён GAS.
    """
    ALTER TABLE edu_agent_facts
      ADD COLUMN IF NOT EXISTS avg_traffic_vol DOUBLE PRECISION NOT NULL DEFAULT 0
    """,
    # Лиды, у которых скор оплаты вообще есть, — знаменатель среднего p_pay
    # (sync/agent/quality.py). Скоринг требует client_id, проставленного не на
    # всех лендах, поэтому eff_leads в этой роли мерил бы покрытие скорингом,
    # а не качество когорты. Отдельным ALTER — таблица давно создана в бою.
    """
    ALTER TABLE edu_agent_facts
      ADD COLUMN IF NOT EXISTS scored_leads INTEGER NOT NULL DEFAULT 0
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
    # Читаемое имя ключа. Ключ региона — числовой RegionId (его требует
    # RegionalAdjustment), и без имени строка «213» не читается ни в идее, ни
    # в разборе прогона. Не в ключ: имя региона в справочнике Директа может
    # смениться, и тогда одна и та же область разъехалась бы на две строки.
    """
    ALTER TABLE edu_agent_facts_sliced
      ADD COLUMN IF NOT EXISTS slice_label TEXT
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
    # РЕЕСТР ГИПОТЕЗ поверх того же блокнота (sync/agent/experiments.py).
    # Отдельным ALTER, а не правкой CREATE TABLE выше: таблица давно живёт в
    # бою (194 квазиэксперимента), и CREATE TABLE IF NOT EXISTS её не трогает —
    # новая колонка в теле CREATE появилась бы только на чистой базе.
    #
    # До этих колонок строка была ПОСМЕРТНОЙ: сторож писал в неё исход уже
    # случившегося действия. Ставка как замысел не существовала нигде, и связь
    # «гипотеза → действие → замер → вердикт» негде было держать.
    #
    #   status            — жизненный цикл ставки (queued/running/won/lost/
    #                       rolled_back). NULL — строка НЕ реестра: посмертная
    #                       запись сторожа (source='action') или квазиэксперимент
    #                       истории (source='quasi'). Значения по умолчанию нет
    #                       намеренно: 'queued' на старых строках объявил бы
    #                       194 давно закрытых наблюдения открытыми ставками.
    #   status_reason     — почему статус такой; читается на разборе вместо
    #                       восстановления причины по датам.
    #   stake_rub         — СПИСАННАЯ цена ставки (writer/risk.fit_into_budget).
    #                       Не новый бюджет, а запись о том, что уже списал
    #                       недельный риск-бюджет.
    #   stake_source      — каким карманом оплачена. Колонка нужна ровно затем,
    #                       чтобы появление второго источника денег было видно
    #                       в данных, а не только в диффе.
    #   horizon_days      — горизонт замера, назначенный В МОМЕНТ ЗАПУСКА.
    #   success_criterion — критерий успеха словами, тоже назначенный заранее:
    #                       вердикт обязан сверяться с тем, о чём договорились,
    #                       а не с тем, что удобно объявить успехом задним числом.
    #   red_line          — красная линия КОПИЕЙ. Ссылка на журнал действий не
    #                       годится: перепланировка ещё не применённого действия
    #                       переписывает его red_line (writer/db.INSERT_ACTION_SQL),
    #                       а ставка обязана помнить линию своего запуска.
    #   idempotency_key   — связь с edu_agent_actions: по нему подтягивается
    #                       состояние действия (применено, откатано, закрыто).
    #   closed_at         — момент закрытия реестром. Не дубль measured_on: там
    #                       конец окна наблюдения (дата, считает сторож), здесь
    #                       отметка о смене статуса.
    """
    ALTER TABLE edu_agent_experiments
      ADD COLUMN IF NOT EXISTS status            TEXT,
      ADD COLUMN IF NOT EXISTS status_reason     TEXT,
      ADD COLUMN IF NOT EXISTS stake_rub         DOUBLE PRECISION,
      ADD COLUMN IF NOT EXISTS stake_source      TEXT,
      ADD COLUMN IF NOT EXISTS horizon_days      INTEGER,
      ADD COLUMN IF NOT EXISTS success_criterion TEXT,
      ADD COLUMN IF NOT EXISTS red_line          JSONB NOT NULL DEFAULT '{}'::jsonb,
      ADD COLUMN IF NOT EXISTS idempotency_key   TEXT,
      ADD COLUMN IF NOT EXISTS closed_at         TIMESTAMPTZ
    """,
    # Открытые ставки читаются КАЖДЫМ прогоном сторожа, а закрытые копятся без
    # предела — частичный индекс держит выборку размером с очередь, а не с
    # историей. Тот же довод, что у индексов кулдауна в writer/db.py.
    """
    CREATE INDEX IF NOT EXISTS edu_agent_experiments_open_idx
      ON edu_agent_experiments (status)
      WHERE status IN ('queued', 'running')
    """,
    # Подтягивание состояния действия идёт по ключу идемпотентности, и он же
    # обязан быть уникальным среди ставок: две ставки на одно действие — это
    # двойной счёт замера. У посмертных строк ключа нет (NULL), и уникальный
    # индекс их не ограничивает — NULL в Postgres не конфликтует с NULL.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS edu_agent_experiments_idem_idx
      ON edu_agent_experiments (idempotency_key)
      WHERE idempotency_key IS NOT NULL
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
      rel_error     DOUBLE PRECISION,
      PRIMARY KEY (calc_date, object_level, object_id, setting_kind, setting_key)
    )
    """,
    # Прод-таблица создана до Э2.3 — колонка ошибки добавляется идемпотентно.
    # NULL = «ошибка неизвестна» (строки старого формата); слой уверенности
    # обязан отличать это от «ошибка мала».
    """
    ALTER TABLE edu_agent_computed_settings
      ADD COLUMN IF NOT EXISTS rel_error DOUBLE PRECISION
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
    # РЕЕСТР ИДЕЙ (sync/agent/ideas/registry.py). Идея жила внутри такта и
    # умирала вместе с ним: генератор детерминирован, назавтра он находит ровно
    # то же самое — и человек читает тот же список второй раз, третий, десятый.
    # Реестр даёт идее срок жизни, историю и приоритет.
    #
    # Три колонки сверх схемы плана, и все три — про одно и то же «нет»:
    #
    #   subject_key — отпечаток ОБЪЕКТА идеи (registry.subject_key), без
    #                 источника. Отклонение человеком ищется по нему, а не по
    #                 idea_id: идентификатор строки выведен из пары
    #                 (source, subject), и та же связка, найденная завтра
    #                 другим генератором, приехала бы под новым id и обошла
    #                 запрет. Отдельной таблицы отказов нет намеренно —
    #                 второе хранилище того же факта разъезжается с первым.
    #   rejected_by — КТО отклонил. Это и есть машинный признак «сказал
    #                 человек»: разбирать префикс dropped_reason (свободный
    #                 текст для отчёта) значило бы держать правило в строке,
    #                 которую однажды перепишут ради формулировки.
    #   rejected_at — когда. Нужен на разборе: отказ полугодовой давности —
    #                 повод спросить человека заново, свежий — нет.
    """
    CREATE TABLE IF NOT EXISTS edu_agent_ideas (
      idea_id        TEXT PRIMARY KEY,
      created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
      source         TEXT NOT NULL,
      account        TEXT NOT NULL,
      subject        JSONB NOT NULL,
      subject_key    TEXT NOT NULL,
      tier           SMALLINT NOT NULL,
      lane           TEXT NOT NULL,
      expected_rub   DOUBLE PRECISION,
      test_cost_rub  DOUBLE PRECISION,
      horizon_days   SMALLINT NOT NULL,
      success_rule   JSONB NOT NULL,
      status         TEXT NOT NULL DEFAULT 'new',
      action_id      TEXT,
      experiment_id  TEXT,
      dropped_reason TEXT,
      rejected_by    TEXT,
      rejected_at    TIMESTAMPTZ
    )
    """,
    # Полезная нагрузка рычага. Отдельным оператором, а не строкой в теле
    # CREATE TABLE выше: таблица уже создана в бою, и правка тела её не
    # догонит — CREATE TABLE IF NOT EXISTS промолчит, а первый же прогон
    # упадёт на неизвестной колонке (тот же приём, что у поздних колонок
    # edu_agent_experiments и edu_agent_computed_settings).
    #
    # Зачем колонка вообще. Расчётный такт (agent_e0) и такт записи
    # (agent_e1) — РАЗНЫЕ прогоны: первый заводит идею, второй читает её из
    # базы (registry.open_ideas). Всё, чего нет в колонке, для такта записи
    # не существует — и до этой правки нагрузка терялась на проекции идеи
    # на COLUMNS, а каждая идея получала назавтра отказ «применять нечем».
    #
    # NULL здесь законен и означает «рычага нет»: у предложения (класс 3)
    # его не бывает по определению, и требовать с него нагрузку значило бы
    # не пускать в реестр ровно те идеи, ради экрана которых он и заведён.
    """
    ALTER TABLE edu_agent_ideas
      ADD COLUMN IF NOT EXISTS action JSONB
    """,
    # Доказательства идеи: чем генератор её обосновал — список связок-доноров,
    # план кросс-минусовки, замеренные числа. Отдельно от subject, и это
    # принципиально: subject — АДРЕС, из него выведен idea_id, и любое число
    # внутри заводило бы идею заново каждым прогоном, а вместе с ней теряло
    # бы отказ человека (он помнится по отпечатку subject).
    #
    # Доказательства, наоборот, обязаны обновляться каждым прогоном: состав
    # связок-доноров плавает, и заморозь его — экран показывал бы человеку
    # обоснование недельной давности. Поэтому detail входит в GENERATOR_FIELDS,
    # но не входит в идентичность.
    #
    # Отдельным ALTER, а не правкой CREATE TABLE выше: таблица заведена в бою
    # 27.08, и новая колонка в теле CREATE появилась бы только на чистой базе.
    """
    ALTER TABLE edu_agent_ideas
      ADD COLUMN IF NOT EXISTS detail JSONB
    """,
    # Кто из людей взял идею в работу и когда (registry.take_into_work).
    # Отдельно от rejected_by, потому что это противоположное решение, и
    # отдельно от статуса, потому что queued ставит и такт записи: без имени
    # взятая человеком идея неотличима от идеи, поставленной в очередь
    # машиной, — а ждать их исполнения надо от разных исполнителей.
    """
    ALTER TABLE edu_agent_ideas
      ADD COLUMN IF NOT EXISTS queued_by TEXT
    """,
    """
    ALTER TABLE edu_agent_ideas
      ADD COLUMN IF NOT EXISTS queued_at TIMESTAMPTZ
    """,
    # Отклонения человеком читаются КАЖДЫМ прогоном генераторов и на каждую
    # порцию идей, а копятся без предела — частичный индекс держит выборку
    # размером с список запретов, а не с историей реестра (тот же довод, что у
    # индекса открытых ставок выше).
    """
    CREATE INDEX IF NOT EXISTS edu_agent_ideas_rejected_idx
      ON edu_agent_ideas (subject_key)
      WHERE rejected_by IS NOT NULL
    """,
    # Очередь нарядов билдеру (sync/agent/build_queue.py,
    # docs/BUILD-ORDER-QUEUE.md). Единственная таблица агента, которую читает
    # ЧУЖОЙ репозиторий: тело кампании собирает «EDU кампании», и
    # campaign.create из edu-sync невыразим в принципе.
    #
    #   order_id      — ключ. БЕЗ ДАТЫ (build_order.make_order_id): у реестра
    #                   ровно одна открытая идея на (кабинет, вид,
    #                   направление), и дата в ключе заводила бы новую
    #                   кампанию каждым прогоном генератора, оставляя прежнюю
    #                   тратить.
    #   order_json    — ПРОВЕРЕННОЕ тело наряда. Билдер собирает ровно то, что
    #                   прошло build_order.validate: разъедься эти два текста,
    #                   проверка была бы о другом наряде.
    #   status        — queued → taken → built | failed, плюс cancelled.
    #                   Тело обновляется только в queued: собирать движущуюся
    #                   цель нельзя.
    #   campaign_id   — адрес созданного объекта. До ответа билдера его не
    #                   существует: Id появляется только после campaigns.add,
    #                   и без него агенту нечего ни наблюдать, ни откатывать.
    #   started_on    — день ПЕРВОЙ ОТКРУТКИ, не создания. Кампания создаётся
    #                   на паузе, между созданием и включением стоит человек,
    #                   и окно наблюдения от дня создания съело бы горизонт
    #                   днями простоя. NULL законен и означает «ещё не
    #                   включена».
    #   experiment_id — наблюдение в edu_agent_experiments. Заводится только
    #                   вместе с датой старта: строка реестра без дня открутки
    #                   обещала бы вердикт по пустому окну.
    """
    CREATE TABLE IF NOT EXISTS edu_agent_build_orders (
      order_id      TEXT PRIMARY KEY,
      idea_id       TEXT,
      account       TEXT NOT NULL,
      kind          TEXT NOT NULL,
      level_slug    TEXT NOT NULL,
      campaign_name TEXT NOT NULL,
      direction     TEXT,
      status        TEXT NOT NULL DEFAULT 'queued',
      status_reason TEXT,
      order_json    JSONB NOT NULL,
      campaign_id   TEXT,
      started_on    DATE,
      experiment_id TEXT,
      queued_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # Открытые наряды читает КАЖДЫЙ прогон билдера, а закрытые копятся без
    # предела: частичный индекс держит выборку размером с очередь, а не с
    # историей. Тот же довод, что у индекса открытых ставок.
    """
    CREATE INDEX IF NOT EXISTS edu_agent_build_orders_open_idx
      ON edu_agent_build_orders (status)
      WHERE status IN ('queued', 'taken')
    """,
    # Две кампании на один наряд — двойной расход и общий аукцион. Id
    # приходит только с ответом билдера, поэтому индекс частичный: у
    # неотвеченных нарядов колонка пуста, и уникальность их не касается.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS edu_agent_build_orders_campaign_idx
      ON edu_agent_build_orders (campaign_id)
      WHERE campaign_id IS NOT NULL
    """,
]


def ensure_agent_tables() -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            for statement in AGENT_DDL:
                cur.execute(statement)
            enable_rls_for_ddl(cur, AGENT_DDL)
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
               cost, clicks, impressions, conversions,
               w_avg_impr_pos, w_auction_win_share, w_avg_traffic_vol
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


def load_lag_rows(date_from: str, date_to: str) -> List[Dict[str, Any]]:
    """Только даты создания и оплаты — под оценку лага оплаты.

    Отдельная выборка, а не срез lead_rows, потому что зрелой считается
    когорта старше LAG_COHORT_MIN_AGE_DAYS = 180 дней, а прогон грузит лиды
    ровно за 180. Пересечение этих двух окон — один пограничный день, и в него
    попадают ровно те лиды, у которых лаг максимален (созданы 180 дней назад,
    оплачены недавно). Замер 29.08.2026: p90 по такой выборке = 178 дней при
    настоящем p90 = 37, окно лестницы уезжало на 2025-12-04…2026-03-04 —
    почти целиком за пределы загруженных фактов, и лестница считалась по трём
    дням: 14 оплат вместо 178 за месяц.

    Три колонки вместо сорока двух: год истории лидов целиком в память не
    поднять, а лаг больше ничего и не требует.
    """
    return _fetch_dicts(
        """
        SELECT created_date, payment_date, is_paid
        FROM crm_lead_details
        WHERE created_date BETWEEN %s AND %s AND is_paid
        """,
        (date_from, date_to),
    )


def load_device_bridge(date_from: str, date_to: str) -> List[Dict[str, Any]]:
    """Мост «лид → устройство»: crm_lead_details.client_id × визиты Метрики.

    Устройство клиента — самое частое по его визитам (клиент мог ходить с двух).
    JOIN LATERAL по client_id здесь нельзя: у edu_visit_behavior нет индекса по
    client_id, per-lead lookup превращается в seq scan на каждый лид. Таблица
    визитов ограничена клиентами-лидами по построению синка, поэтому две
    агрегатные выборки + склейка в питоне — секунды (замер probe 32622086445).
    Покрытие ~56 % лидов (client_id есть только у веб-лидов vuz) — потребитель
    обязан переносить ОТНОШЕНИЯ сегментов, не уровни.
    """
    leads = _fetch_dicts(
        """
        SELECT client_id, is_eff, is_connected, is_deal, is_paid, amount,
               direction
        FROM crm_lead_details
        WHERE created_date BETWEEN %s AND %s AND client_id IS NOT NULL
        """,
        (date_from, date_to),
    )
    votes = _fetch_dicts(
        """
        SELECT client_id, device_category, COUNT(*) AS n
        FROM edu_visit_behavior
        WHERE device_category IS NOT NULL
        GROUP BY client_id, device_category
        """,
        (),
    )
    tally: Dict[str, Counter] = defaultdict(Counter)
    for v in votes:
        tally[str(v["client_id"])][v["device_category"]] += int(v["n"])
    device_of = {cid: c.most_common(1)[0][0] for cid, c in tally.items()}

    out: List[Dict[str, Any]] = []
    for r in leads:
        device = device_of.get(str(r["client_id"]))
        if device is not None:
            out.append({**r, "device": device})
    return out


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


def load_keywords_by_campaign() -> Dict[str, List[str]]:
    """Ключевые фразы кабинета по кампаниям — из снимка структуры.

    Единственный ЧЕСТНЫЙ ответ на вопрос «что мы купили». Прежде его давала
    колонка matched_key отчёта поисковых запросов, и это было неверно: замер
    последнего окна показал, что из 32 266 строк, где matched_key совпадает с
    самим запросом (7,70 млн ₽ расхода), ключом кабинета оказались 625 — 1,9 %.
    Остальные 31 641 — подбор автотаргетинга, который отчёт возвращает в той же
    колонке. Защита собственной семантики принимала их за наши ключи и не
    давала минусовать ровно тот мусор, ради которого рычаг существует. Там же,
    где matched_key от запроса ОТЛИЧАЕТСЯ (5052 строки), ключами кабинета
    оказались 4071 — 80,6 %.

    Второй довод: спящий ключ. Отчёт показывает только то, что откручивалось в
    окне, а снимок структуры знает и ключи без показов — минус поверх такого
    ключа заблокировал бы его будущие показы, и заметить это было бы нечем.

    Кампания ПРИСУТСТВУЕТ в ответе с пустым списком, если её структура снята, а
    ключей в ней нет: это не пробел данных, а факт — так устроены Мастер
    кампаний и автотаргетинг (6 кампаний, 1,03 млн ₽ в последнем окне).
    Отсутствие кампании в ответе означает другое — снимок её не видел (3
    кампании, 0,59 млн ₽), и вызывающий обязан откатиться на прежний источник,
    а не считать, что защищать нечего.

    Берётся последняя версия каждого объекта, без ограничения свежести:
    защита ошибается в безопасную сторону, если подхватит уже снятый ключ.
    """
    rows = _fetch_dicts(
        """
        WITH latest AS (
            SELECT DISTINCT ON (object_level, object_id)
                   object_level, object_id, campaign_id, payload
            FROM edu_agent_objects
            WHERE object_level IN ('keyword', 'adgroup', 'ad')
            ORDER BY object_level, object_id, last_seen DESC
        )
        SELECT campaign_id,
               COALESCE(ARRAY_AGG(payload->>'Keyword')
                        FILTER (WHERE object_level = 'keyword'
                                  AND COALESCE(payload->>'Keyword', '') <> ''),
                        '{}') AS keywords
        FROM latest
        GROUP BY campaign_id
        """,
        (),
    )
    return {str(row["campaign_id"]): [str(k) for k in (row["keywords"] or [])]
            for row in rows}


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
        SELECT calc_date, setting_kind, setting_key, value, support_n,
               raw_value, rel_error
        FROM edu_agent_computed_settings
        WHERE object_level = %s AND object_id = %s
          AND calc_date = (
              SELECT MAX(calc_date) FROM edu_agent_computed_settings
              WHERE object_level = %s AND object_id = %s
          )
        """,
        (object_level, object_id, object_level, object_id),
    )


def load_latest_campaign_computed(
    campaign_ids: List[str],
) -> Dict[str, List[Dict[str, Any]]]:
    """Последний покампанийный расчёт (Э2.2) для НАЗВАННЫХ кампаний.

    {campaign_id: строки} — кампании без строк в ответе отсутствуют, и это
    штатный сигнал «личных значений нет, применяется кабинетный уровень».
    MAX(calc_date) считается внутри каждой кампании по той же причине, что и
    в load_latest_computed_settings: отставший объект не должен прятать или
    подменять расчёт остальных. Возраст строк проверяет вызывающий
    (agent_e1.computed_freshness_refusal) — «последний» не значит «свежий».
    """
    ids = sorted({str(c) for c in campaign_ids})
    if not ids:
        return {}
    rows = _fetch_dicts(
        """
        SELECT s.object_id, s.calc_date, s.setting_kind, s.setting_key,
               s.value, s.support_n, s.raw_value, s.rel_error
        FROM edu_agent_computed_settings s
        JOIN (
            SELECT object_id, MAX(calc_date) AS calc_date
            FROM edu_agent_computed_settings
            WHERE object_level = 'campaign' AND object_id = ANY(%s)
            GROUP BY object_id
        ) latest USING (object_id, calc_date)
        WHERE s.object_level = 'campaign'
        """,
        (ids,),
    )
    out: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(str(r.pop("object_id")), []).append(r)
    return out


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


def load_baseline_cpo(date_from: str, date_to: str) -> Dict[str, float]:
    """Базовая цена ОПЛАТЫ кампании за то же окно — база второго чекпоинта.

    Зеркало load_baseline_cpa со сменой знаменателя: там эффективные лиды,
    здесь оплаты. Второй чекпоинт (agent_e1_watchdog.money_verdict) сверяет
    вердикт по заявкам с деньгами через 35 дней, и сравнивать ему нужно с
    базой, снятой по ТОМУ ЖЕ окну и тем же способом — иначе разница между
    базой и наблюдением окажется разницей методик.

    Кампании без единой оплаты за окно в словарь не попадают: цены нет, и
    ноль здесь означал бы «оплата бесплатна».
    """
    rows = _fetch_dicts(
        """
        SELECT campaign_id,
               SUM(cost) / NULLIF(SUM(payments_fact), 0) AS cpo
        FROM edu_agent_facts
        WHERE fact_date BETWEEN %s AND %s
        GROUP BY campaign_id
        HAVING SUM(payments_fact) > 0
        """,
        (date_from, date_to),
    )
    return {str(r["campaign_id"]): float(r["cpo"]) for r in rows}


def load_baseline_volume(date_from: str, date_to: str) -> Dict[str, Dict[str, float]]:
    """Объём базы кампании за окно: сколько эффективных лидов в день.

    Зеркало load_baseline_cpa со сменой вопроса: там «сколько стоил лид»,
    здесь «сколько их было». Отдельной функцией, а не расширением соседней:
    у load_baseline_cpa четыре потребителя, и все ждут Dict[str, float].

    Темп (лиды в день), а не сумма за окно: окно базы и окно наблюдения
    разной длины, и суммы сравнивались бы через разные знаменатели.
    """
    rows = _fetch_dicts(
        """
        SELECT campaign_id,
               SUM(eff_leads)            AS leads,
               COUNT(DISTINCT fact_date) AS days
        FROM edu_agent_facts
        WHERE fact_date BETWEEN %s AND %s
        GROUP BY campaign_id
        HAVING COUNT(DISTINCT fact_date) > 0
        """,
        (date_from, date_to),
    )
    return {
        str(r["campaign_id"]): {
            "leads": float(r["leads"] or 0.0),
            "days": int(r["days"]),
            "leads_per_day": round(float(r["leads"] or 0.0) / int(r["days"]), 4),
        }
        for r in rows
    }


def load_daily_account_totals(date_from: str, date_to: str) -> List[Dict[str, Any]]:
    """Дневные агрегаты ВСЕЙ витрины: расход и эффективные лиды по дню.

    Контроль для сезонной поправки красных линий (agent_e1_watchdog):
    подорожал ли лид у кабинета в целом между базой действия и окном
    наблюдения. По строке на день, а не на кампанию, — запрос дешёвый.
    """
    return _fetch_dicts(
        """
        SELECT fact_date,
               SUM(cost)      AS cost,
               SUM(eff_leads) AS eff_leads
        FROM edu_agent_facts
        WHERE fact_date BETWEEN %s AND %s
        GROUP BY fact_date
        ORDER BY fact_date
        """,
        (date_from, date_to),
    )


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
        SELECT campaign_id, fact_date, cost, eff_leads, payments_fact
        FROM edu_agent_facts
        WHERE campaign_id = ANY(%s) AND fact_date BETWEEN %s AND %s
        """,
        (ids, date_from, date_to),
    )


def load_quality_facts(date_from: str, date_to: str) -> List[Dict[str, Any]]:
    """Дневные лиды и скоры по всем кампаниям — сырьё тормоза роста (quality.py).

    Без агрегата и без фильтра по кампаниям: окна «до доливки» и «после» режет
    сам расчёт, а список кампаний до вызова неизвестен — тормоз обязан судить
    и тех, кого сегодня впервые предложат усилить.
    """
    return _fetch_dicts(
        """
        SELECT campaign_id, fact_date, eff_leads, scored_leads, sum_p_pay
        FROM edu_agent_facts
        WHERE fact_date BETWEEN %s AND %s
        """,
        (date_from, date_to),
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


def mart_last_fact_date(date_from: str, date_to: str) -> Optional[str]:
    """Последний день, который уже лежит в витрине фактов внутри окна.

    Нужен сверке сумм: витрина отстаёт от источника на прогон по построению —
    свежие дни источник отдаёт сразу, а в витрину они попадут только текущим
    прогоном. Сравнивать полное окно источника с недописанной витриной значит
    мерить лаг, а не сохранность данных.
    """
    rows = _fetch_dicts(
        "SELECT MAX(fact_date) AS last FROM edu_agent_facts "
        "WHERE fact_date BETWEEN %s AND %s",
        (date_from, date_to),
    )
    last = rows[0]["last"] if rows else None
    return str(last) if last else None


# Доля типичного дня, ниже которой день CRM считается недобравшим. Тот же
# приём, что у ширины витрины в gate.py: эталон — медиана окна, устойчивая
# к единичному битому дню.
CRM_MATURITY_MIN_SHARE = 0.5

# Окно, по которому берётся типичный день. Достаточно двух недель: сезонный
# уровень лидов внутри них не успевает уехать, а медиана уже устойчива.
CRM_MATURITY_WINDOW_DAYS = 14


AGENT_CONFIG_DDL = """
    CREATE TABLE IF NOT EXISTS edu_agent_config (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        preset     TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_by TEXT
    )
"""


def _parse_config_value(raw: str) -> Any:
    """Строка колонки value → значение параметра панели.

    Колонка текстовая (значения разнотипны: доли, дни, «full»), поэтому тип
    восстанавливается здесь. Два случая, на которых прежний разбор
    (`float(raw) if raw.replace('.', '', 1).isdigit() else raw`) врал:

      * **Пусто.** Единственный способ сказать «потолок месячного бюджета не
        задан» после того, как он был задан: NULL колонка не принимает
        (value TEXT NOT NULL), а удаление строки стирает и автора правки.
        Пустая строка приезжала обратно как `''`, и валидация роняла прогон
        на настройке, которую панель считает законной, — то есть очистить
        nullable-параметр было нельзя вовсе.
      * **Отрицательные числа и экспоненциальная запись.** `isdigit()` на
        «-1» и «1e6» ложен, значение оставалось строкой и до сравнения с
        границами доезжало неразобранным. Сейчас таких допустимых значений в
        SPEC нет (все min ≥ 0), но разбор, который врёт про тип, — мина под
        первый же параметр, у которого отрицательное значение осмысленно.

    Значение вне диапазона по-прежнему роняет прогон — но уже в
    sync/agent/config.py, где живут все правила валидации.
    """
    text = str(raw).strip()
    if text == "":
        return None
    # Составные настройки (lane_steps, shadow_lanes) хранятся в той же
    # текстовой колонке как JSON. Без этой ветки они приезжали строкой, и
    # _lane_steps ронял разбор словами «нужен словарь» — то есть выпустить
    # полосу из тени через панель было нельзя вовсе, при живом ключе в SPEC.
    if text[0] in "{[":
        try:
            return json.loads(text)
        except ValueError:
            return text
    try:
        return float(text)
    except ValueError:
        return text


def load_agent_config() -> Dict[str, Any]:
    """Настройки агента из БД: {'preset': ..., 'overrides': {...}}.

    Таблицы нет или она пуста — пустой результат: агент работает на кодовых
    дефолтах, и это законное состояние, а не ошибка. Значения проверяет
    sync/agent/config.resolve — здесь только чтение строк и восстановление
    типа, чтобы правила валидации жили в одном месте.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(AGENT_CONFIG_DDL)
            enable_rls_for_ddl(cur, AGENT_CONFIG_DDL)
        conn.commit()
    rows = _fetch_dicts("SELECT key, value, preset FROM edu_agent_config")
    preset = None
    overrides: Dict[str, Any] = {}
    for row in rows:
        key = str(row["key"])
        if key == "preset":
            preset = str(row["value"])
            continue
        overrides[key] = _parse_config_value(str(row["value"]))
    return {"preset": preset, "overrides": overrides}


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

    Зрелым считается последний день, набравший заметную долю ТИПИЧНОГО дня
    окна (CRM_MATURITY_MIN_SHARE от медианы). Прежний MAX(created_date)
    объявлял день зрелым по ОДНОМУ раннему лиду: граница уезжала вперёд, в
    окна наблюдения попадал день, где CRM ещё почти пуста, и его CPA завышен
    всегда в одну сторону — то есть ровно тот дефект, от которого граница и
    защищает (аудит 2026-08-23).

    None — лидов нет вовсе. Это не «граница сегодня», а «наблюдать не по чему»:
    подставлять здесь сегодняшний день значило бы разрешить вердикт по пустоте.
    """
    rows = _fetch_dicts(
        """
        SELECT created_date AS d, COUNT(*) AS n
        FROM crm_lead_details
        WHERE created_date >= (
            SELECT MAX(created_date) FROM crm_lead_details
        ) - make_interval(days => %s)
        GROUP BY created_date
        ORDER BY created_date
        """,
        (CRM_MATURITY_WINDOW_DAYS,),
    )
    days = []
    for row in rows or ():
        raw = row.get("d")
        if raw is None:
            continue
        day = raw if isinstance(raw, date) else date.fromisoformat(str(raw)[:10])
        days.append((day, int(row.get("n") or 0)))
    if not days:
        return None
    typical = median(sorted(n for _, n in days))
    need = max(1.0, typical * CRM_MATURITY_MIN_SHARE)
    full = [day for day, n in days if n >= need]
    return max(full) if full else max(day for day, _ in days)


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


def load_cost_by_campaign(date_from: str, date_to: str) -> Dict[str, float]:
    """СУММА расхода кампании за окно — знаменатель доли, а не множитель цены.

    Отдельно от load_daily_cost_by_campaign, и это не дубль. Там AVG по дням,
    ПРИСУТСТВУЮЩИМ в окне: кампания, отработавшая 5 дней из 28, получает свой
    дневной темп, и умножение на 28 даёт расход, которого не было. Для цены
    ошибки это верно (важен темп), для доли денег — нет: доля, посчитанная по
    выдуманным суммам, врёт и в числителе, и в знаменателе.
    """
    rows = _fetch_dicts(
        """
        SELECT campaign_id, SUM(cost) AS cost
        FROM edu_agent_facts
        WHERE fact_date BETWEEN %s AND %s
        GROUP BY campaign_id
        """,
        (date_from, date_to),
    )
    return {str(r["campaign_id"]): float(r["cost"] or 0.0) for r in rows}


def load_wordstat_demand(week_from: str) -> List[Dict[str, Any]]:
    """Недельный спрос Wordstat с указанной недели.

    Оба региона ('ru' и 'msk') — отбор делает потребитель
    (sync/agent/demand.py считает только 'ru', чтобы не сложить москвичей
    дважды), а не запрос: фильтр, спрятанный в SQL, невидим в тестах.
    """
    return _fetch_dicts(
        """
        SELECT week_start, region, phrase, frequency
        FROM edu_wordstat_demand
        WHERE week_start >= %s
        """,
        (week_from,),
    )


# ---------------------------------------------------------------- запись


def _batch(sql: str, rows: List[Dict[str, Any]], page_size: int = 500) -> int:
    if not rows:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, rows, page_size=page_size)
        conn.commit()
    return len(rows)


UPSERT_FACTS_SQL = """
        INSERT INTO edu_agent_facts (
            fact_date, campaign_id, campaign_name, project, direction,
            cost, clicks, impressions, leads, eff_leads, sum_p_pay,
            payments_fact, avg_impr_pos, auction_win_share, avg_traffic_vol,
            connected_leads, deals, mins_to_connection_sum, mins_to_connection_count,
            days_to_pay_sum, days_to_pay_count, revenue, conversions, scored_leads
        ) VALUES (
            %(fact_date)s, %(campaign_id)s, %(campaign_name)s, %(project)s, %(direction)s,
            %(cost)s, %(clicks)s, %(impressions)s, %(leads)s, %(eff_leads)s, %(sum_p_pay)s,
            %(payments_fact)s, %(avg_impr_pos)s, %(auction_win_share)s, %(avg_traffic_vol)s,
            %(connected_leads)s, %(deals)s, %(mins_to_connection_sum)s,
            %(mins_to_connection_count)s, %(days_to_pay_sum)s, %(days_to_pay_count)s,
            %(revenue)s, %(conversions)s, %(scored_leads)s
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
            avg_traffic_vol = EXCLUDED.avg_traffic_vol,
            connected_leads = EXCLUDED.connected_leads,
            deals = EXCLUDED.deals,
            mins_to_connection_sum = EXCLUDED.mins_to_connection_sum,
            mins_to_connection_count = EXCLUDED.mins_to_connection_count,
            days_to_pay_sum = EXCLUDED.days_to_pay_sum,
            days_to_pay_count = EXCLUDED.days_to_pay_count,
            revenue = EXCLUDED.revenue,
            conversions = EXCLUDED.conversions,
            scored_leads = EXCLUDED.scored_leads,
            collected_at = now()
"""


def upsert_facts(rows: List[Dict[str, Any]]) -> int:
    return _batch(UPSERT_FACTS_SQL, rows, page_size=1000)


def upsert_sliced_facts(rows: List[Dict[str, Any]]) -> int:
    return _batch(
        """
        INSERT INTO edu_agent_facts_sliced (
            week_start, campaign_id, slice_kind, slice_key, slice_label,
            cost, clicks, impressions, conversions
        ) VALUES (
            %(week_start)s, %(campaign_id)s, %(slice_kind)s, %(slice_key)s,
            %(slice_label)s,
            %(cost)s, %(clicks)s, %(impressions)s, %(conversions)s
        )
        ON CONFLICT (week_start, campaign_id, slice_kind, slice_key) DO UPDATE SET
            slice_label = COALESCE(NULLIF(EXCLUDED.slice_label, ''),
                                   edu_agent_facts_sliced.slice_label),
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


def exclude_holdout(campaign_ids: List[str], excluded_on: str,
                    note: str = "выбыла из заповедника: нет лидов за окно решений") -> int:
    """Пометить кампании заповедника выбывшими (excluded_at).

    Без этого состав заповедника только растёт, а живым он от этого не
    становится: фактическая когорта из девяти кампаний за месяц потеряла
    четыре, и её эффективный размер упал с 5,19 до 2,93 — контроль, по
    которому вычитается сезон, превратился в три кампании, а к +60 дням в
    полторы (docs/AGENT-TICK-POWER.md, 29.08.2026).

    Причина отбора не затирается, а дополняется: строка заповедника — это
    история «почему взяли» и «почему вывели» сразу, и первая половина нужна,
    чтобы понять вторую. Уже выведенные не трогаются (excluded_at IS NULL):
    дата выбытия должна остаться той, в которую кампания умерла.
    """
    payload = [{"campaign_id": str(i), "excluded_at": excluded_on,
                "note": f"; {note} ({excluded_on})"} for i in campaign_ids]
    return _batch(
        """
        UPDATE edu_agent_holdout
           SET excluded_at = %(excluded_at)s,
               reason = reason || %(note)s
         WHERE campaign_id = %(campaign_id)s AND excluded_at IS NULL
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
            verdict = EXCLUDED.verdict,
            -- Замер уточняет и СПОСОБ измерения: нашёлся контроль за то же
            -- окно — механизм становится did_holdout, а класс надёжности A.
            -- Без этих трёх полей строка ставки, заведённая реестром до
            -- отправки, навсегда осталась бы «before_after / B», хотя
            -- посчитана разностью разностей.
            measured_on = EXCLUDED.measured_on,
            mechanism = EXCLUDED.mechanism,
            reliability_class = EXCLUDED.reliability_class,
            -- Слияние, а не замена: в params ставки лежат её собственные поля
            -- (карман разведки, ожидание модели), а сторож приносит числа
            -- замера. Замена стёрла бы половину истории каждой ставки.
            params = edu_agent_experiments.params || EXCLUDED.params
        """,
        payload,
    )


# ------------------------------------------------- реестр гипотез (ставки)


UPSERT_HYPOTHESES_SQL = """
    INSERT INTO edu_agent_experiments (
        experiment_id, hypothesis_type, object_level, object_id, params,
        mechanism, started_on, metric, reliability_class, source,
        status, status_reason, stake_rub, stake_source, horizon_days,
        success_criterion, red_line, idempotency_key
    ) VALUES (
        %(experiment_id)s, %(hypothesis_type)s, %(object_level)s, %(object_id)s,
        %(params)s, %(mechanism)s, %(started_on)s, %(metric)s,
        %(reliability_class)s, %(source)s, %(status)s, %(status_reason)s,
        %(stake_rub)s, %(stake_source)s, %(horizon_days)s,
        %(success_criterion)s, %(red_line)s, %(idempotency_key)s
    )
    ON CONFLICT (experiment_id) DO UPDATE SET
        params            = EXCLUDED.params,
        stake_rub         = EXCLUDED.stake_rub,
        horizon_days      = EXCLUDED.horizon_days,
        success_criterion = EXCLUDED.success_criterion,
        red_line          = EXCLUDED.red_line
    WHERE edu_agent_experiments.status = 'queued'
"""


def upsert_hypotheses(rows: List[Dict[str, Any]]) -> int:
    """Заводит ставки реестра (sync/agent/experiments.open_bet).

    Повторная планировка того же действия обновляет ставку свежими числами —
    но ТОЛЬКО пока она в очереди. Условие в DO UPDATE — та же граница «ещё не
    сделано / уже сделано», что у журнала действий
    (writer/db.INSERT_ACTION_SQL): запущенную или закрытую ставку переписать
    нельзя, иначе вердикт выносился бы по красной линии и горизонту, которых
    в момент запуска не было.
    """
    payload = [{**r,
                "params": json.dumps(r.get("params", {}), ensure_ascii=False),
                "red_line": json.dumps(r.get("red_line", {}), ensure_ascii=False)}
               for r in rows]
    return _batch(UPSERT_HYPOTHESES_SQL, payload)


OPEN_HYPOTHESES_SQL = """
    SELECT e.experiment_id, e.status, e.started_on, e.horizon_days,
           e.stake_rub, e.object_id, e.hypothesis_type, e.success_criterion,
           e.idempotency_key,
           a.applied_at, a.rolled_back_at, a.harmful_verdict_at,
           a.observation_verdict, a.observation_closed_at
      FROM edu_agent_experiments e
      LEFT JOIN edu_agent_actions a ON a.idempotency_key = e.idempotency_key
     WHERE e.status = ANY(%s)
     ORDER BY e.started_on, e.experiment_id
"""


def load_open_hypotheses(statuses: Any) -> List[Dict[str, Any]]:
    """Незакрытые ставки ВМЕСТЕ с состоянием своего действия.

    Одним запросом с журналом, а не двумя выборками и склейкой в питоне:
    решение о судьбе ставки принимается по паре «реестр + действие»
    (experiments.settle), и разъехавшиеся во времени половины дали бы вердикт
    по состоянию, которого одновременно не существовало.

    LEFT JOIN, а не INNER: ставка заводится ДО записи действия в журнал, и в
    коротком окне между двумя записями строки действия ещё нет. Для settle
    это тот же случай, что «действие не применено», — и обрабатывается им же.

    Список статусов передаёт вызывающий (experiments.OPEN_STATUSES): что
    считать открытым, решает жизненный цикл, а не запрос.
    """
    return _fetch_dicts(OPEN_HYPOTHESES_SQL, (list(statuses),))


CLOSE_HYPOTHESIS_SQL = """
    UPDATE edu_agent_experiments
       SET status        = %(status)s,
           status_reason = %(reason)s,
           closed_at     = CASE WHEN %(status)s = ANY(%(closed)s)
                                THEN now() ELSE closed_at END
     WHERE experiment_id = %(experiment_id)s
       AND status = %(from_status)s
    RETURNING experiment_id
"""


def move_hypothesis(experiment_id: str, from_status: str, status: str,
                    reason: str, closed_statuses: Any) -> bool:
    """Перевод ставки в следующий статус. True — перевод лёг.

    Законность перехода проверяет experiments.check_transition ДО вызова;
    здесь стоит гард от ГОНКИ: условие `status = from_status` не даёт двум
    прогонам сторожа закрыть одну ставку дважды и записать два исхода. Тот же
    приём, что у mark_observation_closed в журнале действий.

    False — строку уже забрал другой прогон (или её статус успел измениться).
    Это не ошибка: исход записан, просто не нами.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(CLOSE_HYPOTHESIS_SQL, {
                "experiment_id": str(experiment_id),
                "from_status": str(from_status),
                "status": str(status),
                "reason": str(reason)[:500],
                "closed": list(closed_statuses),
            })
            landed = cur.fetchone() is not None
        conn.commit()
    return landed


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
    payload = [{"rel_error": None, **r, "calc_date": calc_date,
                "object_level": object_level, "object_id": object_id}
               for r in rows]
    return _batch(
        """
        INSERT INTO edu_agent_computed_settings (
            calc_date, object_level, object_id, setting_kind, setting_key,
            value, support_n, raw_value, rel_error
        ) VALUES (
            %(calc_date)s, %(object_level)s, %(object_id)s, %(setting_kind)s,
            %(setting_key)s, %(value)s, %(support_n)s, %(raw_value)s,
            %(rel_error)s
        )
        ON CONFLICT (calc_date, object_level, object_id, setting_kind, setting_key)
        DO UPDATE SET value = EXCLUDED.value,
                      support_n = EXCLUDED.support_n,
                      raw_value = EXCLUDED.raw_value,
                      rel_error = EXCLUDED.rel_error
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


AGENT_MANIFEST_DDL = """
    CREATE TABLE IF NOT EXISTS edu_agent_manifest (
        id             TEXT PRIMARY KEY,
        schema_version INTEGER NOT NULL,
        generated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        payload        JSONB NOT NULL
    )
"""

# Строка одна и всегда с этим ключом: манифест описывает КОД, который сейчас
# работает, а не событие. История его версий — это история репозитория, и
# копить её второй раз в базе значит заводить второй источник правды о том,
# каким агент был во вторник.
MANIFEST_ROW_ID = "current"


def save_manifest(payload: Dict[str, Any]) -> None:
    """Кладёт манифест устройства агента (sync/agent/manifest.build) в базу.

    Пишет ПРОГОН, а не интерфейс: манифест собран из констант самого прогона,
    и выгрузить его может только тот, кто эти константы импортировал. Экран
    Panda-BI читает результат и своего списка полос/ключей не держит.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(AGENT_MANIFEST_DDL)
            enable_rls_for_ddl(cur, AGENT_MANIFEST_DDL)
            cur.execute(
                """
                INSERT INTO edu_agent_manifest (id, schema_version, generated_at, payload)
                VALUES (%(id)s, %(schema_version)s, now(), %(payload)s)
                ON CONFLICT (id) DO UPDATE
                   SET schema_version = EXCLUDED.schema_version,
                       generated_at   = EXCLUDED.generated_at,
                       payload        = EXCLUDED.payload
                """,
                {"id": MANIFEST_ROW_ID,
                 "schema_version": int(payload.get("schema_version") or 0),
                 "payload": json.dumps(payload, ensure_ascii=False)},
            )
        conn.commit()

