-- Экспорт весов логистики для реалтайм-скоринга лида (Vercel /api/edu/lead-quality).
-- Идемпотентно; источник истины — sync/db.py:ensure_ml_weights_table().
-- RLS без политик: через публичный API Supabase таблицу не читает никто,
-- postgres-роль (edu-sync, Panda-BI prisma) — свободно.
CREATE TABLE IF NOT EXISTS edu_ml_weights (
  point      TEXT PRIMARY KEY,
  version    TEXT NOT NULL,
  payload    JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE edu_ml_weights ENABLE ROW LEVEL SECURITY;
