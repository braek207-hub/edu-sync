ALTER TABLE polinarepik_metrica_visits
  DROP CONSTRAINT IF EXISTS polinarepik_metrica_visits_date_client_id_key;

UPDATE polinarepik_metrica_visits
SET
  utm_campaign = COALESCE(utm_campaign, ''),
  utm_source = COALESCE(utm_source, ''),
  utm_medium = COALESCE(utm_medium, '')
WHERE utm_campaign IS NULL OR utm_source IS NULL OR utm_medium IS NULL;

ALTER TABLE polinarepik_metrica_visits
  ALTER COLUMN utm_campaign SET DEFAULT '',
  ALTER COLUMN utm_source SET DEFAULT '',
  ALTER COLUMN utm_medium SET DEFAULT '';

CREATE UNIQUE INDEX IF NOT EXISTS polinarepik_metrica_visits_dedupe_idx
  ON polinarepik_metrica_visits (date, client_id, utm_campaign, utm_source, utm_medium);

CREATE INDEX IF NOT EXISTS polinarepik_metrica_visits_client_date_idx
  ON polinarepik_metrica_visits (client_id, date DESC);

CREATE TABLE IF NOT EXISTS polinarepik_metrica_purchases (
  order_id TEXT PRIMARY KEY,
  purchase_date DATE NOT NULL,
  client_id TEXT,
  traffic_source TEXT,
  utm_source TEXT,
  utm_medium TEXT,
  utm_campaign TEXT,
  purchases INTEGER NOT NULL DEFAULT 1,
  revenue NUMERIC(14, 2),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS polinarepik_metrica_purchases_date_idx
  ON polinarepik_metrica_purchases (purchase_date);
CREATE INDEX IF NOT EXISTS polinarepik_metrica_purchases_client_idx
  ON polinarepik_metrica_purchases (client_id);
