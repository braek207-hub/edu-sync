-- Task 7: две точки скоринга. edu_ml_runs получает scoring_point и составной PK.
-- Идемпотентно; источник истины — sync/db.py:ensure_ml_scoring_tables() (migrations-блок).
ALTER TABLE edu_ml_runs ADD COLUMN IF NOT EXISTS scoring_point TEXT NOT NULL DEFAULT 'at_creation';

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'edu_ml_runs'::regclass AND contype = 'p'
      AND pg_get_constraintdef(oid) = 'PRIMARY KEY (version)'
  ) THEN
    ALTER TABLE edu_ml_runs DROP CONSTRAINT edu_ml_runs_pkey;
    ALTER TABLE edu_ml_runs ADD PRIMARY KEY (version, scoring_point);
  END IF;
END $$;
