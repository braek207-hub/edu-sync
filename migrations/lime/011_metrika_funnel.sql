-- Поведенческая воронка Яндекс.Метрики для строк, собираемых напрямую из Stat API
-- (регион kz_metrika, спека 2026-07-18-lime-kz-metrika-design.md).
-- bounce_rate/page_depth хранятся ПОСТРОЧНОЙ ставкой (как polinarepik_metrica_sources);
-- взвешивание по визитам делает хендлер на чтении. У строк из MySQL/GCC остаются NULL —
-- по этому признаку движок скрывает колонки там, где данных нет.
ALTER TABLE lime_stats ADD COLUMN IF NOT EXISTS bounce_rate      numeric;
ALTER TABLE lime_stats ADD COLUMN IF NOT EXISTS page_depth       numeric;
ALTER TABLE lime_stats ADD COLUMN IF NOT EXISTS cart_reaches     integer;
ALTER TABLE lime_stats ADD COLUMN IF NOT EXISTS checkout_reaches integer;
