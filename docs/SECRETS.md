# Секреты для edu-sync

Значения **не хранятся в git**. Большая часть уже есть в репозитории **`braek207-hub/BJ_auto_metrica`** (workflow `run_direct_daily_multi_edu.yml`).

## Карта: откуда брать

| edu-sync (GitHub Secret) | Уже есть в BJ_auto_metrica | GAS / локально |
|--------------------------|----------------------------|----------------|
| `GCP_SA_KEY` | ✅ `GCP_SA_KEY` | — |
| `SHEET_ID_EDU` или `GOOGLE_SHEETS_ID` | ✅ `SHEET_ID_EDU` | Script Property **`SPREADSHEET_ID`** (тот же ID книги) |
| `DIRECT_TOKEN_EDU` или `DIRECT_TOKEN` | ✅ `DIRECT_TOKEN_EDU` | — |
| `DIRECT_CLIENTS_JSON_EDU` | ✅ `DIRECT_CLIENTS_JSON_EDU` | JSON `[{login, goal_ids, sheet_name}, …]` |
| `DATABASE_URL` | ❌ только для v2 | Supabase **Connect → Prisma** → первая строка (**6543**, `aws-*-ap-south-1.pooler…`) |
| `LIME_DB_HOST` | — | MySQL хост LIME |
| `LIME_DB_SCHEMA` | — | Имя БД (schema) |
| `LIME_DB_USER` | — | MySQL user |
| `LIME_DB_PASSWORD` | — | MySQL password |
| `LIME_DB_PORT` | — | опционально, default `3306` (можно не задавать) |

### LIME

Workflow: `.github/workflows/sync-lime.yml` → таблица `lime_stats` в Supabase (дашборд LIME на Panda-BI).

| Режим | Переменные | Поведение |
|-------|------------|-----------|
| **Cron** | `LIME_SYNC_DAYS=7` | Последние 7 дней, по одному дню |
| **Backfill** | `LIME_SYNC_FROM`, `LIME_SYNC_TO` | Диапазон дат, chunked by day |

Секреты LIME (`LIME_DB_*`) — **только в edu-sync**, не в Panda-BI / Vercel.

### Polina Repik

Workflow: `.github/workflows/sync-polinarepik.yml` → таблицы `polinarepik_direct_stats`, `polinarepik_metrica_visits` в Supabase (дашборд `https://panda-bi.vercel.app/polinarepik`). Заказы Bitrix — ingest на Panda-BI.

| Секрет | Обязателен | Описание |
|--------|------------|----------|
| `DATABASE_URL` | ✅ | уже есть |
| `POLINAREPIK_YANDEX_TOKEN` | ✅ | один OAuth-токен Яндекса (Direct + Metrika) |

Не секреты (зашиты в `sync/polinarepik.py`):

- логин Direct: `polinarepik-wear`
- счётчик Metrika: `100764399`
- attribution: `lastsign`
- sync days: `7` (или input workflow)

**Не нужны в Panda-BI (Vercel):** токен Яндекса для Polina Repik. Bitrix ingest — только если ещё не настроен (`POLINAREPIK_INGEST_TOKEN` на Vercel).

### LIME Direct (кабинет Яндекс Директ)

Workflow: `.github/workflows/sync-lime-direct.yml` → таблица `lime_direct_stats` в Supabase (колонки **ЯНДЕКС ДИРЕКТ** на LIME2).

Отдельный от `sync-lime.yml` (MySQL → `lime_stats`): свой OAuth-токен и логин рекламируемого аккаунта LIME.

| Секрет | Обязателен | Описание |
|--------|------------|----------|
| `DATABASE_URL` | ✅ | уже есть |
| `LIME_DIRECT_TOKEN` | ✅ | OAuth Яндекс Директ, scope `direct:api` |
| `LIME_DIRECT_CLIENT_LOGIN` | ✅ | логин рекламируемого аккаунта LIME |

**Цели Директа — не секрет.** Список берётся из `config/lime_direct_goals.json`
(12 целей на 4 ступени: install / cart / checkout / purchase, с разбивкой по
платформам web / app_ios / app_android). Файл зеркалит `config/lime-direct-goals.json`
дашборда — при правке менять оба, иначе цели молча обнулятся.

Env `LIME_DIRECT_GOALS` больше не используется. Он появился 2026-06-24 (коммит
`47dc2b6`) вместо файлового чтения, имел плоский формат на шесть ключей и не содержал
ступени `install` вовсе. В CI задан не был → отчёт запрашивался без секции `Goals` →
`conversions={}` и все 12 колонок целей в дашборде были мертвы до 2026-07-18.

Расписание: ежедневно 10:00 МСК. Ручной запуск: Actions → **Sync LIME Direct → Supabase** → Run workflow.

Миграции таблицы:
- `panda-bi/supabase/migrations/20260616000000_lime_direct_stats.sql`
- `panda-bi/supabase/migrations/20260618000000_lime_direct_goals.sql` (конверсии)

### LIME GCC (регион Залива, Shopify)

Workflow: `.github/workflows/sync-lime-gcc.yml` → таблица `lime_stats` (region='gcc') в Supabase.
Отдельный ингест: витрины PROCONTEXT для GCC нет, собираем сами. Трафик — Яндекс.Метрика,
заказы/выручка+расход — Triple Whale, конверт AED→₽ (курс ЦБ). Пишет ТОЛЬКО region='gcc'
(delete+insert по этому срезу), не пересекается с RU/KZ. Детали контракта: `docs/GCC_CONTRACTS.md`.

