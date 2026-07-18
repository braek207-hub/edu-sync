-- Дробление региона GCC по странам Залива (ae/bh/kw/sa/qa/om) как измерение внутри region='gcc'.
-- country заполняет ТОЛЬКО GCC-синк; RU/KZ и прочие регионы → NULL. GCC-тотал = Σ стран + NULL-строки
-- (источники без гео-разбивки, напр. расход Meta из TW summary — Фаза 2).
ALTER TABLE lime_stats ADD COLUMN IF NOT EXISTS country text;

-- Ускоряет срез дашборда по стране внутри GCC (частичный — country заполнен только там).
CREATE INDEX IF NOT EXISTS lime_stats_region_country_date_idx
  ON lime_stats (region, country, date)
  WHERE country IS NOT NULL;

-- lime_google_ads_stats.country НЕ добавляем здесь: гео-расход (T4) требует расширения PK
-- (date, region, customer_id, campaign_id, country), а PK жёстко связан с ON CONFLICT в
-- app/api/ingest/google-ads/route.ts. Менять PK отдельно от роута = поломка живого KZ-инжеста
-- (ошибка 42P10). Обе правки идут одной миграцией 007 вместе с гео-запросом в Script.
