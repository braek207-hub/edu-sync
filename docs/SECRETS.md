# Секреты для edu-sync

Значения **не хранятся в git**. Большая часть уже есть в репозитории **`braek207-hub/BJ_auto_metrica`** (workflow `run_direct_daily_multi_edu.yml`).

## Карта: откуда брать

| edu-sync (GitHub Secret) | Уже есть в BJ_auto_metrica | GAS / локально |
|--------------------------|----------------------------|----------------|
| `GCP_SA_KEY` | ✅ `GCP_SA_KEY` | — |
| `SHEET_ID_EDU` или `GOOGLE_SHEETS_ID` | ✅ `SHEET_ID_EDU` | Script Property **`SPREADSHEET_ID`** (тот же ID книги) |
| `DIRECT_TOKEN_EDU` или `DIRECT_TOKEN` | ✅ `DIRECT_TOKEN_EDU` | — |
| `DIRECT_CLIENTS_JSON_EDU` | ✅ `DIRECT_CLIENTS_JSON_EDU` | JSON `[{login, goal_ids, sheet_name}, …]` |
| `DATABASE_URL` | ❌ только для v2 | **EDU v2** `.env.local` → `DIRECT_URL` (Supabase **:5432**, не pooler) |

### GAS

В Apps Script: **Project Settings → Script properties → `SPREADSHEET_ID`** — это ID Google-книги с листами `Лиды`, `Оплаты`, Direct.

Совпадает с `SHEET_ID_EDU` в GitHub.

### CRM

Лиды и оплаты **не в отдельном API** — пока только эта Google-таблица. `edu-sync` читает листы `Лиды` и `Оплаты` тем же SA, что и BJ (`GCP_SA_KEY`).

### Директ

Старый скрипт `main_direct_daily_multi_edu.py` пишет в Sheets. **edu-sync** ходит в API напрямую → Supabase (Sheets для Direct больше не нужен дашборду v2).

---

## Настройка репозитория edu-sync

Минимум секретов в **edu-sync** (Settings → Secrets):

1. Скопировать из **BJ_auto_metrica** (значения те же):
   - `GCP_SA_KEY`
   - `SHEET_ID_EDU`
   - `DIRECT_TOKEN_EDU`
   - `DIRECT_CLIENTS_JSON_EDU`

2. Добавить только новый:
   - `DATABASE_URL` — URI Supabase Postgres **direct** (`db.*.supabase.co:5432`), как `DIRECT_URL` в EDU v2

Опционально дублировать под «универсальными» именами: `GOOGLE_SERVICE_ACCOUNT` (= содержимое `GCP_SA_KEY`), `GOOGLE_SHEETS_ID` (= `SHEET_ID_EDU`).

---

## GitHub CLI (после `gh auth login`)

Создать репо и вручную задать секрет (пример):

```bash
cd "d:\vscode\edu-sync"
gh repo create braek207-hub/edu-sync --public --source=. --push

# DATABASE_URL — из EDU v2 .env.local (ключ DIRECT_URL, не pooler 6543)
gh secret set DATABASE_URL --repo braek207-hub/edu-sync < db_uri.txt
```

Секреты из другого репо **автоматически не копируются** — только вручную или org-level secrets.

Скрипт-подсказка: `scripts/setup-github-secrets.ps1`

---

## Vercel (EDU v2)

Только `DATABASE_URL` (можно pooler для Prisma). **edu-sync** использует direct URL отдельно.
