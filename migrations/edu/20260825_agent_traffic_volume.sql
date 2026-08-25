-- migrations/edu/20260825_agent_traffic_volume.sql
-- Средний объём трафика (0–100) в витрине фактов агента: мера недобора показов.
-- Доля выкупа (auction_win_share) остаётся пустой — поля для неё в Reports API
-- нет, наполнялась она только выгрузкой интерфейса эпохи GAS.
ALTER TABLE edu_agent_facts
  ADD COLUMN IF NOT EXISTS avg_traffic_vol DOUBLE PRECISION NOT NULL DEFAULT 0;
