-- ML Ф1b: артефакты, прогоны, скоры, прогноз выручки. Идемпотентно; источник истины —
-- sync/db.py:ensure_ml_scoring_tables().
CREATE TABLE IF NOT EXISTS edu_ml_artifacts (
  version    TEXT NOT NULL,
  kind       TEXT NOT NULL,
  blob       BYTEA NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (version, kind)
);
CREATE TABLE IF NOT EXISTS edu_ml_runs (
  version       TEXT PRIMARY KEY,
  trained_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  n_train       INTEGER NOT NULL,
  n_pos_pay     INTEGER NOT NULL,
  prauc_pay     DOUBLE PRECISION,
  brier_pay     DOUBLE PRECISION,
  lift_final    DOUBLE PRECISION,
  lift_baseline DOUBLE PRECISION,
  lift_pilot    DOUBLE PRECISION,
  gate_passed   BOOLEAN NOT NULL DEFAULT FALSE,
  stage_metrics JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE TABLE IF NOT EXISTS edu_lead_scores (
  lead_id       TEXT NOT NULL,
  scoring_point TEXT NOT NULL,
  p_connect     DOUBLE PRECISION,
  p_deal        DOUBLE PRECISION,
  p_pay         DOUBLE PRECISION,
  decile        INTEGER,
  top_shap      JSONB NOT NULL DEFAULT '[]'::jsonb,
  model_version TEXT,
  scored_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (lead_id, scoring_point)
);
CREATE TABLE IF NOT EXISTS edu_revenue_forecast (
  as_of_date    DATE NOT NULL,
  segment       TEXT NOT NULL,
  pending_leads INTEGER NOT NULL,
  exp_payments  DOUBLE PRECISION NOT NULL,
  exp_revenue   DOUBLE PRECISION NOT NULL,
  revenue_lo    DOUBLE PRECISION NOT NULL,
  revenue_hi    DOUBLE PRECISION NOT NULL,
  model_version TEXT,
  built_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (as_of_date, segment)
);
