# GCC источники — реальные контракты (зонд B1, 2026-07-17)

Зонд: `scripts/probe_gcc_apis.py`. Магазин: `lime-shop-prod.myshopify.com`
(один Shopify на все страны Залива: ae/bh/kw/sa/qa/om через домены limestore.com / lime-shop.com).

## Triple Whale — summary-page ✅ РАБОТАЕТ

- `POST https://api.triplewhale.com/api/v2/summary-page/get-data`
- Заголовок: `x-api-key: <key>`
- Тело: `{"shopDomain": "...myshopify.com", "period": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}, "todayHour": 25}`
- Ответ: `{"metrics": [ {metricId, title, type, services:[...], values:{current, previous}, charts:{current:[{x:hour,y}], previous:[...]}} , ... ]}` — **720 метрик**, плоско по платформам, top-level только `metrics`.
- **Нет** разбивки по каналам-строкам и **нет** метки валюты. Гранулярность значения — период целиком; `charts` даёт почасовую разбивку (x=час 0..23).

Полезные metricId (день 2026-07-16, магазин целиком):
| metricId | смысл | пример | service |
|---|---|---|---|
| totalSales | Order Revenue (gross) | 38584 | shopify |
| netSales | Total Sales | 5885 | shopify |
| totalOrders | Заказы | 71 | shopify |
| newCustomerSales / rcRevenue | выручка новых / вернувшихся | 20067 / 18517 | shopify |
| totalRefunds | Возвраты | 28088 | shopify |
| grossProfit | Валовая прибыль | 4453 | shopify |
| shopifyAov | AOV | 509.8 | shopify |
| ga_adCost / googleConversionValue | Google расход / атриб. выручка | 816 / 48604 | google-ads |
| fb_ads_spend / facebookConversionValue / facebookPurchases | FB расход / выручка / покупки | 1183 / 16641 / 38 | facebook-ads |
| totalSnapchatSpend, tiktok*, pinterest*, bing | др. платные | 0 сейчас | * |
| klaviyo* | email/SMS | 0 сейчас | klaviyo |
| totalNetProfit / totalNetMargin / totalCpa | TW blended | 7064 / 18.3% / 28.2 | triple-whale |

→ **Канальная модель для дашборда:** маппить платформы в каналы — google-ads→SEM/Google, facebook-ads→SMM paid/Meta, snapchat/tiktok/pinterest→SMM paid, klaviyo→CRM, bing→SEM. Расход = *_spend/adCost; выручка канала = *ConversionValue (TW-атрибуция); заказы канала = *Purchases. Органика/директ/SEO = total shopify минус атрибутированное (или из GA4/пиксель-атрибуции позже).

## Triple Whale — attribution ✅ РАБОТАЕТ (источник заказов/выручки по каналам)

- `POST https://api.triplewhale.com/api/v2/attribution/get-orders-with-journeys-v2`,
  header `x-api-key`, тело **`{"shop": "...myshopify.com", "startDate": "YYYY-MM-DD", "endDate": "...", "excludeJourneyData": true}`**.
  ⚠️ Поле называется **`shop`** (не shopDomain), без `model` — иначе 403 "Access Denied" (первый зонд ошибся именно тут).
- Ответ: `{ordersWithJourneys:[{order_id, order_name, total_price, currency, created_at, customer_id,
  attribution:{firstClick, lastClick, fullFirstClick, fullLastClick, lastPlatformClick, linear, linearAll}}],
  totalForRange, count, earliestDate, finishedRange}`.
- Пагинация: если `totalForRange != count` — повторить с `endDate = earliestDate` (из офиц. примера TW).
- `attribution.<model>` = список тачпоинтов; берём source из первого: `lastPlatformClick` → fallback `lastClick` → `fullLastClick`. `total_price` в **AED** (подтвердило валюту).
- Проба 2026-07-17 (84 заказа): распределение по source чистое:
  google-ads 34 (18669 AED), organic_and_social 20 (8645), facebook-ads 19 (12514),
  mindbox_*/manual_mindbox 5 (CRM), Direct 2, copilot.com/non-attributed 4.
- Маппинг source→канал: google-ads→SEM/Google, facebook-ads→SMM paid/Meta, snapchat/tiktok-ads→SMM paid,
  organic_and_social→SEO/Organic&Social, mindbox*→CRM/Mindbox, Direct→Direct, *.com→Referrals, Non-attributed→Others.

