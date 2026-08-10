-- EDU «Спрос рынка», дневная детализация: спрос из Wordstat ПО-ФРАЗНО (зеркало
-- недельной edu_wordstat_demand). Wordstat хранит дневные точки только 60 дней →
-- таблица покрывает скользящее окно; вся история остаётся в недельной таблице.
-- Пишет sync_edu_demand.py → sync_edu_wordstat_demand_daily.
CREATE TABLE IF NOT EXISTS edu_wordstat_demand_daily (
  day        date    NOT NULL,
  region     text    NOT NULL DEFAULT 'ru',  -- ru | msk (rest считается на чтении)
  phrase     text    NOT NULL,               -- отдельная фраза (не сумма)
  frequency  integer NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (day, region, phrase)
);

-- RLS deny-all (стандарт после аудита 2026-08-02): политик нет намеренно — читает
-- только сервер через Prisma (роль postgres), публичный ключ PostgREST не видит ни строки.
ALTER TABLE edu_wordstat_demand_daily ENABLE ROW LEVEL SECURITY;
