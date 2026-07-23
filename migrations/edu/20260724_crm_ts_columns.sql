-- Ф2: точное время заявки/дозвона (синк раньше обрезал до date). Идемпотентно.
ALTER TABLE crm_lead_details
  ADD COLUMN IF NOT EXISTS created_ts timestamptz,
  ADD COLUMN IF NOT EXISTS connected_ts timestamptz;
