-- Сессии и покупки приложения LIME по ДАТЕ СОБЫТИЯ в разрезе источника и кампании
-- УСТАНОВКИ устройства. Пишет sync/lime_app_install_source.py (сам создаёт таблицу —
-- CREATE TABLE IF NOT EXISTS каждым прогоном).
--
-- Зачем: витрина PROCONTEXT подписывает сессии приложения без UTM источником установки
-- (`vk-ads-(ex.-mytarget)`, `yandex.direct` + medium `(not set)`), кампании у этих строк
-- нет вовсе. Здесь та же атрибуция, но с кампанией: дашборд делит строку витрины на
-- дочерние по кампании установки пропорционально этой витрине, итог остаётся за PROCONTEXT.
--
-- Атрибуция = ПЕРВАЯ установка устройства в окне (как у когорт); установка старше окна
-- или без партнёра → publisher 'unknown', campaign_id '' (остаток «без кампании»).
CREATE TABLE IF NOT EXISTS lime_app_install_source_daily (
  date        date NOT NULL,
  publisher   text NOT NULL,
  campaign_id text NOT NULL DEFAULT '',
  sessions    integer NOT NULL DEFAULT 0,
  devices     integer NOT NULL DEFAULT 0,
  orders      integer NOT NULL DEFAULT 0,
  revenue     numeric(14,2) NOT NULL DEFAULT 0,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (date, publisher, campaign_id)
);
CREATE INDEX IF NOT EXISTS idx_lime_app_install_source_daily_campaign
  ON lime_app_install_source_daily (campaign_id, date);
