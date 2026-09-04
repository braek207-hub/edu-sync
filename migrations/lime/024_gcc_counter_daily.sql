-- Сверочная витрина счётчиков GCC: Яндекс.Метрика 98232701 (визиты/пользователи; ecommerce
-- на счётчике НЕ настроен — заказы всегда NULL) и GA4 417919368 (ecommerce-заказы/выручка).
-- Дашборд обогащает gcc-строки lime_stats вторым источником по (date, country, campaign_id)
-- с фолбэком (date, country, channel, subchannel) — как RU (lime_metrika_campaign_ru).
-- Пишет sync/lime_gcc_counters.py. Идемпотентно.
CREATE TABLE IF NOT EXISTS lime_gcc_counter_daily (
  date          date NOT NULL,
  source        text NOT NULL,            -- 'metrika' | 'ga4'
  country       text NOT NULL,            -- ОАЭ / Саудовская Аравия / Катар / Кувейт / Оман
  channel       text NOT NULL DEFAULT '',
  subchannel    text NOT NULL DEFAULT '',
  campaign_id   text NOT NULL DEFAULT '',
  campaign_name text NOT NULL DEFAULT '',
  visits        numeric NOT NULL DEFAULT 0,
  users         numeric NOT NULL DEFAULT 0,
  orders        numeric NOT NULL DEFAULT 0,
  -- GA4 purchaseRevenue в валюте property; сверено с TW W32 (287/3.56M против 349/4.64M ₽) —
  -- масштаб рублёвый, конвертация не нужна.
  revenue       numeric NOT NULL DEFAULT 0
);

-- Платность канала источника — тем же классификатором, что и витрина (map_metrika_channel /
-- map_ga4_channel). Без неё лист сверки не мог разложить Метрику на ORG/PAID и брал платность
-- по имени канала, теряя остаток креста Stat API.
ALTER TABLE lime_gcc_counter_daily
  ADD COLUMN IF NOT EXISTS traffic_type text NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS lime_gcc_counter_daily_date_source
  ON lime_gcc_counter_daily (date, source);

ALTER TABLE lime_gcc_counter_daily ENABLE ROW LEVEL SECURITY;
