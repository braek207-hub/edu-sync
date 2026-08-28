-- Циклы покупки приложения LIME: сколько дней проходит от установки до первой покупки
-- и между покупками. Пишет sync/lime_appmetrica.py (он же создаёт таблицы сам —
-- CREATE TABLE IF NOT EXISTS в _ensure_cycle_tables, применяется каждым прогоном).
--
-- Лаг хранится ТОЧНЫМ числом дней, а не бакетом: медиана и перцентили тогда считаются
-- точно на любом уровне свёртки (канал, кампания, произвольный период). Бакеты дали бы
-- только приближение, а вопрос — ровно про сдвиг медианы между периодами.
--
-- Грань привязки — ПЕРВАЯ установка устройства (как у месячных когорт lime_app_cohorts),
-- поэтому cohort_date, а не «день события».
CREATE TABLE IF NOT EXISTS lime_app_cycle_daily (
  cohort_date date NOT NULL,
  publisher   text NOT NULL,
  detail      text NOT NULL DEFAULT '',
  campaign_id text NOT NULL DEFAULT '',
  -- 'install_p1' | 'p1_p2' | 'p2_p3'
  step        text NOT NULL,
  days        integer NOT NULL,
  devices     integer NOT NULL DEFAULT 0,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (cohort_date, publisher, detail, campaign_id, step, days)
);
CREATE INDEX IF NOT EXISTS idx_lime_app_cycle_daily_date_step
  ON lime_app_cycle_daily (cohort_date, step);
CREATE INDEX IF NOT EXISTS idx_lime_app_cycle_daily_campaign
  ON lime_app_cycle_daily (campaign_id, cohort_date);

-- Знаменатели: размер когорты первых установок и сколько её устройств дошло до 1/2/3 покупки.
-- Без него «доля купивших за N дней» не посчитать: в lime_app_installs.installs дневной
-- дедуп (устройство считается в каждый день, когда ставило), а здесь нужна первая установка.
CREATE TABLE IF NOT EXISTS lime_app_cycle_cohort (
  cohort_date date NOT NULL,
  publisher   text NOT NULL,
  detail      text NOT NULL DEFAULT '',
  campaign_id text NOT NULL DEFAULT '',
  devices     integer NOT NULL DEFAULT 0,
  buyers1     integer NOT NULL DEFAULT 0,
  buyers2     integer NOT NULL DEFAULT 0,
  buyers3     integer NOT NULL DEFAULT 0,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (cohort_date, publisher, detail, campaign_id)
);
CREATE INDEX IF NOT EXISTS idx_lime_app_cycle_cohort_date
  ON lime_app_cycle_cohort (cohort_date);

-- ENABLE RLS берёт ACCESS EXCLUSIVE lock даже если RLS уже включён → условно (как 017, 018).
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'public' AND c.relname = 'lime_app_cycle_daily' AND c.relrowsecurity)
  THEN
    EXECUTE 'ALTER TABLE lime_app_cycle_daily ENABLE ROW LEVEL SECURITY';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'public' AND c.relname = 'lime_app_cycle_cohort' AND c.relrowsecurity)
  THEN
    EXECUTE 'ALTER TABLE lime_app_cycle_cohort ENABLE ROW LEVEL SECURITY';
  END IF;
END $$;
