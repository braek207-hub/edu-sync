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
- Деление по странам GCC (Фаза 2): dimension `ym:s:startURLDomain` или `ym:s:URLDomain` (ae./bh./sa.…) — проверить.

## GA4 — Data API ⚠️ ОТКЛОНЁН (в пользу Метрики)

Не используем (см. разворот выше). Раньше упирался в: 403 SERVICE_DISABLED —

- `runReport properties/417919368`, dims `[date, hostName, sessionSourceMedium]`, metrics `[sessions, totalUsers, newUsers, bounceRate]`.
- Ошибка: **Google Analytics Data API не включён** в GCP-проекте `131094257045` (проект сервис-аккаунта `D:\vscode\LIME\google_credentials.json`).
- Fix: включить API → https://console.developers.google.com/apis/api/analyticsdata.googleapis.com/overview?project=131094257045 → подождать пару минут → повторить зонд. Авторизация валидна (дошли до API).
- `hostName` в измерениях — чтобы проверить деление трафика по доменам стран GCC (Фаза 2).

## Открытые вопросы к Павлу

1. **Валюта Shopify-магазина** (базовая): AOV ≈ 509 → похоже на AED, но подтвердить (AED / USD / RUB). Определяет конвертацию (A2 сейчас USD→₽; при AED — сделать AED→₽, cbr.ru код R01230).
2. Включить GA4 Data API (ссылка выше).
3. (Опц.) scope Pixel Attribution на TW-ключе — если нужна per-order атрибуция.