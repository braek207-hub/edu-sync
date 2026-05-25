# edu-sync

Ежедневная загрузка данных EDU Dashboard в Supabase:

- **Яндекс Директ API v5** → `direct_stats` (последние 7 дней, cost без НДС)
- **Google Sheets** (листы `Лиды`, `Оплаты`) → `crm_leads`, `crm_payments`

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

Workflow: `.github/workflows/sync.yml` — cron **07:00 MSK** (04:00 UTC) + ручной запуск.

### Secrets (Settings → Secrets → Actions)

| Secret | Описание |
|--------|----------|
| `DATABASE_URL` | Supabase URI (тот же, что на Vercel) |
| `DIRECT_TOKEN` | OAuth Яндекс Директ |
| `DIRECT_CLIENT_LOGIN` | Client-Login |
| `GOOGLE_SHEETS_ID` | ID книги (= `SPREADSHEET_ID` в GAS Script Properties) |
| `GOOGLE_SERVICE_ACCOUNT` | JSON сервис-аккаунта **целиком одной строкой** |

Таблицу нужно расшарить на email сервис-аккаунта (роль Viewer достаточно).

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
