-- Polina Repik: поведенческие метрики (отказы, глубина) + цели воронки
-- (добавление в корзину, инициация оформления) на clientID-визитах Метрики.
-- bounce_rate в процентах (0..100, как отдаёт Метрика); cart/checkout — достижения целей
-- (512437503 / 371515249). source_detail вынесен в source-level (005), т.к. SourceEngineName
-- несовместим с clientID в API Метрики.
ALTER TABLE polinarepik_metrica_visits
  ADD COLUMN IF NOT EXISTS bounce_rate      NUMERIC,
  ADD COLUMN IF NOT EXISTS page_depth       NUMERIC,
  ADD COLUMN IF NOT EXISTS cart_reaches     INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS checkout_reaches INTEGER NOT NULL DEFAULT 0;
