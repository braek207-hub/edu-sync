-- Дневные брендовые SEO-клики LIME: Σ бренд-запросов по обоим хостам за день.
-- query-analytics Вебмастера отдаёт статистику только за последние 2 недели
-- (лаг ~2 дня) → бэкфилла глубже нет, история накапливается ежедневным кроном;
-- вся длинная история живёт в недельной lime_brand_seo (импорт Павла + API).
CREATE TABLE IF NOT EXISTS lime_brand_seo_daily (
  day         date PRIMARY KEY,
  clicks      integer NOT NULL,
  -- NOT NULL в отличие от недельной lime_brand_seo: там nullable из-за файлового
  -- импорта истории без показов, дневная пишется только из API — показы есть всегда.
  impressions integer NOT NULL,
  source      text NOT NULL DEFAULT 'webmaster',
  updated_at  timestamptz NOT NULL DEFAULT now()
);

-- RLS deny-all с рождения таблицы (как lime_wordstat_demand_daily): политик нет намеренно —
-- читает только сервер Panda-BI через Prisma (роль postgres, RLS не применяется),
-- публичный ключ PostgREST не должен видеть ни строки.
ALTER TABLE lime_brand_seo_daily ENABLE ROW LEVEL SECURITY;
