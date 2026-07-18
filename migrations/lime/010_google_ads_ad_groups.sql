-- Справочник групп объявлений Google Ads: ad_group_id → кампания.
-- Нужен для склейки визитов Метрики с кампаниями: у поисковых кампаний KZ utm_campaign
-- статичный ('g'), а разрешающий id приходит в utm_content. Пишет Google Ads Script через
-- /api/ingest/google-ads, как и статистику.
--
-- Номер 010, не 009: на момент реализации 009 уже занят миграцией 009_gsc_country.sql
-- (см. commit 8aa6c65 "номер 008 занят гео-расходом Google Ads" — тот же паттерн коллизии
-- номеров между параллельными сессиями).
--
-- ⚠️ ЭТА МИГРАЦИЯ ПЕРЕКРЫТА 012: там таблица переименована в lime_google_ads_entities
-- (справочник расширен с групп на любые сущности кампании). Оставлена для истории и для
-- чистой БД, где 010 создаёт таблицу, а 012 её переименовывает. На уже мигрированной БД
-- создавать заново НЕЛЬЗЯ: скрипт применения гоняет все миграции каждый прогон, и голый
-- CREATE IF NOT EXISTS каждый раз плодил бы пустой дубль рядом с рабочей таблицей.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.tables
                 WHERE table_schema = 'public' AND table_name = 'lime_google_ads_entities')
  THEN
    CREATE TABLE IF NOT EXISTS lime_google_ads_ad_groups (
      ad_group_id   text PRIMARY KEY,
      campaign_id   text NOT NULL,
      campaign_name text,
      region        text NOT NULL DEFAULT 'kz',
      updated_at    timestamptz NOT NULL DEFAULT now()
    );
    -- Условно: ALTER ... ENABLE RLS берёт ACCESS EXCLUSIVE lock даже если RLS уже включён,
    -- а миграции применяются при КАЖДОМ прогоне синка → на живой записи ловили timeout.
    IF NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname = 'public' AND c.relname = 'lime_google_ads_ad_groups'
                     AND c.relrowsecurity)
    THEN
      EXECUTE 'ALTER TABLE lime_google_ads_ad_groups ENABLE ROW LEVEL SECURITY';
    END IF;
  END IF;
END $$;
