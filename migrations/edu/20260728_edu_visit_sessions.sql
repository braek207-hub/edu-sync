-- Пер-визитные сессии Яндекс Метрики (Logs API) для Ф2 ML-пайплайна EDU.
-- В отличие от edu_visit_behavior (агрегат визитов за день) — точное время визита
-- (visit_ts) и полный набор сырых полей визита; читает feature-builder на уровне визита.
-- Пишет reader (задача 2 плана 2026-07-27-edu-ml-phase-b-logs-api) через Logs API.
-- Инвариант: visit_ts — timestamptz (UTC), как остальные ts-поля пайплайна.
CREATE TABLE IF NOT EXISTS edu_visit_sessions (
  counter_id              BIGINT       NOT NULL,           -- счётчик Метрики
  visit_ts                TIMESTAMPTZ  NOT NULL,           -- точное время визита (UTC)
  client_id               TEXT         NOT NULL,           -- ym:s:clientID — джойн-ключ к лиду
  visit_id                TEXT         NOT NULL,           -- ym:s:visitID — уникален в рамках counter_id
  visit_duration          INTEGER,                          -- ym:s:visitDuration, сек
  bounce                  INTEGER,                          -- ym:s:bounce, 0/1
  page_views              INTEGER,                          -- ym:s:pageViews
  is_new_user             INTEGER,                          -- ym:s:isNewUser, 0/1
  user_id_hash            TEXT,                             -- ym:s:userIDHash
  utm_source              TEXT,
  utm_medium              TEXT,
  utm_campaign            TEXT,
  utm_content             TEXT,
  utm_term                TEXT,
  first_traffic_source    TEXT,                             -- ym:s:firstTrafficSource
  lastsign_traffic_source TEXT,                              -- ym:s:lastSignTrafficSource
  source_engine           TEXT,                             -- ym:s:<...>Engine
  referer                 TEXT,
  direct_platform_type    TEXT,                             -- ym:s:directPlatformType
  direct_condition_type   TEXT,                              -- ym:s:directConditionType
  direct_phrase           TEXT,                              -- ym:s:directPhrase
  direct_order_name       TEXT,                              -- ym:s:directOrderName
  has_gclid               INTEGER,                           -- ym:s:hasGCLID, 0/1
  device_category         TEXT,
  os                      TEXT,
  browser                 TEXT,
  phone_model             TEXT,
  screen_w                INTEGER,
  screen_h                INTEGER,
  network_type            TEXT,
  PRIMARY KEY (counter_id, client_id, visit_id)
);

-- Джойн к лидам идёт по client_id (feature-builder агрегирует визиты по client_id).
CREATE INDEX IF NOT EXISTS idx_edu_visit_sessions_client
  ON edu_visit_sessions (client_id);

-- RLS: доступ только серверный (см. аудит panda-bi-audit-cleanup).
ALTER TABLE edu_visit_sessions ENABLE ROW LEVEL SECURITY;
