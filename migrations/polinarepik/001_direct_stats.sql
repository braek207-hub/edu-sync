CREATE TABLE IF NOT EXISTS polinarepik_direct_stats (
  id SERIAL PRIMARY KEY,
  date DATE NOT NULL,
  campaign_id TEXT NOT NULL,
  campaign_name TEXT,
  source_type TEXT,
  cost NUMERIC(14, 2),
  clicks INTEGER,
  impressions INTEGER,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT polinarepik_direct_stats_date_campaign_id_key UNIQUE (date, campaign_id)
);

CREATE INDEX IF NOT EXISTS polinarepik_direct_stats_date_idx ON polinarepik_direct_stats (date);