### Итоговая модель GCC
- Заказы+выручка по каналам → **attribution** (source→канал, AED→₽). Полная, вкл. органику/директ/CRM.
- Расход по каналам → **summary-page** per-platform (ga_adCost, fb_ads_spend, snapchat/tiktok/pinterest/bing).
- Трафик по каналам → **Яндекс.Метрика 98232701**.

## Яндекс.Метрика — трафик ✅ РАБОТАЕТ (выбранный источник трафика)

**Разворот (2026-07-18): трафик GCC берём из Яндекс.Метрики, НЕ из GA4** (как RU/KZ по смыслу;
переиспользует Метрика-клиент; убирает блокер GA4 Data API).

- Счётчик **98232701** = `LIME - UAE` (site `ae.lime-shop.com`, Active). Один на все страны GCC.
- Токен: `YANDEX_TOKEN` из `d:\vscode\LIME\config.py` (Директ+Метрика+Медиаметрика, аккаунт LIME RU) —
  имеет доступ к GCC-счётчику. Перенесён в `edu-sync\.env` как `GCC_METRICA_TOKEN`.
  ⚠️ Токен захардкожен дефолтом в config.py/config_kz.py + в `Яндекс API LIME RU.txt` — **утечка, отозвать/убрать в env**.
- Management: `GET https://api-metrika.yandex.net/management/v1/counter/98232701`, header `Authorization: OAuth <token>` → HTTP 200.
- Stat API: `GET https://api-metrika.yandex.net/stat/v1/data` params `ids=98232701, date1, date2,
  metrics=ym:s:visits,ym:s:users,ym:s:pageviews,ym:s:bounceRate,
  dimensions=ym:s:date,ym:s:lastsignTrafficSource,ym:s:lastsignSourceEngine, accuracy=full`.
