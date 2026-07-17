-- Статистика Google Ads (через Google Ads Script → ingest /api/ingest/google-ads, без API/заявки).
-- Паритет с кабинетом Директа (lime_direct_stats) + Google-специфика: post-view + видео.
-- Пишет приложение (ingest-эндпоинт), не edu-sync. Расход в валюте аккаунта (KZT для KZ),
-- конверсия в рубли — на слое чтения. Применено в проде через Supabase MCP; здесь для истории.
CREATE TABLE IF NOT EXISTS lime_google_ads_stats (
  date            date NOT NULL,
  region          text NOT NULL DEFAULT 'kz',
  customer_id     text NOT NULL,
  campaign_id     text NOT NULL,
  campaign_name   text,
  campaign_type   text,
  impressions     bigint NOT NULL DEFAULT 0,
  clicks          bigint NOT NULL DEFAULT 0,
  cost            numeric NOT NULL DEFAULT 0,
  currency        text,
  video_views     bigint NOT NULL DEFAULT 0,
  video_view_rate double precision,
  avg_cpv         numeric,
  conversions     jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (date, region, customer_id, campaign_id)
);
ALTER TABLE lime_google_ads_stats ENABLE ROW LEVEL SECURITY;
