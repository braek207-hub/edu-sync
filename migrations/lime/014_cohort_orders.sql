-- Когортные заказы Роистата по дате ВИЗИТА (накопительно, дозревают). NULL = источник не знает:
-- заполняет только sync/lime_kz_roistat.py, у остальных срезов остаётся пустым, и хендлер
-- показывает «—» (паттерн net_* из миграции 013). cohort_new+cohort_repeat=cohort_orders.
ALTER TABLE lime_stats ADD COLUMN IF NOT EXISTS cohort_orders integer;
ALTER TABLE lime_stats ADD COLUMN IF NOT EXISTS cohort_revenue numeric;
ALTER TABLE lime_stats ADD COLUMN IF NOT EXISTS cohort_new_sales integer;
ALTER TABLE lime_stats ADD COLUMN IF NOT EXISTS cohort_repeat_sales integer;
