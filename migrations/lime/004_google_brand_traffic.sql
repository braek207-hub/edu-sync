-- Google-половина брендового трафика LIME: спрос (Google Trends) + SEO-клики (Search Console).
-- Отдельные таблицы, НЕ смешиваются с Яндексом (lime_wordstat_demand / lime_brand_seo):
-- разные аудитории и разные SERP, суммировать нельзя. Недельная гранулярность (Пн ISO), регион ru.

-- Брендовый спрос Google: недельная частотность бренд-фраз.
-- frequency — абсолютная ОЦЕНКА объёма (Trends не даёт точный счётчик как Wordstat; источник trendsmcp).
CREATE TABLE IF NOT EXISTS lime_gtrends_demand (
  week_start date NOT NULL,
  region     text NOT NULL DEFAULT 'ru',
  frequency  integer NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (week_start, region)
);

-- Брендовые SEO-клики Google (Search Console): Σ бренд-запросов по обоим сайтам за неделю.
CREATE TABLE IF NOT EXISTS lime_gsc_seo (
  week_start  date NOT NULL,
  region      text NOT NULL DEFAULT 'ru',
  clicks      integer NOT NULL,
  impressions integer,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (week_start, region)
);