| Секрет | Обязателен | Описание |
|--------|------------|----------|
| `DATABASE_URL` | ✅ | уже есть |
| `GCC_METRICA_TOKEN` | ✅ | OAuth Яндекса с доступом к Метрике счётчика GCC (то же значение, что `YANDEX_TOKEN` медийного синка LIME — Директ+Метрика+Медиаметрика) |
| `GCC_TRIPLEWHALE_API_KEY` | ✅ | Triple Whale Data-Out API key (scopes Read: Summary Page + Pixel Attribution) |
| `GCC_TW_SHOP_DOMAIN` | ✅ | постоянный домен магазина `lime-shop-prod.myshopify.com` |

Не секреты (зашиты в `sync/lime_gcc.py` / код):
- счётчик Метрики GCC (UAE): `98232701` (env `GCC_METRICA_COUNTER_ID` для override)
- валюта магазина: AED → ₽ по курсу ЦБ (`sync/fx.py`)
- sync days: `7` (или input workflow)

⚠️ `GCC_METRICA_TOKEN`: сейчас = `YANDEX_TOKEN`, который **захардкожен** в `d:\vscode\LIME\config.py` —
отозвать/перевыпустить и хранить только в секретах.

### GAS

В Apps Script: **Project Settings → Script properties → `SPREADSHEET_ID`** — это ID Google-книги с листами `Лиды`, `Оплаты`, Direct.

Совпадает с `SHEET_ID_EDU` в GitHub.

### CRM

Лиды и оплаты **не в отдельном API** — пока только эта Google-таблица. `edu-sync` читает листы `Лиды` и `Оплаты` тем же SA, что и BJ (`GCP_SA_KEY`).

### Директ

**edu-sync** грузит Директ только из **Яндекс Direct API** → Supabase.

| Режим | Переменные | Поведение |
|-------|------------|-----------|
| **Триггер (workflow_dispatch)** | `DIRECT_SYNC_MODE=incremental`, `DIRECT_DAYS_BACK=7` | Последние **7 дней**, upsert |
| **Полный период** | `DIRECT_SYNC_MODE=full`, `DIRECT_DATE_FROM=2026-01-01` | С даты по сегодня, delete + upsert |

`DIRECT_SOURCE=sheets` — только legacy (не используется в Actions).

| Переменная | Значение |
|------------|----------|
| `DIRECT_SOURCE` | `api` (default) |
| `DIRECT_SYNC_MODE` | `incremental` (default) или `full` |
| `DIRECT_DAYS_BACK` | для incremental, default `7` |
| `DIRECT_DATE_FROM` | для full, default `2026-01-01` |

---

## Настройка репозитория edu-sync

Минимум секретов в **edu-sync** (Settings → Secrets):

1. Скопировать из **BJ_auto_metrica** (значения те же):
   - `GCP_SA_KEY`
   - `SHEET_ID_EDU`
   - `DIRECT_TOKEN_EDU`
   - `DIRECT_CLIENTS_JSON_EDU`

2. Добавить для Supabase:
   - `DATABASE_URL` — **transaction pooler** из Supabase Connect (порт **6543**, user `postgres.vkawfgoqjjdlcfvzihbx`, регион **ap-south-1** — не `eu-central-1`)

3. Добавить для LIME (workflow `Sync LIME → Supabase`):
   - `LIME_DB_HOST`, `LIME_DB_SCHEMA`, `LIME_DB_USER`, `LIME_DB_PASSWORD`
   - `LIME_DB_PORT` — опционально (`3306` по умолчанию; **не создавайте пустой секрет**)

4. Добавить для Polina Repik (workflow `Sync Polina Repik → Supabase`):
   - `POLINAREPIK_YANDEX_TOKEN` — OAuth Яндекса (Direct + Metrika, аккаунт polinarepik-wear)

5. Добавить для LIME Direct (workflow `Sync LIME Direct → Supabase`):
   - `LIME_DIRECT_TOKEN` — OAuth Яндекс Директ (аккаунт LIME)
   - `LIME_DIRECT_CLIENT_LOGIN` — логин рекламируемого аккаунта LIME

Опционально дублировать под «универсальными» именами: `GOOGLE_SERVICE_ACCOUNT` (= содержимое `GCP_SA_KEY`), `GOOGLE_SHEETS_ID` (= `SHEET_ID_EDU`).

---

## GitHub CLI (после `gh auth login`)

Создать репо и вручную задать секрет (пример):

```bash
cd "d:\vscode\edu-sync"
gh repo create braek207-hub/edu-sync --public --source=. --push

# DATABASE_URL — скопировать из Supabase Connect → Prisma (6543, ap-south-1 pooler)
gh secret set DATABASE_URL --repo braek207-hub/edu-sync < db_uri.txt
```

Секреты из другого репо **автоматически не копируются** — только вручную или org-level secrets.

Скрипт-подсказка: `scripts/setup-github-secrets.ps1`

---

## Vercel (Panda-BI)

Прод-домен: **https://panda-bi.vercel.app**

`NEXT_PUBLIC_SITE_URL` (Production) = `https://panda-bi.vercel.app`

`DATABASE_URL` (6543) и `DIRECT_URL` (5432) — обе строки из Supabase Connect → Prisma. **edu-sync** использует только `DATABASE_URL`.
