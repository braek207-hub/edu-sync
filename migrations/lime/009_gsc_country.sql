-- Страна внутри региона для брендового трафика Google (GCC = 6 стран Залива).
-- Страна берётся из dimension country в Search Console (гео пользователя), НЕ из домена:
-- пользователи Бахрейна видят в выдаче в основном ae./sa., по своему домену их почти нет.
-- Значение — русское название, как lime_stats.country (sync/gcc_channels.py), чтобы
-- селектор страны в дашборде был общим. KZ/RU пишут '' — регион целиком.
ALTER TABLE lime_gsc_seo ADD COLUMN IF NOT EXISTS country text NOT NULL DEFAULT '';

-- PK (week_start, region) → (week_start, region, country). Идемпотентно: перекладываем
-- только если ключ ещё старый (два поля). NULL в PK недопустим — отсюда DEFAULT ''.
DO $$
DECLARE
  pk_cols int;
BEGIN
  SELECT array_length(conkey, 1) INTO pk_cols
  FROM pg_constraint
  WHERE conrelid = 'lime_gsc_seo'::regclass AND contype = 'p';

  IF pk_cols = 2 THEN
    ALTER TABLE lime_gsc_seo DROP CONSTRAINT lime_gsc_seo_pkey;
    ALTER TABLE lime_gsc_seo ADD PRIMARY KEY (week_start, region, country);
  END IF;
END $$;