- Проба (2026-07-17): 27 строк, totals=[visits 4493, users 3771, pageviews 22204, bounce 24.7%].
  Разбивка по источникам чистая: Ad traffic/{Google Ads, Instagram, Facebook, Yandex.Direct},
  Search engine/{Google, Bing, Yandex}, Social network/{Facebook, instagram}, Mailing, Direct,
  Link traffic/*.com, Internal, Messenger/Telegram, QR.
- Маппинг → таксономия: Ad+engine → SEM(Google/Yandex)/SMM paid(Meta); Search engine → SEO;
  Social network → SMM organic; Mailing → CRM/Email; Direct → Direct; Link → Referrals.

### Деление по странам ✅ ЗОНД P1 (2026-07-18)

- Рабочий dimension — **`ym:s:startURLDomain`**. `ym:s:URLDomain` **не существует**
  (HTTP 400 `invalid_parameter ... error code: 4001`) — не использовать.
- Проба за 7 дней (2026-07-12…07-18), только этот dimension: **все 6 стран присутствуют**,
  totals visits=35660 / users=25660 — сумма по доменам = totals (деление полное, без «прочего»):

  | домен | visits | users |
  |---|---|---|
  | ae.limestore.com | 30656 | 21854 |
  | sa.limestore.com | 3087 | 2306 |
  | kw.limestore.com | 806 | 644 |
  | qa.limestore.com | 756 | 633 |
  | om.limestore.com | 275 | 239 |
  | bh.limestore.com | 80 | 71 |

- В связке с прод-набором измерений работает:
  `dimensions=ym:s:date,ym:s:startURLDomain,ym:s:lastsignTrafficSource,ym:s:lastsignSourceEngine`
  (проба за 2026-07-17 — строки вида `2026-07-17 / sa.limestore.com / Ad traffic / Google Ads`).
- Фикстура: `tests/fixtures/metrika_domain_sample.json` (обрезана до 2 строк на домен).
- ⚠️ В окне пробы трафик только на `*.limestore.com`, но сайт счётчика — `ae.lime-shop.com`,
  и в journey TW встречается `ae.lime-shop.com` → **страну определять по префиксу домена**
  (`ae|bh|kw|sa|qa|om`), а не по полному хосту.

## GA4 — Data API ⚠️ ОТКЛОНЁН (в пользу Метрики)

Не используем (см. разворот выше). Раньше упирался в: 403 SERVICE_DISABLED —

- `runReport properties/417919368`, dims `[date, hostName, sessionSourceMedium]`, metrics `[sessions, totalUsers, newUsers, bounceRate]`.
- Ошибка: **Google Analytics Data API не включён** в GCP-проекте `131094257045` (проект сервис-аккаунта `D:\vscode\LIME\google_credentials.json`).
- Fix: включить API → https://console.developers.google.com/apis/api/analyticsdata.googleapis.com/overview?project=131094257045 → подождать пару минут → повторить зонд. Авторизация валидна (дошли до API).
- `hostName` в измерениях — чтобы проверить деление трафика по доменам стран GCC (Фаза 2).

## TW journey → страна заказа ✅ ЗОНД P3 (2026-07-18)

Запрос тот же, что и для заказов, но **`"excludeJourneyData": false`**.

- Каждый заказ получает поле **`journey`** — список тачпоинтов `{time, event, path}`.
  События только двух видов: `page loaded` (26634 шт. за день, есть `path`) и
  `add2c` (2207 шт., `path` НЕТ — вместо него `productId`).
  **Событий checkout/purchase в journey нет** — страну берём из URL страниц.
- `journey` отсортирован **по убыванию времени**: `journey[0]` — самый свежий тачпоинт
  (ближайший к моменту заказа), `journey[-1]` — самый ранний (глубина ~10 дней).
- **Правило:** `order_country` = префикс хоста первого тачпоинта, у которого `path` даёт
  хост с префиксом из `ae|bh|kw|sa|qa|om` (т.е. самый свежий тачпоинт со страной).
  Нет такого → `country = NULL` (заказ попадает только в GCC-тотал).
- Проверка на 84 заказах (2026-07-17): смешанные домены в journey у 9 заказов (10.7%),
  но правило «свежий» расходится с «доминирующим по числу тачпоинтов» лишь у **2 из 84** (2.4%);
  без страны — 3 заказа (3.6%). Распределение по правилу: ae 77, sa 3, kw 1, NULL 3.
- Хосты в journey бывают и `*.limestore.com`, и `*.lime-shop.com` → матчить **префикс**, не хост.
- Фикстура: `tests/fixtures/tw_orders_journey_sample.json` (7 заказов, journey обрезан до 6
  тачпоинтов, PII вырезан: order_id/order_name = REDACTED, без customer_id/email).
- ⚠️ Цена: `excludeJourneyData: false` раздувает ответ (84 заказа ≈ 29k тачпоинтов) — в синке
  парсить потоково и не логировать journey целиком.

## Google Ads — гео-расход ⛔ ЗОНД P2 BLOCKED (нет кабинета GCC)

**Статус: missing-data.** В `lime_google_ads_stats` есть только `region='kz'` (981 строка,
1 аккаунт) — Script в GCC-аккаунте ещё не поставлен (действие Павла). Контракт ниже — по
докам Google Ads API, **требует живой проверки при установке Script**.

- Ресурс: **`FROM geographic_view`** — метрики агрегированы по стране, одна строка на страну
  (плюс разрез по другим сегментам). Поля: `geographic_view.country_criterion_id`,
  `geographic_view.location_type`.
- Кандидат-запрос для `AdsApp.search()` (Script, тот же стиль, что r1 в
  `docs/integrations/google-ads-ingest-script.js` в репо EDU v2):
  ```
  SELECT campaign.id, segments.date, geographic_view.country_criterion_id,
         metrics.impressions, metrics.clicks, metrics.cost_micros
  FROM geographic_view
  WHERE segments.date BETWEEN '<from>' AND '<to>'
    AND geographic_view.location_type = 'LOCATION_OF_PRESENCE'
  ```
- ⚠️ `location_type` обязателен в WHERE: без него строки идут и по `LOCATION_OF_PRESENCE`
  (физическое местоположение), и по `AREA_OF_INTEREST` → расход задвоится. Для «расход по
  стране покупателя» берём LOP.
- `country_criterion_id` — числовой id гео-таргета, **не** название. Резолвить в стране
  отдельным запросом, а не хардкодом id:
  ```
  SELECT geo_target_constant.id, geo_target_constant.name, geo_target_constant.country_code
  FROM geo_target_constant WHERE geo_target_constant.id IN (<ids из первого запроса>)
  ```
  → `country_code` (AE/BH/KW/SA/QA/OM) → та же таблица стран, что и для доменов.
- `cost_micros` делить на 1 000 000; валюта = валюта аккаунта (`getCurrencyCode()`), далее →₽.

## Открытые вопросы к Павлу

1. **Валюта Shopify-магазина** (базовая): AOV ≈ 509 → похоже на AED, но подтвердить (AED / USD / RUB). Определяет конвертацию (A2 сейчас USD→₽; при AED — сделать AED→₽, cbr.ru код R01230).
2. Включить GA4 Data API (ссылка выше).
3. (Опц.) scope Pixel Attribution на TW-ключе — если нужна per-order атрибуция.