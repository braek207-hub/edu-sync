-- Гео-расход Google Ads по странам (T4 дробления GCC). Строка = date × кампания × страна.
--
-- Модель: строки НЕПЕРЕСЕКАЮЩИЕСЯ срезы, а не «итог + детализация» — иначе SUM(cost) задвоит.
-- KZ/RU шлют country='' (одна строка на кампанию, как раньше) → для них PK эквивалентен старому.
-- GCC шлёт по строке на страну + строку country='' с остатком (показы без гео-привязки),
-- поэтому SUM по кампании = полный расход кабинета в обеих схемах.
ALTER TABLE lime_google_ads_stats ADD COLUMN IF NOT EXISTS country text NOT NULL DEFAULT '';

-- PK расширяем на country. Существующие строки получили country='' → уникальность не нарушена
-- (старый PK был уникален по 4 колонкам). ⚠️ ON CONFLICT в app/api/ingest/google-ads/route.ts
-- (lib/google-ads/stats.ts) обязан быть обновлён на этот же кортеж — иначе ingest падает 42P10.
ALTER TABLE lime_google_ads_stats DROP CONSTRAINT lime_google_ads_stats_pkey;
ALTER TABLE lime_google_ads_stats
  ADD CONSTRAINT lime_google_ads_stats_pkey
  PRIMARY KEY (date, region, customer_id, campaign_id, country);
