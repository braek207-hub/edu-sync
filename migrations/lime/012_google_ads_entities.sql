-- Справочник lime_google_ads_ad_groups → lime_google_ads_entities: расширяем с групп
-- объявлений на любую под-сущность кампании, которую может подставить шаблон отслеживания
-- в utm_content.
--
-- Причина: гипотеза "id в utm_content — это id группы объявлений" не подтвердилась. Реальные
-- id групп 12-значные, начинаются с 1-2 (например 193954649928), а Метрика шлёт id вида
-- 782935363650 / 813255959161 / 803404556573 — тоже 12 знаков, но диапазон 7-8, и различных
-- значений всего девять при живых визитах по кампаниям с настоящим campaign_id в
-- utm_campaign. Это не группа — вероятнее всего id объявления (макрос {creative}). Справочник
-- переименован в общий вид: колонка ad_group_id → entity_id, плюс kind ('ad_group' | 'ad'),
-- чтобы склейка резолвила id независимо от того, что именно подставляет шаблон.
--
-- Идемпотентно: скрипт применения (scripts/apply_lime_migrations.py) гоняет ВСЕ миграции
-- при каждом запуске, поэтому переименования обёрнуты проверками existence — без них повторный
-- запуск падает на "table already exists" / "column already exists". Переименования сохраняют
-- данные (30 строк, накопленных предыдущей версией скрипта в кабинете).
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = 'public' AND table_name = 'lime_google_ads_ad_groups')
     AND NOT EXISTS (SELECT 1 FROM information_schema.tables
                      WHERE table_schema = 'public' AND table_name = 'lime_google_ads_entities')
  THEN
    ALTER TABLE lime_google_ads_ad_groups RENAME TO lime_google_ads_entities;
  END IF;
END $$;

-- На случай первого применения в окружении без таблицы вовсе (например тестовая БД).
CREATE TABLE IF NOT EXISTS lime_google_ads_entities (
  entity_id     text PRIMARY KEY,
  campaign_id   text NOT NULL,
  campaign_name text,
  region        text NOT NULL DEFAULT 'kz',
  updated_at    timestamptz NOT NULL DEFAULT now()
);

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = 'lime_google_ads_entities'
               AND column_name = 'ad_group_id')
     AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                      WHERE table_schema = 'public' AND table_name = 'lime_google_ads_entities'
                        AND column_name = 'entity_id')
  THEN
    ALTER TABLE lime_google_ads_entities RENAME COLUMN ad_group_id TO entity_id;
  END IF;
END $$;

ALTER TABLE lime_google_ads_entities
  ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'ad_group';

ALTER TABLE lime_google_ads_entities ENABLE ROW LEVEL SECURITY;
