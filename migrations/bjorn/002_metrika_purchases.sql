-- Склейка заказов Bitrix с атрибуцией Метрики по purchaseID (= номер заказа Bitrix),
-- по образцу polinarepik_metrica_purchases (migrations/polinarepik/003_attribution.sql).
-- Джойн с bjorn_orders по order_id; client_id — резерв для склейки по визитам,
-- когда заказа нет в ecommerce (телефон/другой браузер).

CREATE TABLE IF NOT EXISTS bjorn_metrika_purchases (
  order_id TEXT PRIMARY KEY,
  purchase_date DATE NOT NULL,
  client_id TEXT NOT NULL DEFAULT '',
  traffic_source TEXT NOT NULL DEFAULT '',
  source_engine TEXT NOT NULL DEFAULT '',
  utm_source TEXT NOT NULL DEFAULT '',
  utm_medium TEXT NOT NULL DEFAULT '',
  utm_campaign TEXT NOT NULL DEFAULT '',
  purchases INTEGER NOT NULL DEFAULT 1,
  revenue NUMERIC(14, 2) NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE bjorn_metrika_purchases ENABLE ROW LEVEL SECURITY;

CREATE INDEX IF NOT EXISTS bjorn_metrika_purchases_date_idx
  ON bjorn_metrika_purchases (purchase_date);
CREATE INDEX IF NOT EXISTS bjorn_metrika_purchases_client_idx
  ON bjorn_metrika_purchases (client_id);
