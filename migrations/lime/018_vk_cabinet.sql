-- Метка кабинета VK (login аккаунта) — различает несколько VK-кабинетов LIME в одной таблице
-- (Основной/Kids/Man/APP). ad_plan_id глобально уникальны между кабинетами → PK не меняем.
-- Человекочитаемые имена (Kids/Man/APP) — маппинг login→имя на слое дашборда.
ALTER TABLE lime_vk_ads_stats ADD COLUMN IF NOT EXISTS cabinet text;
