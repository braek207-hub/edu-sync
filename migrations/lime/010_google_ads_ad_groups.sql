-- Справочник групп объявлений Google Ads: ad_group_id → кампания.
-- Нужен для склейки визитов Метрики с кампаниями: у поисковых кампаний KZ utm_campaign
-- статичный ('g'), а id группы приходит в utm_content. Пишет Google Ads Script через
-- /api/ingest/google-ads (поле ad_groups), как и статистику.
--
-- Номер 010, не 009: на момент реализации 009 уже занят миграцией 009_gsc_country.sql
-- (см. commit 8aa6c65 "номер 008 занят гео-расходом Google Ads" — тот же паттерн коллизии
-- номеров между параллельными сессиями).
CREATE TABLE IF NOT EXISTS lime_google_ads_ad_groups (
  ad_group_id   text PRIMARY KEY,
  campaign_id   text NOT NULL,
  campaign_name text,
  region        text NOT NULL DEFAULT 'kz',
  updated_at    timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE lime_google_ads_ad_groups ENABLE ROW LEVEL SECURITY;
