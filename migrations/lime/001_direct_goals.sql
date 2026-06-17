-- LIME Direct: конверсии по целям (LSC) + метаданные бюджета
ALTER TABLE lime_direct_stats
  ADD COLUMN IF NOT EXISTS conversions JSONB,
  ADD COLUMN IF NOT EXISTS package_strategy_id BIGINT,
  ADD COLUMN IF NOT EXISTS package_strategy_name TEXT,
  ADD COLUMN IF NOT EXISTS budget_source TEXT,
  ADD COLUMN IF NOT EXISTS budget_type TEXT;

CREATE INDEX IF NOT EXISTS lime_direct_stats_conversions_gin
  ON lime_direct_stats USING gin (conversions);
