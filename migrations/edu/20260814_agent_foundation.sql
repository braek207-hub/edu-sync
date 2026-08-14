-- Автопилот Директа EDU, Э0: таблицы агента.
-- Идемпотентно: безопасно применять повторно.

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
    );

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
    );

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
    );

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
    );

CREATE TABLE IF NOT EXISTS edu_agent_settings_snapshot (
      campaign_id   TEXT NOT NULL,
      content_hash  TEXT NOT NULL,
      settings      JSONB NOT NULL DEFAULT '{}'::jsonb,
      first_seen    DATE NOT NULL,
      last_seen     DATE NOT NULL,
      PRIMARY KEY (campaign_id, content_hash)
    );

CREATE TABLE IF NOT EXISTS edu_agent_behavior (
      window_from   DATE NOT NULL,
      window_to     DATE NOT NULL,
      campaign_id   TEXT NOT NULL,
      visits        INTEGER NOT NULL DEFAULT 0,
      bounces       INTEGER NOT NULL DEFAULT 0,
      pageviews     INTEGER NOT NULL DEFAULT 0,
      visit_seconds BIGINT  NOT NULL DEFAULT 0,
      PRIMARY KEY (window_from, campaign_id)
    );

CREATE TABLE IF NOT EXISTS edu_agent_guard (
      run_ts       TIMESTAMPTZ NOT NULL DEFAULT now(),
      check_name   TEXT NOT NULL,
      status       TEXT NOT NULL,
      detail       JSONB NOT NULL DEFAULT '{}'::jsonb,
      PRIMARY KEY (run_ts, check_name)
    );

CREATE TABLE IF NOT EXISTS edu_agent_holdout (
      campaign_id   TEXT PRIMARY KEY,
      direction     TEXT,
      stratum       TEXT NOT NULL,
      included_at   DATE NOT NULL,
      excluded_at   DATE,
      reason        TEXT NOT NULL
    );

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
    );

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
    );

CREATE TABLE IF NOT EXISTS edu_agent_profile (
      calc_date    DATE NOT NULL,
      campaign_id  TEXT NOT NULL,
      distance     DOUBLE PRECISION NOT NULL,
      gaps         JSONB NOT NULL DEFAULT '[]'::jsonb,
      quartile     INTEGER NOT NULL,
      PRIMARY KEY (calc_date, campaign_id)
    );
