CREATE TABLE IF NOT EXISTS polinarepik_metrica_visits (
  id SERIAL PRIMARY KEY,
  date DATE NOT NULL,
  client_id TEXT NOT NULL,
  traffic_source TEXT,
  utm_source TEXT,
  utm_medium TEXT,
  utm_campaign TEXT DEFAULT '',
  visits INTEGER NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS polinarepik_metrica_visits_date_idx ON polinarepik_metrica_visits (date);
CREATE INDEX IF NOT EXISTS polinarepik_metrica_visits_client_id_idx ON polinarepik_metrica_visits (client_id);
CREATE INDEX IF NOT EXISTS polinarepik_metrica_visits_date_campaign_idx ON polinarepik_metrica_visits (date, utm_campaign);
