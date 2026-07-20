-- Заявки когорты (leadCount) по дате визита — рядом с cohort_orders (оплаченные продажи).
-- NULL = источник не знает (не-Roistat). Заполняет только sync/lime_kz_roistat.py.
-- Когорта: cohort_leads=заявки визитов, cohort_orders=оплаченные продажи, new+repeat=orders.
ALTER TABLE lime_stats ADD COLUMN IF NOT EXISTS cohort_leads integer;
