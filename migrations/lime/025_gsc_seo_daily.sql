-- Дневной срез Google Search Console (KZ, GCC) — тот же «качественный бренд», что и в
-- недельной lime_gsc_seo, но без свёртки в понедельник: дневная детализация нужна
-- переключателю «Недели/Дни» блока «Брендовый спрос» на Google-вкладках.
--
-- Отдельная таблица, а не колонка grain у недельной: недельный ряд живёт ~16 месяцев
-- (глубина GSC), дневной — короткое окно графика, и упаковывать два зерна в один
-- первичный ключ значило бы каждый раз фильтровать по нему в каждом запросе.
--
-- country: у KZ пусто (регион целиком, гео-фильтр в запросе), у GCC — страна витрины
-- (ae → ОАЭ), значения совпадают с lime_gsc_seo.country и lime_stats.country.
CREATE TABLE IF NOT EXISTS lime_gsc_seo_daily (
  day         date NOT NULL,
  region      text NOT NULL,
  country     text NOT NULL DEFAULT '',
  clicks      integer NOT NULL,
  impressions integer NOT NULL,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (day, region, country)
);

-- RLS deny-all с рождения таблицы (как lime_brand_seo_daily): политик нет намеренно —
-- читает только сервер Panda-BI через Prisma (роль postgres, RLS не применяется),
-- публичный ключ PostgREST не должен видеть ни строки.
ALTER TABLE lime_gsc_seo_daily ENABLE ROW LEVEL SECURITY;
