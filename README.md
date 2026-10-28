# edu-sync

Код синков данных Panda-BI (все дашборды: EDU, LIME, BJORN, Polina Repik, mesh-n-flesh) —
Яндекс Директ, Метрика, AppMetrica, CRM-таблицы, Roistat, VK и др. → таблицы базы Panda-BI.

## Где это исполняется (с 17.10.2026)

- **Не в GitHub Actions.** Синки — юниты systemd `panda-sync@<слаг>_<источник>` на сервере Panda-BI
  (VPS Timeweb `5.129.215.93`, checkout `/opt/panda/edu-sync`). Перед каждым прогоном юнит делает
  `git pull --ff-only` — пуш в `main` подхватывается следующим запуском.
- **Расписание, env и команда каждого юнита** — в репо EduDash: `infra/panda/sync/gen-units.py` → `units/`.
  Источник каждого юнита — бывший workflow отсюда; расхождения объяснены там же.
- **База** — Postgres self-hosted Supabase на том же сервере. Supabase Cloud — архив только на чтение,
  данные заморожены на 17.10.2026: всё, что читается из Cloud или по `DATABASE_URL` из старых `.env`,
  — не прод.
- **Прогоны** — таблица `public.sync_runs` базы сервера (статус, хвост лога ошибки), экран источников
  проекта в Panda-BI. Ручной запуск — кнопка «Обновить» источника или `systemctl start panda-sync@<x>`.
- **Секреты** — `/opt/panda/sync*.env` на сервере (карта — EduDash `docs/ENV-MAP.md`).

**В GitHub Actions остались** только workflow, пишущие в Метрику (токен записи Метрики есть лишь в
секретах GitHub): `bjorn-slide-rt-segments.yml`, `bjorn-build-retarget`, `Metrika retarget segments`,
`Export Metrika VUZ CSV`, — и ручные `probe-*`. Остальные `.github/workflows/sync-*.yml` выключены
и остаются как история; правка их cron ни на что не влияет.

## Локальный запуск

Проект — uv: зависимости в `pyproject.toml`, точные версии в `uv.lock`. `requirements.txt`
(с хэшами) генерируется из lock — его ставят сервер (`setup.sh` в EduDash) и workflow GitHub.

```bash
uv sync --all-extras                        # .venv со всеми пакетами, включая тесты и ruff
git config core.hooksPath scripts/git-hooks # один раз: pre-push гоняет lock, ruff и тесты
cp .env.example .env     # заполнить переменные; база — только через ssh-туннель к серверу
uv run python main.py
```

Новая или обновлённая зависимость: правка `pyproject.toml` → `uv lock` →
`uv export --format requirements-txt --no-dev --extra agent --no-emit-project --no-header -o requirements.txt`.
Пакеты моложе 7 дней uv не берёт (`exclude-newer` в uv.toml), Dependabot ждёт столько же.

## Тесты

```bash
uv run pytest -q
uv run ruff check .
```

## Связанные проекты

- **EduDash** (`C:\vscode\EDU v2`) — Panda-BI: приложение, `infra/panda/sync/` (юниты синков),
  `docs/INTEGRATIONS.md` § «Где что живёт»
- **project EDU/gas** — референс логики CRM и классификации кампаний
