-- Клиенты когорты (paidClientCount) по дате визита — оплатившие клиенты визитов недели.
-- Без деления new/loyal: Roistat не отдаёт newClientCount/repeatClientCount (метрики невалидны,
-- проверено пробой). NULL = источник не знает. Заполняет только sync/lime_kz_roistat.py.
ALTER TABLE lime_stats ADD COLUMN IF NOT EXISTS cohort_clients integer;
