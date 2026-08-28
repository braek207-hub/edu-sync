-- Все цели счётчика LIME по дню/каналу/кампании. Пишет sync/lime_metrika_goals.py
-- (он же создаёт таблицы сам — _DDL применяется каждым прогоном).
--
-- ДЛИННАЯ витрина, строка = (день × разрез × цель). Колонка на цель (как четыре цели
-- e-com воронки в lime_metrika_campaign_ru) не масштабируется: в счётчике живут
-- регистрация, авторизация, цели Mindbox и автоцели Директа, их состав меняется
-- без нашего участия.
--
-- Нули не пишутся: десятки целей × сотни разрезов дали бы декартов продукт из строк,
-- которые читатель всё равно трактует как «нет данных».
CREATE TABLE IF NOT EXISTS lime_metrika_goal_daily (
  date         date NOT NULL,
  channel      text NOT NULL,
  subchannel   text NOT NULL DEFAULT '',
  traffic_type text NOT NULL DEFAULT '',
  campaign_id  text NOT NULL DEFAULT '',
  goal_id      text NOT NULL,
  reaches      integer NOT NULL DEFAULT 0,
  updated_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (date, channel, subchannel, campaign_id, goal_id)
);
CREATE INDEX IF NOT EXISTS idx_lime_metrika_goal_daily_goal
  ON lime_metrika_goal_daily (goal_id, date);
CREATE INDEX IF NOT EXISTS idx_lime_metrika_goal_daily_campaign
  ON lime_metrika_goal_daily (campaign_id, date);

-- Справочник целей: id → имя. Без него витрина — таблица чисел с id вместо названий.
-- source ('auto' | 'user' | ...) отделяет автоцели Директа: их десятки, дашборд прячет
-- их по умолчанию.
CREATE TABLE IF NOT EXISTS lime_metrika_goals (
  goal_id        text PRIMARY KEY,
  name           text NOT NULL DEFAULT '',
  type           text NOT NULL DEFAULT '',
  source         text NOT NULL DEFAULT '',
  is_retargeting boolean NOT NULL DEFAULT false,
  synced_at      timestamptz NOT NULL DEFAULT now()
);

-- ENABLE RLS берёт ACCESS EXCLUSIVE lock даже если RLS уже включён → условно (как 017, 018, 027).
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'public' AND c.relname = 'lime_metrika_goal_daily' AND c.relrowsecurity)
  THEN
    EXECUTE 'ALTER TABLE lime_metrika_goal_daily ENABLE ROW LEVEL SECURITY';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'public' AND c.relname = 'lime_metrika_goals' AND c.relrowsecurity)
  THEN
    EXECUTE 'ALTER TABLE lime_metrika_goals ENABLE ROW LEVEL SECURITY';
  END IF;
END $$;
