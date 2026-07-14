-- Брендовый трафик LIME: спрос (Wordstat) + SEO-клики (Вебмастер).
-- Единая недельная тотал-грануляр (Пн ISO). Paid Brand берётся из lime_direct_stats на чтении.

-- Брендовый спрос: Σ 5 фраз за неделю, широкое соответствие, регион ru.
CREATE TABLE IF NOT EXISTS lime_wordstat_demand (
  week_start date NOT NULL,
  region     text NOT NULL DEFAULT 'ru',
  frequency  integer NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (week_start, region)
);

-- Брендовые SEO-клики: Σ бренд-запросов по обоим хостам за неделю.
-- source: 'webmaster' (API, вперёд) | 'file' (импорт истории Павла 2023-2025).
CREATE TABLE IF NOT EXISTS lime_brand_seo (
  week_start  date NOT NULL,
  clicks      integer NOT NULL,
  impressions integer,
  source      text NOT NULL DEFAULT 'webmaster',
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (week_start)
);
