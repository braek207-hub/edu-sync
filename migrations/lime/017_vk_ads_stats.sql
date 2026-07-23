-- Статистика кабинета VK Реклама (ads.vk.com) → канал VK, регион ru.
-- Паритет с lime_direct_stats: расход/клики/показы + конверсии по типам (jsonb).
-- Пишет edu-sync (sync/lime_vk_ads.py). Валюта RUB, spent как в кабинете (без НДС).
-- Применяется при каждом прогоне синка (idempotent).
CREATE TABLE IF NOT EXISTS lime_vk_ads_stats (
  date          date NOT NULL,
  region        text NOT NULL DEFAULT 'ru',
  campaign_id   text NOT NULL,
  campaign_name text,
  objective     text,
  status        text,
  shows         bigint NOT NULL DEFAULT 0,
  clicks        bigint NOT NULL DEFAULT 0,
  spent         numeric NOT NULL DEFAULT 0,
  goals_total   bigint NOT NULL DEFAULT 0,
  vk_result     bigint NOT NULL DEFAULT 0,
  conversions   jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (date, campaign_id)
);
-- ENABLE RLS берёт ACCESS EXCLUSIVE lock даже если RLS уже включён (statement timeout на
-- живой записи) → включаем условно, как в 005_google_ads_stats.sql.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'public' AND c.relname = 'lime_vk_ads_stats' AND c.relrowsecurity)
  THEN
    EXECUTE 'ALTER TABLE lime_vk_ads_stats ENABLE ROW LEVEL SECURITY';
  END IF;
END $$;
