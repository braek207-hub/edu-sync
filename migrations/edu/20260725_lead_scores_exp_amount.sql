-- Ф1c: per-lead ожидаемый чек (E(amount) из Tweedie) для прогноза выручки/ROMI по кампании. Идемпотентно.
ALTER TABLE edu_lead_scores ADD COLUMN IF NOT EXISTS exp_amount NUMERIC;
