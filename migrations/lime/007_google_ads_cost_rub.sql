-- Расход Google Ads в рублях. Google отдаёт расход в валюте аккаунта (KZ-аккаунт LIME = USD),
-- дашборд считает в рублях → конвертируем ЗАРАНЕЕ (sync/google_ads_fx.py, курс ЦБ через sync/fx.py),
-- чтобы хендлер читал готовые рубли и не дёргал курс в рантайме.
ALTER TABLE lime_google_ads_stats ADD COLUMN IF NOT EXISTS cost_rub numeric;
