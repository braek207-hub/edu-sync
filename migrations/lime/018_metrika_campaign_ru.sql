-- RU-срез Яндекс.Метрики (счётчик 23504302) по каналу/кампании — ПОВЕДЕНИЕ + POST-CLICK
-- воронка. Отдельная витрина (НЕ lime_stats): дашборд обогащает ею строки витрины PROCONTEXT
-- по (date, campaign_id) для рекламы и (date, channel, subchannel) для прочих, без задвоения
-- визитов (визиты остаются за PROCONTEXT). Post-view — за Медиаметрикой, сюда не входит.
-- Пишет edu-sync (sync/lime_ru_metrika.py). bounce_w/depth_w — взвешены по визитам (Σ×visits),
-- аддитивны; ставка на чтении = w/visits. Применяется при каждом прогоне синка (idempotent).
CREATE TABLE IF NOT EXISTS lime_metrika_campaign_ru (
  date          date NOT NULL,
  channel       text NOT NULL DEFAULT '',
  subchannel    text NOT NULL DEFAULT '',
  traffic_type  text,
  campaign_id   text NOT NULL DEFAULT '',
  campaign_name text NOT NULL DEFAULT '',
  visits        bigint NOT NULL DEFAULT 0,
  users         bigint NOT NULL DEFAULT 0,
  new_users     bigint NOT NULL DEFAULT 0,
  bounce_w      numeric NOT NULL DEFAULT 0,
  depth_w       numeric NOT NULL DEFAULT 0,
  cart          bigint NOT NULL DEFAULT 0,
  checkout      bigint NOT NULL DEFAULT 0,
  orders        bigint NOT NULL DEFAULT 0,
  revenue       numeric NOT NULL DEFAULT 0,
  updated_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (date, channel, subchannel, campaign_id, campaign_name)
);
-- Джойн дашборда идёт по (date, campaign_id) — индекс под него.
CREATE INDEX IF NOT EXISTS idx_lime_metrika_ru_date_campaign
  ON lime_metrika_campaign_ru (date, campaign_id);
-- ENABLE RLS берёт ACCESS EXCLUSIVE lock даже если RLS уже включён → условно (как 017).
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'public' AND c.relname = 'lime_metrika_campaign_ru' AND c.relrowsecurity)
  THEN
    EXECUTE 'ALTER TABLE lime_metrika_campaign_ru ENABLE ROW LEVEL SECURITY';
  END IF;
END $$;
