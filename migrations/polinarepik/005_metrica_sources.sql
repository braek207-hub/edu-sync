-- Polina Repik: source-level срез Метрики для «Источник детально» (Канал).
-- source_detail = ym:s:lastsignSourceEngineName (Яндекс: Директ / Google / Яндекс / ВКонтакте…).
-- Без clientID (SourceEngineName с ним несовместим). Хендлер мапит source_detail на строки
-- по (traffic_source, utm_source, utm_medium, utm_campaign).
CREATE TABLE IF NOT EXISTS polinarepik_metrica_sources (
  id SERIAL PRIMARY KEY,
  date DATE NOT NULL,
  traffic_source TEXT,
  source_detail TEXT,
  utm_source TEXT,
  utm_medium TEXT,
  utm_campaign TEXT DEFAULT '',
  visits INTEGER NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (date, traffic_source, source_detail, utm_source, utm_medium, utm_campaign)
);

CREATE INDEX IF NOT EXISTS polinarepik_metrica_sources_date_idx ON polinarepik_metrica_sources (date);
CREATE INDEX IF NOT EXISTS polinarepik_metrica_sources_key_idx
  ON polinarepik_metrica_sources (traffic_source, utm_source, utm_medium, utm_campaign);
