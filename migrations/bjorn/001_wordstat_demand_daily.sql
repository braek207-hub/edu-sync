-- BJORN «Спрос рынка», дневная детализация: категорийный спрос Wordstat ПО-ФРАЗНО
-- (зеркало недельной bjorn_wordstat_demand; та создана мимо репо — DDL восстановлен
-- в EduDash supabase/migrations/_from-prod/20260717105720). Wordstat хранит дневные
-- точки только 60 дней → скользящее окно; история — в недельной таблице.
-- Пишет sync_bjorn_demand.py → sync_bjorn_wordstat_demand_daily.
CREATE TABLE IF NOT EXISTS bjorn_wordstat_demand_daily (
  day        date    NOT NULL,
  region     text    NOT NULL DEFAULT 'ru',
  phrase     text    NOT NULL,
  frequency  integer NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (day, region, phrase)
);

-- RLS deny-all (стандарт после аудита 2026-08-02): читает только сервер (Prisma).
ALTER TABLE bjorn_wordstat_demand_daily ENABLE ROW LEVEL SECURITY;
