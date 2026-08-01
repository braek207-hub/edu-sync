-- Справочник сущностей VK Рекламы: id ГРУППЫ объявлений → id КАМПАНИИ (ad_plan).
--
-- Зачем: установки приложения в AppMetrica несут в click_url_parameters макрос `c` — это id
-- ГРУППЫ объявлений VK, а расход в lime_vk_ads_stats лежит на грани КАМПАНИИ (ad_plan_id,
-- в API v2 «campaigns» — это группы, кампании интерфейса = ad_plans). Без справочника
-- установки VK не связываются с расходом и CPI/ROAS по VK-кампаниям не считаются.
--
-- kind: 'ad_group' — обычная строка (группа → её кампания); 'ad_plan' — ссылка кампании на
-- себя, чтобы резолв срабатывал и если макрос `c` подставит id кампании. Тот же приём, что
-- kind в lime_google_ads_entities (миграция 012).
--
-- Строки НИКОГДА не удаляются: удалённая в кабинете группа обязана остаться, иначе
-- исторические установки перестанут резолвиться.
CREATE TABLE IF NOT EXISTS lime_vk_entities (
  entity_id  text PRIMARY KEY,
  kind       text NOT NULL,
  ad_plan_id text NOT NULL,
  cabinet    text,
  name       text,
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Условно: ALTER ... ENABLE RLS берёт ACCESS EXCLUSIVE lock даже если RLS уже включён,
-- а миграции применяются при КАЖДОМ прогоне синка → на живой записи ловили statement timeout.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'public' AND c.relname = 'lime_vk_entities'
                   AND c.relrowsecurity)
  THEN
    EXECUTE 'ALTER TABLE lime_vk_entities ENABLE ROW LEVEL SECURITY';
  END IF;
END $$;
