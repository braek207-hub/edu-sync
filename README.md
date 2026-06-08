# edu-sync

Ежедневная загрузка данных EDU Dashboard в Supabase:

- **Яндекс Директ API v5** → `direct_stats` (триггер: 7 дней; full: с `DIRECT_DATE_FROM`)
- **Google Sheets** (листы `Лиды`, `Оплаты`) → `crm_leads`, `crm_payments`
- **LIME MySQL** (`lc_simple_view`) → `lime_stats` (workflow `sync-lime.yml`)

Дашборд на Vercel ([EDU v2](https://github.com/braek207-hub/EduDash)) читает только Supabase.

## Локальный запуск

```bash
cd edu-sync
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
cp .env.example .env     # заполнить переменные
python main.py
```

## GitHub Actions

Workflows:
- `.github/workflows/sync.yml` — EDU Direct + CRM (`workflow_dispatch`)
- `.github/workflows/sync-lime.yml` — LIME → `lime_stats` (cron + backfill)

### Secrets

**Подробно:** [docs/SECRETS.md](docs/SECRETS.md)

Четыре секрета уже используются в **`BJ_auto_metrica`**: `GCP_SA_KEY`, `SHEET_ID_EDU`, `DIRECT_TOKEN_EDU`, `DIRECT_CLIENTS_JSON_EDU` — скопируйте в edu-sync.

Новый только **`DATABASE_URL`** (Supabase `:5432`, как `DIRECT_URL` в EDU v2 `.env.local`).

Автозаполнение после `gh auth login`: `scripts/setup-github-secrets.ps1` + файл `.env.sync`.

## Создание репозитория

```bash
git init
git add .
git commit -m "feat: initial edu-sync (Direct + CRM → Supabase)"
git remote add origin https://github.com/braek207-hub/edu-sync.git
git push -u origin main
```

## Тесты

```bash
pytest -v
```

## Связанные проекты

- **EDU v2** — UI + `/api/dashboard`
- **project EDU/gas** — референс логики CRM и классификации кампаний
