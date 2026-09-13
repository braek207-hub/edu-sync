-- Покупки приложения LIME по (дата УСТАНОВКИ устройства × дата ПОКУПКИ) в разрезе
-- партнёра/кампании установки. Пишет sync/lime_appmetrica.py (сам создаёт таблицу —
-- CREATE TABLE IF NOT EXISTS каждым прогоном, DELETE+INSERT всего окна 7 месяцев).
--
-- Зачем: «Заказы/Выручка установок» за период дашборд считает как установки периода и их
-- покупки ДО КОНЦА периода (purchase_date <= to), а не пожизненную когорту. Дневная строка
-- lime_app_installs с накопительной суммой этого не даёт — нужна вторая ось даты.
--
-- Привязка = первая установка устройства в окне; покупки до дня установки отброшены.
CREATE TABLE IF NOT EXISTS lime_app_install_purchase_daily (
  install_date  date NOT NULL,
  purchase_date date NOT NULL,
  publisher     text NOT NULL,
  detail        text NOT NULL DEFAULT '',
  campaign_id   text NOT NULL DEFAULT '',
  orders        integer NOT NULL DEFAULT 0,
  revenue       numeric(14,2) NOT NULL DEFAULT 0,
  updated_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (install_date, purchase_date, publisher, detail, campaign_id)
);
CREATE INDEX IF NOT EXISTS idx_lime_app_install_purchase_daily_purchase
  ON lime_app_install_purchase_daily (purchase_date);
ALTER TABLE lime_app_install_purchase_daily ENABLE ROW LEVEL SECURITY;
