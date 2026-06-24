-- Настройки кампаний LIME из Яндекс Директа (синк lime_direct.py, одна строка на кампанию).
CREATE TABLE IF NOT EXISTS lime_campaign_settings (
  campaign_id   TEXT PRIMARY KEY,
  campaign_name TEXT,
  settings      JSONB NOT NULL DEFAULT '{}',
  synced_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS lime_campaign_settings_synced_at
  ON lime_campaign_settings (synced_at);
