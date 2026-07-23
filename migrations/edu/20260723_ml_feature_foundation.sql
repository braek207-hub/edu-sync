-- Feature store ML-скоринга EDU (land=vuz). Идемпотентно; источник истины —
-- sync/db.py:ensure_ml_feature_tables(). Здесь для аудита схемы.
CREATE TABLE IF NOT EXISTS edu_lead_features (
  lead_id        TEXT PRIMARY KEY,
  client_id      TEXT,
  land           TEXT NOT NULL,
  created_date   DATE NOT NULL,
  -- метка (outcome): NULL для незрелых когорт
  label_paid     BOOLEAN,
  label_connected BOOLEAN,
  label_deal     BOOLEAN,
  is_matured     BOOLEAN NOT NULL DEFAULT FALSE,
  amount         DOUBLE PRECISION,          -- outcome, для Tweedie в Ф1b
  days_to_pay    INTEGER,                   -- outcome
  -- фичи хранятся как JSONB с флагом точки доступности внутри имени: f__<point>__<name>
  features       JSONB NOT NULL DEFAULT '{}'::jsonb,
  built_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_elf_created ON edu_lead_features(created_date);
CREATE INDEX IF NOT EXISTS idx_elf_client ON edu_lead_features(client_id);

-- Kaplan-Meier кривая: доля оплат, наступивших к возрасту когорты `age_days`.
CREATE TABLE IF NOT EXISTS edu_ml_maturation (
  land            TEXT NOT NULL,
  age_days        INTEGER NOT NULL,
  matured_fraction DOUBLE PRECISION NOT NULL, -- 0..1, монотонно неубывающая
  built_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (land, age_days)
);
