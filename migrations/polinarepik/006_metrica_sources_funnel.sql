-- Polina Repik: воронка (отказы/глубина/корзина/чекаут) на source-level срезе Метрики.
-- bounce_rate в процентах (0..100, как отдаёт Метрика); cart/checkout — достижения целей
-- (512437503 / 371515249). Совместимы с lastsignSourceEngineName (без clientID) — иначе
-- чем clientID-визиты (004), но те же цели/поведение, на source-уровне для колонок воронки
-- дашборда Polina (визиты дашборда идут из metrica_sources, не из metrica_visits).
ALTER TABLE polinarepik_metrica_sources
  ADD COLUMN IF NOT EXISTS bounce_rate      NUMERIC,
  ADD COLUMN IF NOT EXISTS page_depth       NUMERIC,
  ADD COLUMN IF NOT EXISTS cart_reaches     INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS checkout_reaches INTEGER NOT NULL DEFAULT 0;
