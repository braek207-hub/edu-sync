-- Автопилот Директа EDU, Э1a: движок записи.

CREATE TABLE IF NOT EXISTS edu_agent_actions (
      action_id        TEXT PRIMARY KEY,
      idempotency_key  TEXT NOT NULL UNIQUE,
      created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
      applied_at       TIMESTAMPTZ,
      account          TEXT NOT NULL,
      object_level     TEXT NOT NULL,
      object_id        TEXT NOT NULL,
      action_kind      TEXT NOT NULL,
      payload          JSONB NOT NULL DEFAULT '{}'::jsonb,
      previous_state   JSONB NOT NULL DEFAULT '{}'::jsonb,
      red_line         JSONB NOT NULL DEFAULT '{}'::jsonb,
      risk_rub         DOUBLE PRECISION NOT NULL DEFAULT 0,
      status           TEXT NOT NULL DEFAULT 'planned',
      response         JSONB NOT NULL DEFAULT '{}'::jsonb,
      rolled_back_at   TIMESTAMPTZ
    );

CREATE TABLE IF NOT EXISTS edu_agent_risk_budget (
      week_start   DATE PRIMARY KEY,
      limit_rub    DOUBLE PRECISION NOT NULL,
      note         TEXT
    );
