-- Дневной брендовый спрос LIME: Σ 5 фраз за день, широкое соответствие, регион ru.
-- Wordstat хранит дневную детализацию только 60 дней → таблица всегда покрывает
-- лишь скользящее окно; вся история живёт в недельной lime_wordstat_demand.
CREATE TABLE IF NOT EXISTS lime_wordstat_demand_daily (
  day        date NOT NULL,
  region     text NOT NULL DEFAULT 'ru',
  frequency  integer NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (day, region)
);

-- RLS deny-all с рождения таблицы (как lime_wordstat_demand): политик нет намеренно —
-- читает только сервер Panda-BI через Prisma (роль postgres, RLS не применяется),
-- публичный ключ PostgREST не должен видеть ни строки.
ALTER TABLE lime_wordstat_demand_daily ENABLE ROW LEVEL SECURITY;
