-- Кэш access_token VK Рекламы (ads.vk.com) между прогонами синка.
--
-- Зачем: токен VK живёт ~24ч, а синк гоняет несколько раз в сутки (крон + ручные
-- dispatch). Выпуск нового токена на КАЖДЫЙ прогон копит активные токены до лимита
-- VK (5 на пару client_id+пользователь) → 403 token_limit_exceeded. Раньше при этой
-- ошибке код отзывал (token/delete) ВСЕ токены клиента — а приложением (client_id)
-- пользуется и подрядчик (агентство ПРОКОНТЕКСТ), поэтому отзыв сносил и его токен.
-- Отзыв убран полностью (см. sync/lime_vk_ads.py); вместо этого — кэш: переиспользуем
-- живой токен, пока не истёк, новый выпускаем только когда нужно.
--
-- CREATE TABLE IF NOT EXISTS: миграции применяются при КАЖДОМ прогоне синка
-- (scripts/apply_lime_migrations.py), повтор обязан проходить без ошибок.
CREATE TABLE IF NOT EXISTS lime_vk_tokens (
  client_id  text PRIMARY KEY,
  access_token text NOT NULL,
  expires_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE lime_vk_tokens IS
  'Секрет: access_token рекламного кабинета VK. Наружу (API/дашборд) не отдавать — '
  'читает и пишет только синк по DATABASE_URL (sync/lime_vk_ads.py).';

-- Условно: ALTER ... ENABLE RLS берёт ACCESS EXCLUSIVE lock даже если RLS уже включён,
-- а миграции применяются при КАЖДОМ прогоне синка → на живой записи ловили statement
-- timeout (см. миграцию 012). Политик не создаём — таблица не должна быть доступна
-- анониму вообще (не витрина, только служебный кэш токена).
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'public' AND c.relname = 'lime_vk_tokens'
                   AND c.relrowsecurity)
  THEN
    EXECUTE 'ALTER TABLE lime_vk_tokens ENABLE ROW LEVEL SECURITY';
  END IF;
END $$;
