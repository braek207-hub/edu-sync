-- Колонка refresh_token в кэше токенов VK Рекламы (lime_vk_tokens).
--
-- Зачем: sync/lime_vk_ads.py (коммит 783119f) перешёл на обновление токена через
-- grant_type=refresh_token вместо выпуска нового при каждом протухании кэша — выпуск
-- каждый раз занимает новый слот из пяти общих с агентством ПРОКОНТЕКСТ, обновление
-- слот не занимает (см. комментарий в _get_token). Код уже читает/пишет эту колонку;
-- без миграции столбца в проде — ошибка на каждом SELECT/INSERT.
--
-- ADD COLUMN IF NOT EXISTS: миграции применяются при КАЖДОМ прогоне синка
-- (scripts/apply_lime_migrations.py), повтор обязан проходить без ошибок.
ALTER TABLE lime_vk_tokens ADD COLUMN IF NOT EXISTS refresh_token text;

COMMENT ON COLUMN lime_vk_tokens.refresh_token IS
  'Секрет наравне с access_token: обновляет доступ к рекламному кабинету VK без выпуска '
  'нового токена (слот не занимает). Наружу не отдавать, читает/пишет только синк.';
