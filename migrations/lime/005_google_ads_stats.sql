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
-- Условно: ALTER ... ENABLE RLS берёт ACCESS EXCLUSIVE lock даже если RLS уже включён,
-- а миграции применяются при КАЖДОМ прогоне синка → на живой записи ловили statement timeout.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'public' AND c.relname = 'lime_google_ads_stats' AND c.relrowsecurity)
  THEN
    EXECUTE 'ALTER TABLE lime_google_ads_stats ENABLE ROW LEVEL SECURITY';
  END IF;
END $$;