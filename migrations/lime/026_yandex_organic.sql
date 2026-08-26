-- Органика Яндекса по гео (KZ, Залив) — SEO-половина Яндекс-вкладок «Брендового спроса».
--
-- ПОЧЕМУ МЕТРИКА, А НЕ ВЕБМАСТЕР. У API Вебмастера гео-среза нет вовсе: у метода
-- search-queries параметры только order_by / query_indicator / device_type_indicator /
-- date_from / date_to / offset / limit, а посланные region_ids|region_id|country
-- он молча игнорирует — живой зонд 2026-08-26 вернул на всех вариантах бит-в-бит
-- один и тот же ряд. В аккаунте всего два хоста (limestore.com, lime-shop.com), и
-- казахстанские заходы сидят внутри общего лимитстора, отделить их хостом нельзя.
-- Метрика того же счётчика 23504302 гео-срез даёт (ym:s:regionCountryName), и визит
-- из поисковой выдачи — это и есть клик по выдаче, то есть та же величина, что
-- TOTAL_CLICKS Вебмастера в RU-ряду.
--
-- БРЕНД НЕ ВЫЧИТАЕМ — как и в RU (см. sync/webmaster.py): у KZ фразу видно лишь у 15%
-- визитов (остальное Яндекс прячет), а среди видимых 97% брендовые, причём почти весь
-- «небренд» — опечатки бренда («лаим», «limr», «liime»). Фильтр по фразе выбросил бы
-- 85% ряда ради 3% примеси.
--
-- Зерно — день (недельный ряд собирается свёрткой в SQL): одна таблица вместо пары
-- «недели + дни», как у Директа (lime_direct_stats).
CREATE TABLE IF NOT EXISTS lime_yandex_organic (
  day        date NOT NULL,
  region     text NOT NULL,           -- 'kz' | 'gcc' (ключ вкладки в конфиге LIME)
  country    text NOT NULL DEFAULT '', -- у KZ пусто (регион = страна), у GCC — страна визита
  visits     integer NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (day, region, country)
);

-- RLS deny-all с рождения таблицы (как lime_gsc_seo_daily): читает только сервер
-- Panda-BI через Prisma, публичный ключ PostgREST не должен видеть ни строки.
ALTER TABLE lime_yandex_organic ENABLE ROW LEVEL SECURITY;
