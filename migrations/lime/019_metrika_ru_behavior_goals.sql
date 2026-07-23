-- Доп. поведение и post-click цели RU-Метрики: время на сайте (взвешено по визитам) +
-- цели «просмотр карточки»/«смотреть образ». Пишет sync/lime_ru_metrika.py.
-- Идемпотентно (ADD COLUMN IF NOT EXISTS) — применяется при каждом прогоне синка.
ALTER TABLE lime_metrika_campaign_ru
  ADD COLUMN IF NOT EXISTS duration_w numeric NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS card_view  bigint  NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS look_image bigint  NOT NULL DEFAULT 0;
