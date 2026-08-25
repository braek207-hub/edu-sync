# Агент-директолог, волна 1: полнота картины и уважение к обучению — план реализации

> **Для агентов-исполнителей:** ОБЯЗАТЕЛЬНЫЙ СУБ-СКИЛЛ: `superpowers:subagent-driven-development`
> (рекомендуется) или `superpowers:executing-plans`. Шаги отмечены чекбоксами `- [ ]`.

**Цель:** закрыть три дыры в картине мира агента (недобор трафика, слепая зона расхода,
спрос как календарь) и научить его не сбивать обучение автостратегий Директа.

**Архитектура:** все расчёты — чистые функции в `sync/agent/*`, вызываемые из такта Э0
(`sync/agent_e0.py`) и такта записи Э1 (`sync/agent_e1.py`). Ни один новый модуль не ходит
в сеть и в БД сам: данные ему передают, результат он возвращает словарём. Запись в БД —
только через существующие `agent_db.upsert_computed_settings` и `writer_db`. Новых
внешних источников не появляется: недобор трафика берётся из уже собираемого поля
`AvgTrafficVolume`, спрос — из уже наполненной `edu_wordstat_demand`.

**Стек:** Python 3.11, psycopg2, pytest. Тесты — чистые, без БД и сети (образец:
`tests/test_agent_facts.py`). БД — PostgreSQL (Supabase), схема применяется идемпотентными
DDL-строками в `sync/agent/db.py::AGENT_DDL` + файл в `migrations/edu/`.

**Спека:** `docs/AGENT-ROADMAP-2026-08-25.md` — пункты Ф7.6, Ф7.7, Ф7.8, Ф6.5.
Читать вместе с этим планом.

## Что из спеки уже закрыто до начала волны (не делать заново)

- **Ф6.4 «Денежный чекпоинт»** реализован коммитом `057f1ab`:
  `sync/agent_e1_watchdog.py::money_metrics / money_verdict / money_check_due /
  money_checkpoint`, `MONEY_CHECKPOINT_DAYS = 35`, колонки `money_checked_at`,
  `money_verdict` в `edu_agent_actions`, расхождение «по заявкам ↔ по деньгам»
  печатается как `contradictions`. Тесты — `tests/test_agent_money_checkpoint.py`.
  Задач по нему в этом плане нет.
- Ф5 (связка с билдером), Ф8 (генератор идей), Ф9 (боевое применение и автономия) —
  **отдельные планы**, каждый со своим работающим результатом. Эта волна их не трогает.

## Глобальные ограничения

- Кириллица в исходниках — файлы UTF-8, каждый новый `.py` начинается с `# -*- coding: utf-8 -*-`.
- Никаких новых обращений к API Директа в расчётном такте, кроме probe-скрипта задачи 1.
- Все пороги — именованные константы модуля с комментарием об источнике числа
  (правило `no-guessing`: за каждой константой стоит замер или ссылка на справку).
- Тесты не ходят в сеть и в БД. Данные — литералы в теле теста.
- Гейт перед каждым коммитом: `python -m pytest tests/ -q` из корня `d:\vscode\edu-sync`.
- Ветка: `agent-wave1` (правило параллельных сессий: `git add` только своими путями,
  никогда `git add -A`).
- Ничего из этой волны не применяется в боевой кабинет: рычаги эта волна не добавляет,
  только меняет отбор и отчётность.

---

## Задача 1: probe — чем на самом деле меряется недобор трафика

Спека (Ф7.6) требует ввести «долю выкупа» в оценку насыщения и указывает, что колонка
`auction_win_share` пуста. Причина известна из кода: `sync/direct_sheets.py:61-62` брал
долю выкупа из колонок Google-таблицы (`impressionshare` / `searchimpressionshare`) —
это выгрузка интерфейса, а API-путь `sync/direct.py:137` пишет туда жёсткий `0.0`.
В списке полей Reports API (`https://yandex.ru/dev/direct/doc/ru/fields-list`) поля
доли показов нет, зато есть `AvgTrafficVolume` — и он уже собирается
(`sync/direct.py:26`, `w_avg_traffic_vol` в `direct_stats`).

Задача — не гадать, а получить вердикт живым запросом: принимает ли API поля-кандидаты
доли выкупа и заполнен ли фактически объём трафика.

**Файлы:**
- Создать: `probe_traffic_headroom.py`
- Создать: `.github/workflows/probe-traffic-headroom.yml`
- Создать: `tests/test_probe_traffic_headroom.py`
- Создать: `docs/AGENT-DATA-SOURCES.md`

**Интерфейсы:**
- Отдаёт: `field_verdict(status: int, body: str) -> str` — вердикт по одному полю,
  одно из `"OK"`, `"FIELD_UNKNOWN"`, `"ERROR:<код>"`. Используется только внутри probe.
- Дальнейшие задачи зависят не от кода probe, а от его **вывода**, записанного
  в `docs/AGENT-DATA-SOURCES.md`.

- [ ] **Шаг 1: Написать падающий тест разбора ответа**

```python
# tests/test_probe_traffic_headroom.py
# -*- coding: utf-8 -*-
"""Разбор ответа Reports API в probe недобора трафика.

Поле, которого у API нет, отвечает 400 с BadParams и текстом про FieldNames.
Отличить это от временной ошибки обязательно: «поля нет» — вывод навсегда,
«сервис ответил 502» — повод повторить.
"""

from probe_traffic_headroom import field_verdict


def test_ok_when_report_returned():
    assert field_verdict(200, "Date\tClicks\n2026-08-01\t10\n") == "OK"


def test_offline_report_is_ok_too():
    # 201/202 — отчёт принят в очередь: поле принято, данные приедут позже.
    assert field_verdict(201, "") == "OK"


def test_unknown_field_detected_by_message():
    body = '{"error":{"error_code":8000,"error_detail":"Недопустимое значение параметра FieldNames"}}'
    assert field_verdict(400, body) == "FIELD_UNKNOWN"


def test_other_error_is_not_field_verdict():
    body = '{"error":{"error_code":152,"error_detail":"Не хватает средств"}}'
    assert field_verdict(400, body) == "ERROR:152"
```

- [ ] **Шаг 2: Прогнать тест и убедиться, что он падает**

Запуск: `python -m pytest tests/test_probe_traffic_headroom.py -q`
Ожидается: FAIL — `ModuleNotFoundError: No module named 'probe_traffic_headroom'`.

- [ ] **Шаг 3: Написать probe**

```python
# probe_traffic_headroom.py
# -*- coding: utf-8 -*-
"""
probe_traffic_headroom.py — чем меряется недобор трафика в кампаниях EDU.

Два вопроса, оба закрываются фактом, а не рассуждением:

1. Принимает ли Reports API поле доли выкупа. В списке полей его нет, но
   список бывает неполон, а колонка edu_agent_facts.auction_win_share
   существует и пуста — значит когда-то данные откуда-то брались (из
   Google-таблицы, sync/direct_sheets.py). Пробуем кандидатов по одному:
   поле, которого нет, API отвергает целиком, поэтому спрашивать их пачкой
   бессмысленно.
2. Заполнен ли AvgTrafficVolume — поле, которое в списке ЕСТЬ и которое
   sync/direct.py уже пишет в direct_stats.w_avg_traffic_vol. Если оно живое,
   недобор трафика считается из имеющихся данных и ждать нечего.

Запуск: python probe_traffic_headroom.py
ENV: DIRECT_TOKEN, DIRECT_CLIENTS_JSON, DATABASE_URL
"""

import json
import os
from datetime import date, timedelta

import requests

REPORTS_URL = "https://api.direct.yandex.com/json/v5/reports"

# Кандидаты на «долю выкупа». Первые три — как поле могло бы называться по
# аналогии с колонками выгрузки интерфейса (sync/direct_sheets.py:61),
# AvgTrafficVolume — заведомо существующий контроль: если и он вернёт
# FIELD_UNKNOWN, сломан probe, а не API.
CANDIDATES = [
    "ImpressionShare",
    "SearchImpressionShare",
    "AuctionWinShare",
    "AvgTrafficVolume",
]

FIELD_ERROR_MARKERS = ("FieldNames", "FieldName")


def field_verdict(status: int, body: str) -> str:
    """Вердикт по одному полю: принято, не существует, иная ошибка."""
    if status in (200, 201, 202):
        return "OK"
    try:
        error = (json.loads(body) or {}).get("error") or {}
    except ValueError:
        return f"ERROR:http{status}"
    code = error.get("error_code")
    detail = f"{error.get('error_detail', '')} {error.get('error_string', '')}"
    if any(marker in detail for marker in FIELD_ERROR_MARKERS):
        return "FIELD_UNKNOWN"
    return f"ERROR:{code}"


def _client_login() -> str:
    raw = (os.environ.get("DIRECT_CLIENTS_JSON") or "").strip()
    if raw:
        for item in json.loads(raw):
            if isinstance(item, dict) and str(item.get("login", "")).strip():
                return str(item["login"]).strip()
    return os.environ["DIRECT_CLIENT_LOGIN"]


def probe_field(login: str, field: str, date_from: str, date_to: str) -> str:
    body = {"params": {
        "SelectionCriteria": {"DateFrom": date_from, "DateTo": date_to},
        "FieldNames": ["CampaignId", field],
        "ReportName": f"probe-headroom-{field}-{date_from}",
        "ReportType": "CUSTOM_REPORT",
        "DateRangeType": "CUSTOM_DATE",
        "Format": "TSV",
        "IncludeVAT": "YES",
        "IncludeDiscount": "NO",
    }}
    resp = requests.post(
        REPORTS_URL,
        json=body,
        headers={
            "Authorization": f"Bearer {os.environ['DIRECT_TOKEN']}",
            "Client-Login": login,
            "Accept-Language": "ru",
            "processingMode": "auto",
            "returnMoneyInMicros": "false",
            "skipReportHeader": "true",
            "skipColumnHeader": "true",
            "skipReportSummary": "true",
        },
        timeout=120,
    )
    return field_verdict(resp.status_code, resp.text)


def db_fill() -> dict:
    """Заполненность взвешенных колонок в direct_stats за 30 дней."""
    from sync.db import get_connection

    since = (date.today() - timedelta(days=30)).isoformat()
    sql = """
        SELECT count(*) AS rows,
               count(*) FILTER (WHERE coalesce(w_auction_win_share, 0) > 0) AS win_nonzero,
               count(*) FILTER (WHERE coalesce(w_avg_traffic_vol, 0) > 0) AS traffic_nonzero,
               sum(impressions) AS impressions,
               sum(w_avg_traffic_vol) AS traffic_weighted
        FROM direct_stats
        WHERE date >= %s AND project = 'vuz'
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, (since,))
        row = cur.fetchone()
    rows, win_nz, traf_nz, impressions, traffic_weighted = row
    return {
        "rows": rows,
        "win_share_nonzero": win_nz,
        "traffic_volume_nonzero": traf_nz,
        "avg_traffic_volume": (round(float(traffic_weighted) / impressions, 2)
                               if impressions else None),
    }


def main() -> int:
    login = _client_login()
    date_to = (date.today() - timedelta(days=1)).isoformat()
    date_from = (date.today() - timedelta(days=8)).isoformat()
    fields = {field: probe_field(login, field, date_from, date_to)
              for field in CANDIDATES}
    print(json.dumps({"login": login, "window": [date_from, date_to],
                      "fields": fields, "db": db_fill()},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Шаг 4: Прогнать тест и убедиться, что он зелёный**

Запуск: `python -m pytest tests/test_probe_traffic_headroom.py -q`
Ожидается: PASS (4 теста).

- [ ] **Шаг 5: Добавить workflow для боевого прогона**

```yaml
# .github/workflows/probe-traffic-headroom.yml
name: probe-traffic-headroom
on:
  workflow_dispatch:
jobs:
  run:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -r requirements.txt
      - name: Probe
        env:
          DIRECT_TOKEN: ${{ secrets.EDU_DIRECT_TOKEN }}
          DIRECT_CLIENTS_JSON: ${{ secrets.DIRECT_CLIENTS_JSON }}
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
        run: python probe_traffic_headroom.py
```

- [ ] **Шаг 6: Прогнать probe в бою и записать вердикт**

Запуск: вкладка Actions → `probe-traffic-headroom` → Run workflow.
Скопировать JSON из лога и завести `docs/AGENT-DATA-SOURCES.md` с разделом:

```markdown
# Источники данных агента: что чем меряется

## Недобор трафика (замер <дата>, probe_traffic_headroom)

| Поле | Вердикт API | Комментарий |
|---|---|---|
| ImpressionShare | <из лога> | |
| SearchImpressionShare | <из лога> | |
| AuctionWinShare | <из лога> | |
| AvgTrafficVolume | <из лога> | контроль: поле из справочника |

Заполненность в `direct_stats` за 30 дней: <из лога>.

**Вывод.** <Одна фраза: чем меряем недобор — долей выкупа из API, если поле нашлось,
иначе объёмом трафика AvgTrafficVolume.>
```

Если probe покажет, что поле доли выкупа API отдаёт, — задачи 2–4 делаются по нему
(имя колонки в `direct_stats` уже есть: `w_auction_win_share`), а не по объёму трафика.
Дальнейшие задачи написаны под объём трафика как под ожидаемый исход; при другом исходе
меняются только имя поля в SELECT и название константы, структура задач не меняется.

- [ ] **Шаг 7: Коммит**

```bash
git add probe_traffic_headroom.py tests/test_probe_traffic_headroom.py \
        .github/workflows/probe-traffic-headroom.yml docs/AGENT-DATA-SOURCES.md
git commit -m "probe(agent): чем меряется недобор трафика — вердикт API и заполненность витрины"
```

---

## Задача 2: взвешенные метрики Директа доезжают до фактов как средние

`sync/agent/facts.py:79-80` кладёт в `avg_impr_pos` и `auction_win_share` **взвешенную
сумму** из `direct_stats` (`w_* = значение × показы`), причём присваиванием: несколько
исходных строк на одну пару «день × кампания» затирают друг друга, а деления на показы
нет вовсе. То есть колонка не только пуста по доле выкупа — она и по позиции показа
хранит не то, что обещает имя. Плюс `AvgTrafficVolume` до фактов вообще не доезжает:
`sync/agent/db.py:248-258` его не выбирает.

**Файлы:**
- Изменить: `sync/agent/facts.py:38-81`
- Изменить: `sync/agent/db.py:45-70` (DDL), `:248-258` (SELECT), `:755-790` (upsert)
- Создать: `migrations/edu/20260825_agent_traffic_volume.sql`
- Изменить: `tests/test_agent_facts.py`

**Интерфейсы:**
- Потребляет: строки `load_direct_rows` с ключами `impressions`, `w_avg_impr_pos`,
  `w_auction_win_share`, **новый** `w_avg_traffic_vol`.
- Отдаёт: строки фактов с полями `avg_impr_pos`, `auction_win_share`,
  **новым** `avg_traffic_vol` — все три уже поделены на показы (средние за день).

- [ ] **Шаг 1: Написать падающие тесты**

Дописать в конец `tests/test_agent_facts.py`:

```python
def test_weighted_direct_metrics_are_averaged_by_impressions():
    # w_* приходят взвешенными на показы: среднее = Σw / Σпоказов.
    # Присваивание вместо деления давало «среднюю позицию 1350».
    rows = assemble_facts(
        [{"date": "2026-08-01", "campaign_id": "111", "cost": 100.0, "clicks": 5,
          "impressions": 1000, "w_avg_impr_pos": 1500.0,
          "w_auction_win_share": 400.0, "w_avg_traffic_vol": 62000.0}],
        [], [])
    row = rows[0]
    assert row["avg_impr_pos"] == 1.5
    assert row["auction_win_share"] == 0.4
    assert row["avg_traffic_vol"] == 62.0


def test_two_direct_rows_of_one_day_are_summed_not_overwritten():
    # Один и тот же день+кампания приходит двумя строками (два кабинета
    # одного проекта, дозагрузка). Затирание последней строкой теряло половину.
    rows = assemble_facts(
        [{"date": "2026-08-01", "campaign_id": "111", "cost": 50.0, "clicks": 2,
          "impressions": 1000, "w_avg_impr_pos": 1000.0,
          "w_auction_win_share": 0.0, "w_avg_traffic_vol": 40000.0},
         {"date": "2026-08-01", "campaign_id": "111", "cost": 50.0, "clicks": 3,
          "impressions": 1000, "w_avg_impr_pos": 2000.0,
          "w_auction_win_share": 0.0, "w_avg_traffic_vol": 80000.0}],
        [], [])
    row = rows[0]
    assert row["impressions"] == 2000
    assert row["avg_impr_pos"] == 1.5
    assert row["avg_traffic_vol"] == 60.0


def test_zero_impressions_give_zero_not_division_error():
    rows = assemble_facts(
        [{"date": "2026-08-01", "campaign_id": "111", "cost": 0.0, "clicks": 0,
          "impressions": 0, "w_avg_impr_pos": 0.0,
          "w_auction_win_share": 0.0, "w_avg_traffic_vol": 0.0}],
        [], [])
    assert rows[0]["avg_traffic_vol"] == 0.0
```

- [ ] **Шаг 2: Прогнать и убедиться, что падает**

Запуск: `python -m pytest tests/test_agent_facts.py -q`
Ожидается: FAIL — `KeyError: 'avg_traffic_vol'` и `assert 1500.0 == 1.5`.

- [ ] **Шаг 3: Починить сборку фактов**

В `sync/agent/facts.py` в `_slot` заменить три строки инициализации на накопители:

```python
                "avg_impr_pos": 0.0,
                "auction_win_share": 0.0,
                "avg_traffic_vol": 0.0,
                # Накопители взвешенных сумм Директа: w_* = значение × показы.
                # Средним они становятся в самом конце, делением на показы дня.
                # Держать их отдельно обязательно: строк источника на пару
                # «день × кампания» бывает больше одной, и присваивание среднего
                # по ходу цикла теряло бы все, кроме последней.
                "_w_impr_pos": 0.0,
                "_w_win_share": 0.0,
                "_w_traffic_vol": 0.0,
```

В цикле по `direct_rows` заменить две строки присваивания на:

```python
        slot["_w_impr_pos"] += float(r.get("w_avg_impr_pos") or 0.0)
        slot["_w_win_share"] += float(r.get("w_auction_win_share") or 0.0)
        slot["_w_traffic_vol"] += float(r.get("w_avg_traffic_vol") or 0.0)
```

Перед `return` добавить финализацию:

```python
    for slot in facts.values():
        impressions = slot["impressions"]
        for target, source in (("avg_impr_pos", "_w_impr_pos"),
                               ("auction_win_share", "_w_win_share"),
                               ("avg_traffic_vol", "_w_traffic_vol")):
            slot[target] = round(slot.pop(source) / impressions, 4) if impressions else 0.0

    return sorted(facts.values(), key=lambda x: (x["fact_date"], x["campaign_id"]))
```

- [ ] **Шаг 4: Прогнать тесты фактов**

Запуск: `python -m pytest tests/test_agent_facts.py -q`
Ожидается: PASS.

- [ ] **Шаг 5: Провести новое поле через БД**

В `sync/agent/db.py` в `AGENT_DDL` после ALTER с `conversions` добавить:

```python
    # Средний объём трафика Директа (0–100) — мера того, сколько показов
    # кампания недобирает на своей ставке. Поля «доля выкупа» в Reports API
    # нет (probe_traffic_headroom, docs/AGENT-DATA-SOURCES.md), а колонка
    # auction_win_share наполнялась только выгрузкой интерфейса времён GAS.
    """
    ALTER TABLE edu_agent_facts
      ADD COLUMN IF NOT EXISTS avg_traffic_vol DOUBLE PRECISION NOT NULL DEFAULT 0
    """,
```

В `load_direct_rows` добавить поле в SELECT:

```python
               w_avg_impr_pos, w_auction_win_share, w_avg_traffic_vol
```

В upsert фактов (`sync/agent/db.py:755-790`) добавить `avg_traffic_vol` в список колонок,
в список `%(...)s` и в блок `ON CONFLICT ... DO UPDATE SET`:

```sql
            avg_traffic_vol = EXCLUDED.avg_traffic_vol,
```

Файл миграции:

```sql
-- migrations/edu/20260825_agent_traffic_volume.sql
-- Средний объём трафика (0–100) в витрине фактов агента: мера недобора показов.
-- Доля выкупа (auction_win_share) остаётся пустой — поля для неё в Reports API
-- нет, наполнялась она только выгрузкой интерфейса эпохи GAS.
ALTER TABLE edu_agent_facts
  ADD COLUMN IF NOT EXISTS avg_traffic_vol DOUBLE PRECISION NOT NULL DEFAULT 0;
```

- [ ] **Шаг 6: Прогнать SQL-тесты и весь набор**

Запуск: `python -m pytest tests/ -q`
Ожидается: PASS. `tests/test_agent_db_sql.py` проверяет соответствие колонок и плейсхолдеров —
если он красный, значит поле добавлено не во все три места upsert.

- [ ] **Шаг 7: Коммит**

```bash
git add sync/agent/facts.py sync/agent/db.py tests/test_agent_facts.py \
        migrations/edu/20260825_agent_traffic_volume.sql
git commit -m "fix(agent): взвешенные метрики Директа доезжают до фактов средними, а не суммами"
```

---

## Задача 3: недобор трафика как отдельная величина

Спека (Ф7.6): «кампания с выкупом 30 % не насыщена, ей есть куда расти, даже если DiD
этого не видит». Считаем это по объёму трафика: 100 — весь доступный трафик позиции,
60 — кампания покупает 60 % того, что могла бы. Это **не** доля выкупа показов и
называть её так нельзя: объём трафика говорит о качестве позиции, а не о доле аукционов.

**Файлы:**
- Создать: `sync/agent/headroom.py`
- Создать: `tests/test_agent_headroom.py`

**Интерфейсы:**
- Потребляет: строки фактов с `campaign_id`, `fact_date`, `impressions`, `cost`,
  `avg_traffic_vol` (задача 2).
- Отдаёт:
  - `traffic_headroom(facts, window_from, window_to) -> Dict[str, Dict[str, Any]]` —
    по `campaign_id`: `{"traffic_volume": float 0..100, "headroom_share": float 0..1,
    "impressions": int, "cost": float, "verdict": str}`, где `verdict` ∈
    `"есть куда расти" | "выкуплен" | "неопределённо"`.
  - `computed_rows(section) -> Dict[str, List[Dict[str, Any]]]` — строки для
    `edu_agent_computed_settings` по `object_id`, `setting_kind="headroom"`.

- [ ] **Шаг 1: Написать падающий тест**

```python
# tests/test_agent_headroom.py
# -*- coding: utf-8 -*-
"""Недобор трафика: сколько показов кампания не покупает на своей ставке."""

from sync.agent.headroom import computed_rows, traffic_headroom

WINDOW = ("2026-08-01", "2026-08-28")


def _fact(day, campaign_id, impressions, volume, cost=1000.0):
    return {"fact_date": day, "campaign_id": campaign_id, "cost": cost,
            "impressions": impressions, "avg_traffic_vol": volume}


def test_volume_is_weighted_by_impressions():
    # День с 9000 показов на объёме 50 весит вдевятеро против дня с 1000 на 100.
    rows = [_fact("2026-08-02", "111", 9000, 50.0),
            _fact("2026-08-03", "111", 1000, 100.0)]
    out = traffic_headroom(rows, *WINDOW)
    assert out["111"]["traffic_volume"] == 55.0
    assert out["111"]["headroom_share"] == 0.45


def test_low_volume_with_enough_impressions_has_room():
    rows = [_fact("2026-08-02", "111", 20000, 45.0)]
    assert traffic_headroom(rows, *WINDOW)["111"]["verdict"] == "есть куда расти"


def test_high_volume_is_bought_out():
    rows = [_fact("2026-08-02", "111", 20000, 95.0)]
    assert traffic_headroom(rows, *WINDOW)["111"]["verdict"] == "выкуплен"


def test_small_campaign_is_undetermined_not_optimistic():
    # 300 показов — объём, на котором среднее ничего не значит. Вердикт
    # «есть куда расти» здесь стал бы поводом долить деньги в шум.
    rows = [_fact("2026-08-02", "111", 300, 20.0)]
    assert traffic_headroom(rows, *WINDOW)["111"]["verdict"] == "неопределённо"


def test_days_outside_window_are_ignored():
    rows = [_fact("2026-07-01", "111", 50000, 10.0),
            _fact("2026-08-02", "111", 20000, 90.0)]
    out = traffic_headroom(rows, *WINDOW)
    assert out["111"]["impressions"] == 20000
    assert out["111"]["traffic_volume"] == 90.0


def test_zero_impressions_campaign_is_absent():
    # Кампания без показов не получает вердикта: делить не на что, а строка
    # с нулевым объёмом читалась бы как «весь трафик недобран».
    assert traffic_headroom([_fact("2026-08-02", "111", 0, 0.0)], *WINDOW) == {}


def test_computed_rows_carry_support():
    section = traffic_headroom([_fact("2026-08-02", "111", 20000, 45.0)], *WINDOW)
    rows = computed_rows(section)["111"]
    by_key = {r["setting_key"]: r for r in rows}
    assert by_key["traffic_volume"]["value"] == 45.0
    assert by_key["headroom_share"]["value"] == 0.55
    assert by_key["traffic_volume"]["support_n"] == 20000
    assert all(r["setting_kind"] == "headroom" for r in rows)
```

- [ ] **Шаг 2: Прогнать и убедиться, что падает**

Запуск: `python -m pytest tests/test_agent_headroom.py -q`
Ожидается: FAIL — `ModuleNotFoundError: No module named 'sync.agent.headroom'`.

- [ ] **Шаг 3: Написать модуль**

```python
# sync/agent/headroom.py
# -*- coding: utf-8 -*-
"""
sync/agent/headroom.py — недобор трафика кампании.

Объём трафика Директа (AvgTrafficVolume, 0–100) показывает, какую долю
доступного трафика позиции получает объявление. 100 — берём всё, что даёт
позиция; 45 — меньше половины. Кампания с низким объёмом НЕ насыщена
независимо от того, что говорит кривая насыщения: расход упирается не в
исчерпанный спрос, а в ставку.

Почему не «доля выкупа». Колонка edu_agent_facts.auction_win_share пуста, и
наполнить её нечем: поля доли показов в Reports API нет (probe_traffic_
headroom, docs/AGENT-DATA-SOURCES.md), исторические значения приходили
выгрузкой интерфейса эпохи GAS. Объём трафика — то, что API отдаёт сегодня;
это другая величина, и называть её долей выкупа нельзя.

Здесь считается ТОЛЬКО признак «есть куда расти». Решение «долить» принимает
портфель (portfolio.py) по экономике: недобор трафика — не причина тратить,
а причина не считать кампанию упершейся в потолок.
"""

from typing import Any, Dict, List

FULL_VOLUME = 100.0

# Порог наблюдаемости. Ниже — среднее по объёму трафика собрано с горстки
# аукционов и скачет сильнее, чем измеряемая величина. 5000 показов за окно —
# примерно 180 показов в день, ниже этого кампании в EDU живут дни-обрывки.
MIN_IMPRESSIONS = 5_000

# Границы вердикта. 70 — ниже этого недобор больше трети, и это уже повод
# усомниться в «насыщении». 90 — выше этого добирать почти нечего, и разница
# с 100 съедается округлением позиций.
ROOM_BELOW_VOLUME = 70.0
BOUGHT_OUT_VOLUME = 90.0


def traffic_headroom(facts: List[Dict[str, Any]],
                     window_from: str, window_to: str) -> Dict[str, Dict[str, Any]]:
    """Недобор трафика по кампаниям за окно.

    Средний объём взвешивается ПОКАЗАМИ, а не днями: день с тысячей показов
    и день с сотней тысяч — не равноправные наблюдения одной величины.
    """
    totals: Dict[str, Dict[str, float]] = {}
    for row in facts:
        day = str(row.get("fact_date"))[:10]
        if day < window_from or day > window_to:
            continue
        impressions = int(row.get("impressions") or 0)
        if impressions <= 0:
            continue
        slot = totals.setdefault(str(row["campaign_id"]),
                                 {"impressions": 0.0, "weighted": 0.0, "cost": 0.0})
        slot["impressions"] += impressions
        slot["weighted"] += float(row.get("avg_traffic_vol") or 0.0) * impressions
        slot["cost"] += float(row.get("cost") or 0.0)

    out: Dict[str, Dict[str, Any]] = {}
    for campaign_id, slot in totals.items():
        impressions = int(slot["impressions"])
        if impressions <= 0:
            continue
        volume = slot["weighted"] / impressions
        if impressions < MIN_IMPRESSIONS:
            verdict = "неопределённо"
        elif volume < ROOM_BELOW_VOLUME:
            verdict = "есть куда расти"
        elif volume >= BOUGHT_OUT_VOLUME:
            verdict = "выкуплен"
        else:
            verdict = "неопределённо"
        out[campaign_id] = {
            "traffic_volume": round(volume, 2),
            "headroom_share": round(max(0.0, FULL_VOLUME - volume) / FULL_VOLUME, 4),
            "impressions": impressions,
            "cost": round(slot["cost"], 2),
            "verdict": verdict,
        }
    return out


def computed_rows(section: Dict[str, Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Строки edu_agent_computed_settings по объектам.

    support_n — показы: сила этого числа измеряется показами, а не лидами,
    и потребитель обязан видеть, на чём оно стоит.
    """
    out: Dict[str, List[Dict[str, Any]]] = {}
    for campaign_id, row in section.items():
        out[str(campaign_id)] = [
            {"setting_kind": "headroom", "setting_key": "traffic_volume",
             "value": row["traffic_volume"], "raw_value": row["traffic_volume"],
             "support_n": row["impressions"], "rel_error": 0.0},
            {"setting_kind": "headroom", "setting_key": "headroom_share",
             "value": row["headroom_share"], "raw_value": row["traffic_volume"],
             "support_n": row["impressions"], "rel_error": 0.0},
        ]
    return out
```

- [ ] **Шаг 4: Прогнать тесты**

Запуск: `python -m pytest tests/test_agent_headroom.py -q`
Ожидается: PASS (7 тестов).

- [ ] **Шаг 5: Коммит**

```bash
git add sync/agent/headroom.py tests/test_agent_headroom.py
git commit -m "feat(agent): недобор трафика — сколько показов кампания не покупает"
```

---

## Задача 4: недобор трафика виден в кривых и приоритетен для разведки

Величина без потребителя бесполезна. Два потребителя:
1. **Отчёт кривых насыщения** — рядом с «насыщается / не насыщена / неопределённо»
   должен стоять недобор трафика: статистика могла не набрать силу, а ставка режет
   объём прямо сейчас.
2. **Карман разведки** (`portfolio.exploration_bonus`, 7 % бюджета) — делит деньги
   пропорционально незнанию. Кампания с большим недобором — это ровно то место, где
   «долить и посмотреть» даёт новое знание дешевле всего.

Экономику λ это не трогает: пороги и целевые бюджеты считаются как прежде.

**Файлы:**
- Изменить: `sync/agent/saturation.py:227-260` (`_curve`), `:263-300` (`saturation_curves`)
- Изменить: `sync/agent/portfolio.py:102-129` (`exploration_bonus`), `:269-330` (`portfolio_targets`)
- Изменить: `sync/agent_e0.py` (передать секцию недобора)
- Изменить: `tests/test_agent_saturation.py`, `tests/test_agent_portfolio.py`

**Интерфейсы:**
- Потребляет: `traffic_headroom(...)` из задачи 3 — словарь по `campaign_id`.
- Отдаёт: у каждой кривой кампании поля `traffic_volume` (float | None),
  `headroom_share` (float | None), `growth_room` (bool | None);
  `exploration_bonus` учитывает `headroom_share` кампании.

- [ ] **Шаг 1: Написать падающие тесты кривых**

Дописать в `tests/test_agent_saturation.py`:

```python
def test_curve_carries_traffic_headroom():
    from sync.agent.saturation import saturation_curves

    facts = [{"fact_date": f"2026-08-{day:02d}", "campaign_id": "111",
              "cost": 1000.0, "eff_leads": 5, "impressions": 3000,
              "avg_traffic_vol": 45.0, "direction": "vpo"}
             for day in range(1, 29)]
    section = saturation_curves(
        facts, [], {"111": "vpo"}, mature_through="2026-08-28",
        headroom_by_campaign={"111": {"traffic_volume": 45.0, "headroom_share": 0.55,
                                      "verdict": "есть куда расти"}})
    curve = section["campaigns"]["111"]
    assert curve["traffic_volume"] == 45.0
    assert curve["headroom_share"] == 0.55
    assert curve["growth_room"] is True


def test_curve_without_headroom_data_says_none_not_false():
    # Отсутствие замера и «замерили, места нет» — разные вещи. False здесь
    # означал бы «расти некуда», а мы просто не знаем.
    from sync.agent.saturation import saturation_curves

    facts = [{"fact_date": f"2026-08-{day:02d}", "campaign_id": "111",
              "cost": 1000.0, "eff_leads": 5, "impressions": 3000,
              "direction": "vpo"}
             for day in range(1, 29)]
    section = saturation_curves(facts, [], {"111": "vpo"}, mature_through="2026-08-28")
    curve = section["campaigns"]["111"]
    assert curve["growth_room"] is None
    assert curve["traffic_volume"] is None
```

- [ ] **Шаг 2: Прогнать и убедиться, что падает**

Запуск: `python -m pytest tests/test_agent_saturation.py -q`
Ожидается: FAIL — `TypeError: saturation_curves() got an unexpected keyword argument
'headroom_by_campaign'`.

- [ ] **Шаг 3: Провести недобор через кривые**

В `sync/agent/saturation.py` расширить `_curve`:

```python
def _curve(eps: Dict[str, float], cost: float, leads: int,
           headroom: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
```

и перед `return` собрать три поля:

```python
    # Недобор трафика — независимый от статистики признак «расти есть куда».
    # Кривая молчит, когда наблюдений мало; ставка режет объём и при молчащей
    # кривой, и это видно сразу.
    volume = (float(headroom["traffic_volume"]) if headroom else None)
    room_share = (float(headroom["headroom_share"]) if headroom else None)
    growth_room = (headroom.get("verdict") == "есть куда расти") if headroom else None
```

и добавить их в возвращаемый словарь:

```python
        "traffic_volume": volume,
        "headroom_share": room_share,
        "growth_room": growth_room,
```

В `saturation_curves` добавить параметр и пробросить его в каждый вызов `_curve`:

```python
def saturation_curves(
    facts: List[Dict[str, Any]],
    quasi_experiments: List[Dict[str, Any]],
    direction_by_campaign: Dict[str, Optional[str]],
    mature_through: str,
    error_floor: Optional[float] = None,
    headroom_by_campaign: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
```

```python
    headroom_by_campaign = headroom_by_campaign or {}
```

в месте вызова `_curve(...)` для кампании — передать `headroom_by_campaign.get(str(campaign_id))`.
Для строк направлений (не кампаний) недобор не передаётся: он не складывается по
направлению линейно, а среднее по чужому весу — та же ошибка «величина посчитана по
чужой популяции», что уже чинилась в Э0.

- [ ] **Шаг 4: Прогнать тесты насыщения**

Запуск: `python -m pytest tests/test_agent_saturation.py -q`
Ожидается: PASS.

- [ ] **Шаг 5: Написать падающий тест разведки**

Дописать в `tests/test_agent_portfolio.py`:

```python
def test_exploration_prefers_campaigns_with_traffic_headroom():
    from sync.agent.portfolio import exploration_bonus

    base = {"value_rel_error": 0.2, "marginal_rel_error": 0.2, "cost": 100_000.0}
    campaigns = [
        {"campaign_id": "111", **base, "headroom_share": 0.6},
        {"campaign_id": "222", **base, "headroom_share": 0.0},
    ]
    bonus = exploration_bonus(campaigns, explore_rub=16_000.0)
    # Незнание и расход равны, отличается только недобор: 1.6 против 1.0.
    assert round(bonus["111"] / bonus["222"], 2) == 1.6
    assert round(bonus["111"] + bonus["222"], 2) == 16_000.0


def test_exploration_without_headroom_is_unchanged():
    from sync.agent.portfolio import exploration_bonus

    campaigns = [
        {"campaign_id": "111", "value_rel_error": 0.2, "marginal_rel_error": 0.0,
         "cost": 100_000.0},
        {"campaign_id": "222", "value_rel_error": 0.1, "marginal_rel_error": 0.0,
         "cost": 100_000.0},
    ]
    bonus = exploration_bonus(campaigns, explore_rub=3_000.0)
    assert round(bonus["111"] / bonus["222"], 2) == 2.0
```

- [ ] **Шаг 6: Прогнать и убедиться, что падает**

Запуск: `python -m pytest tests/test_agent_portfolio.py -q`
Ожидается: FAIL — отношение `1.0` вместо `1.6`.

- [ ] **Шаг 7: Учесть недобор в кармане разведки**

В `sync/agent/portfolio.py::exploration_bonus` заменить расчёт веса:

```python
        rel = math.sqrt(float(campaign.get("value_rel_error") or 0.0) ** 2
                        + float(campaign.get("marginal_rel_error") or 0.0) ** 2)
        # Недобор трафика поднимает ценность разведки: там, где ставка режет
        # объём, доливка отвечает на вопрос «сколько ещё есть», а на выкупленной
        # кампании тот же рубль отвечает только «дороже ли следующий показ».
        # Множитель 1..2 — линейный по недобору, без свободных параметров.
        room = min(max(float(campaign.get("headroom_share") or 0.0), 0.0), 1.0)
        weight = rel * float(campaign.get("cost") or 0.0) * (1.0 + room)
```

В `portfolio_targets` при сборке словаря кампании (там, где берутся `beta` и
`marginal_cpl` из кривой, `sync/agent/portfolio.py:315-316`) добавить:

```python
            "headroom_share": (float(curve["headroom_share"])
                               if curve.get("headroom_share") is not None else 0.0),
```

- [ ] **Шаг 8: Прогнать весь набор**

Запуск: `python -m pytest tests/ -q`
Ожидается: PASS.

- [ ] **Шаг 9: Подключить в такте Э0**

В `sync/agent_e0.py` рядом с расчётом насыщения (перед вызовом `saturation_curves`):

```python
    from sync.agent.headroom import computed_rows as headroom_computed_rows
    from sync.agent.headroom import traffic_headroom

    # Недобор трафика считается по тому же зрелому окну, что кривые: иначе
    # признак «есть куда расти» и вердикт насыщения говорили бы о разных неделях.
    headroom_section = traffic_headroom(facts, slice_from, date_to)
    headroom_rows_written = 0
    for campaign_id, rows in headroom_computed_rows(headroom_section).items():
        agent_db.upsert_computed_settings(
            rows, calc_date=today_iso, object_id=campaign_id,
            object_level="campaign")
        headroom_rows_written += len(rows)
```

передать секцию в кривые:

```python
        headroom_by_campaign=headroom_section,
```

и добавить в печатаемый отчёт (`print(json.dumps({...}))`):

```python
        "traffic_headroom": {
            "campaigns": len(headroom_section),
            "with_room": sum(1 for r in headroom_section.values()
                             if r["verdict"] == "есть куда расти"),
            "bought_out": sum(1 for r in headroom_section.values()
                              if r["verdict"] == "выкуплен"),
            # Расход в кампаниях, которым есть куда расти: цена вопроса.
            "cost_with_room": round(sum(r["cost"] for r in headroom_section.values()
                                        if r["verdict"] == "есть куда расти"), 2),
            "computed_rows": headroom_rows_written,
        },
```

- [ ] **Шаг 10: Прогнать Э0 вхолостую и весь набор**

Запуск: `python -m pytest tests/test_agent_e0.py tests/ -q`
Ожидается: PASS. Боевой прогон Э0 — по крону в 09:30 МСК, отдельно запускать не нужно.

- [ ] **Шаг 11: Коммит**

```bash
git add sync/agent/saturation.py sync/agent/portfolio.py sync/agent_e0.py \
        tests/test_agent_saturation.py tests/test_agent_portfolio.py
git commit -m "feat(agent): недобор трафика в кривых насыщения и в кармане разведки"
```

---

## Задача 5: карта перезапуска обучения стратегий

Спека (Ф6.5): агент двигает целевой CPA и бюджеты, не зная, что часть изменений
перезапускает обучение автостратегии. Справка Директа
(`https://b2b.yandex.ru/adv/edu/materials/strategii-direct`) называет перезапускающими:
выбор другой стратегии, смены модели атрибуции и модели оплаты, **изменение ограничения
расхода**, **корректировку целевых действий** (добавление, смена, удаление), **остановку
кампании более чем на семь дней**. Обучение занимает недели.

Отсюда карта по видам действий движка (`sync/agent/writer/apply.py::to_api_call`):

| Вид действия | Класс | Почему |
|---|---|---|
| `budget.set` | сбрасывает | недельный лимит = ограничение расхода |
| `budget.set_daily` | сбрасывает | дневной лимит = ограничение расхода |
| `tcpa.set` | сбрасывает | цель CPA — параметр целевого действия стратегии |
| `campaign.suspend` | сбрасывает | остановка дольше семи дней |
| `bidmodifier.set` / `bidmodifier.add` | безопасно | корректировок в списке справки нет |
| `negative.add` | безопасно | минус-фразы в списке справки нет |
| `placement.exclude` | безопасно | запрет площадок в списке справки нет |
| `schedule.set` | неизвестно | временного таргетинга в списке нет, но он меняет объём |

Неизвестное остаётся неизвестным: класс `unknown` считается сбрасывающим при отборе
(осторожная сторона), но в отчёте различим — иначе «мы не знаем» навсегда замаскируется
под «мы знаем».

**Файлы:**
- Создать: `sync/agent/writer/learning.py`
- Создать: `tests/test_agent_writer_learning.py`
- Изменить: `sync/agent/writer/db.py` (DDL-ALTER + `last_learning_reset`)
- Изменить: `sync/agent_e1.py:1378-1387` (гейт) и отчёт прогона
- Создать: `migrations/edu/20260825_agent_learning_reset.sql`

**Интерфейсы:**
- Отдаёт:
  - `learning_impact(action: Dict) -> str` — `"resets" | "safe" | "unknown"`.
  - `split_by_learning_cooldown(actions, last_reset_by_object, today, cooldown_days=LEARNING_COOLDOWN_DAYS)
    -> Tuple[List[Dict], List[Dict]]` — (разрешённые, запертые); у запертых
    добавлено `blocked_reason` и `last_learning_reset_at`.
  - `LEARNING_COOLDOWN_DAYS = 14`.
  - `writer_db.last_learning_reset(account: str) -> Dict[str, date]` — по `object_id`
    дата последнего применённого сбрасывающего действия.

- [ ] **Шаг 1: Написать падающий тест**

```python
# tests/test_agent_writer_learning.py
# -*- coding: utf-8 -*-
"""Карта перезапуска обучения: какие действия сбивают стратегию и как часто их можно.

Источник классификации — справка Директа (обучение начинается заново при смене
стратегии, модели атрибуции и оплаты, изменении ограничения расхода,
корректировке целевых действий, остановке кампании дольше семи дней).
"""

from datetime import date

from sync.agent.writer.learning import (
    LEARNING_COOLDOWN_DAYS, learning_impact, split_by_learning_cooldown,
)


def test_budget_and_tcpa_reset_learning():
    assert learning_impact({"action_kind": "budget.set"}) == "resets"
    assert learning_impact({"action_kind": "budget.set_daily"}) == "resets"
    assert learning_impact({"action_kind": "tcpa.set"}) == "resets"
    assert learning_impact({"action_kind": "campaign.suspend"}) == "resets"


def test_modifiers_and_lists_are_safe():
    assert learning_impact({"action_kind": "bidmodifier.set"}) == "safe"
    assert learning_impact({"action_kind": "bidmodifier.add"}) == "safe"
    assert learning_impact({"action_kind": "negative.add"}) == "safe"
    assert learning_impact({"action_kind": "placement.exclude"}) == "safe"


def test_schedule_is_unknown_not_safe():
    # Временного таргетинга в списке справки нет, но он меняет объём показов.
    # Записать его в безопасные значило бы выдать незнание за знание.
    assert learning_impact({"action_kind": "schedule.set"}) == "unknown"


def test_new_action_kind_is_unknown_by_default():
    assert learning_impact({"action_kind": "campaign.resume"}) == "unknown"


def test_safe_actions_pass_cooldown_untouched():
    actions = [{"object_id": "111", "action_kind": "bidmodifier.set"}]
    allowed, blocked = split_by_learning_cooldown(
        actions, {"111": date(2026, 8, 20)}, today=date(2026, 8, 25))
    assert allowed == actions
    assert blocked == []


def test_resetting_action_blocked_inside_cooldown():
    actions = [{"object_id": "111", "action_kind": "budget.set"}]
    allowed, blocked = split_by_learning_cooldown(
        actions, {"111": date(2026, 8, 20)}, today=date(2026, 8, 25))
    assert allowed == []
    assert len(blocked) == 1
    assert str(LEARNING_COOLDOWN_DAYS) in blocked[0]["blocked_reason"]
    assert blocked[0]["last_learning_reset_at"] == "2026-08-20"


def test_resetting_action_passes_after_cooldown():
    actions = [{"object_id": "111", "action_kind": "tcpa.set"}]
    allowed, blocked = split_by_learning_cooldown(
        actions, {"111": date(2026, 8, 1)}, today=date(2026, 8, 25))
    assert allowed == actions
    assert blocked == []


def test_object_without_history_passes():
    actions = [{"object_id": "222", "action_kind": "budget.set"}]
    allowed, blocked = split_by_learning_cooldown(actions, {}, today=date(2026, 8, 25))
    assert allowed == actions


def test_unknown_class_is_treated_as_resetting():
    # Осторожная сторона: неизвестное действие внутри кулдауна не проходит.
    actions = [{"object_id": "111", "action_kind": "schedule.set"}]
    allowed, blocked = split_by_learning_cooldown(
        actions, {"111": date(2026, 8, 24)}, today=date(2026, 8, 25))
    assert allowed == []
    assert blocked[0]["learning_impact"] == "unknown"


def test_two_resetting_actions_on_one_object_in_one_run():
    # Кулдаун смотрит и на уже отобранное в этом же прогоне: два сбрасывающих
    # изменения подряд — это два перезапуска обучения одной кампании за день.
    actions = [{"object_id": "111", "action_kind": "budget.set"},
               {"object_id": "111", "action_kind": "tcpa.set"}]
    allowed, blocked = split_by_learning_cooldown(actions, {}, today=date(2026, 8, 25))
    assert len(allowed) == 1
    assert len(blocked) == 1
```

- [ ] **Шаг 2: Прогнать и убедиться, что падает**

Запуск: `python -m pytest tests/test_agent_writer_learning.py -q`
Ожидается: FAIL — `ModuleNotFoundError: No module named 'sync.agent.writer.learning'`.

- [ ] **Шаг 3: Написать модуль**

```python
# sync/agent/writer/learning.py
# -*- coding: utf-8 -*-
"""
sync/agent/writer/learning.py — какие действия перезапускают обучение стратегии.

Справка Директа (b2b.yandex.ru/adv/edu/materials/strategii-direct): обучение
начинается заново при выборе другой стратегии, смене модели атрибуции или
оплаты, ИЗМЕНЕНИИ ОГРАНИЧЕНИЯ РАСХОДА, КОРРЕКТИРОВКЕ ЦЕЛЕВЫХ ДЕЙСТВИЙ
(добавление, смена, удаление) и остановке кампании дольше семи дней. Пока
стратегия учится заново, её решения хуже — и наблюдение за нашим действием
меряет не наше действие, а переобучение.

Отсюда две обязанности модуля:
  • назвать класс каждого вида действия движка;
  • не давать трогать одну кампанию сбрасывающим действием чаще кулдауна.

Классификация — по видам действий apply.to_api_call, а не по полям payload:
вид действия задаётся движком и меняется только правкой кода, поле payload
может прийти любым.

Неизвестный вид — «unknown», и при отборе он ведёт себя как сбрасывающий.
Обратный дефолт («не знаем — значит безопасно») тихо пропускал бы каждый
новый рычаг: ровно тот довод, по которому рельса ALLOWED_ACTION_KINDS
устроена allow-листом, а не блок-листом.
"""

from datetime import date
from typing import Any, Dict, List, Optional, Tuple

RESETS_LEARNING = {
    "budget.set",        # недельный лимит — ограничение расхода
    "budget.set_daily",  # дневной лимит — оно же
    "tcpa.set",          # цель CPA — параметр целевого действия стратегии
    "campaign.suspend",  # остановка дольше семи дней
}

SAFE_FOR_LEARNING = {
    "bidmodifier.set",
    "bidmodifier.add",
    "negative.add",
    "placement.exclude",
}

# Обучение занимает недели (справка: «прежде чем стратегия покажет наилучшие
# результаты, как правило, проходит несколько недель»). Две недели — нижняя
# граница этого срока: чаще трогать значит мерить переобучение, а не эффект.
LEARNING_COOLDOWN_DAYS = 14

COOLDOWN_REASON = (
    "обучение стратегии перезапускалось меньше {days} дней назад "
    "({last}) — повторное сбрасывающее изменение мерило бы переобучение, "
    "а не эффект действия"
)


def learning_impact(action: Dict[str, Any]) -> str:
    """Класс действия: 'resets' | 'safe' | 'unknown'."""
    kind = str(action.get("action_kind") or "")
    if kind in RESETS_LEARNING:
        return "resets"
    if kind in SAFE_FOR_LEARNING:
        return "safe"
    return "unknown"


def _as_date(value: Any) -> Optional[date]:
    if isinstance(value, date):
        return value
    if not value:
        return None
    return date.fromisoformat(str(value)[:10])


def split_by_learning_cooldown(
    actions: List[Dict[str, Any]],
    last_reset_by_object: Dict[str, Any],
    today: date,
    cooldown_days: int = LEARNING_COOLDOWN_DAYS,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Делит действия на разрешённые и запертые кулдауном обучения.

    Внутри одного прогона счётчик тоже ведётся: два сбрасывающих действия по
    одной кампании в один день — два перезапуска обучения, и второе обязано
    выпасть здесь, а не быть замеченным через две недели по журналу.
    """
    allowed: List[Dict[str, Any]] = []
    blocked: List[Dict[str, Any]] = []
    seen: Dict[str, date] = {}
    for action in actions:
        impact = learning_impact(action)
        if impact == "safe":
            allowed.append(action)
            continue
        object_id = str(action.get("object_id"))
        last = seen.get(object_id) or _as_date(last_reset_by_object.get(object_id))
        if last is not None and (today - last).days < cooldown_days:
            blocked.append({**action,
                            "learning_impact": impact,
                            "blocked_reason": COOLDOWN_REASON.format(
                                days=cooldown_days, last=last.isoformat()),
                            "last_learning_reset_at": last.isoformat()})
            continue
        seen[object_id] = today
        allowed.append({**action, "learning_impact": impact})
    return allowed, blocked
```

- [ ] **Шаг 4: Прогнать тесты модуля**

Запуск: `python -m pytest tests/test_agent_writer_learning.py -q`
Ожидается: PASS (11 тестов).

- [ ] **Шаг 5: Завести колонку журнала и чтение истории**

В `sync/agent/writer/db.py` в список DDL добавить ALTER:

```python
    # Класс действия по влиянию на обучение стратегии (learning.py).
    # Отдельной колонкой, а не выводом из action_kind на чтении: карта
    # классов будет меняться со справкой Директа, а журнал обязан помнить,
    # чем действие считалось В МОМЕНТ ПРИМЕНЕНИЯ — иначе завтрашняя правка
    # карты задним числом перепишет вчерашнюю историю.
    """
    ALTER TABLE edu_agent_actions
      ADD COLUMN IF NOT EXISTS learning_impact TEXT
    """,
```

и функцию чтения:

```python
def last_learning_reset(account: str) -> Dict[str, Any]:
    """По object_id — дата последнего ПРИМЕНЁННОГО сбрасывающего действия.

    Считаются только применённые (applied_at IS NOT NULL): запланированное и
    не ушедшее в кабинет изменение обучение не сбивало.
    """
    rows = _fetch_dicts(
        """
        SELECT object_id, max(applied_at::date) AS last_reset
        FROM edu_agent_actions
        WHERE account = %s
          AND applied_at IS NOT NULL
          AND learning_impact IN ('resets', 'unknown')
        GROUP BY object_id
        """,
        (account,),
    )
    return {str(r["object_id"]): r["last_reset"] for r in rows}
```

Файл миграции:

```sql
-- migrations/edu/20260825_agent_learning_reset.sql
-- Класс действия по влиянию на обучение автостратегии Директа:
-- resets | safe | unknown. Хранится в журнале, а не выводится на чтении:
-- карта классов меняется вслед за справкой, история — нет.
ALTER TABLE edu_agent_actions
  ADD COLUMN IF NOT EXISTS learning_impact TEXT;
```

- [ ] **Шаг 6: Встроить гейт в такт Э1**

В `sync/agent_e1.py` после блока кулдауна вредных сегментов (строка ~1380, сразу за
`blocked += in_cooldown`):

```python
    # Кулдаун ОБУЧЕНИЯ — отдельно от кулдауна вредных сегментов: там мы не
    # трогаем то, что уже навредило, здесь — не сбиваем стратегию чаще, чем
    # она успевает выучиться. Стоит там же, до отбора по лимиту действий:
    # отсечённое здесь не занимает слотов прогона.
    last_resets = writer_db.last_learning_reset(login)
    allowed, in_learning_cooldown = split_by_learning_cooldown(
        allowed, last_resets, today=date.today())
    blocked += in_learning_cooldown
```

Импорт наверху файла:

```python
from sync.agent.writer.learning import (
    learning_impact, split_by_learning_cooldown,
)
```

При записи действия в журнал (там, где собирается строка для `writer_db`) добавить поле:

```python
        "learning_impact": learning_impact(action),
```

В отчёт прогона Э1 добавить секцию:

```python
        "learning": {
            "resets_planned": sum(1 for a in allowed
                                  if learning_impact(a) == "resets"),
            "unknown_planned": sum(1 for a in allowed
                                   if learning_impact(a) == "unknown"),
            "blocked_by_cooldown": len(in_learning_cooldown),
            "cooldown_days": LEARNING_COOLDOWN_DAYS,
        },
```

- [ ] **Шаг 7: Написать тест гейта в такте**

Дописать в `tests/test_agent_e1.py`:

```python
def test_learning_cooldown_blocks_repeated_budget_change(monkeypatch):
    # Бюджет той же кампании двигали 3 дня назад — второе движение мерило бы
    # переобучение стратегии, а не эффект.
    from datetime import date

    from sync.agent.writer.learning import split_by_learning_cooldown

    actions = [{"object_id": "111", "action_kind": "budget.set",
                "idempotency_key": "k1"}]
    allowed, blocked = split_by_learning_cooldown(
        actions, {"111": date(2026, 8, 22)}, today=date(2026, 8, 25))
    assert allowed == []
    assert "обучение стратегии" in blocked[0]["blocked_reason"]
```

- [ ] **Шаг 8: Прогнать весь набор**

Запуск: `python -m pytest tests/ -q`
Ожидается: PASS.

- [ ] **Шаг 9: Коммит**

```bash
git add sync/agent/writer/learning.py sync/agent/writer/db.py sync/agent_e1.py \
        tests/test_agent_writer_learning.py tests/test_agent_e1.py \
        migrations/edu/20260825_agent_learning_reset.sql
git commit -m "feat(agent): карта перезапуска обучения — сбрасывающие действия под кулдауном"
```

---

## Задача 6: слепая доля расхода в каждом отчёте

Спека (Ф7.7): 15 % расхода (3,0 млн ₽/мес) живёт вне `edu_campaign_settings` — Мастер
кампаний и прочее, чего API не отдаёт. Полное закрытие слепой зоны — отдельная работа
(сессия браузера, внутренние отчёты), но минимум обязателен сейчас: каждое число агента
должно нести рядом долю расхода, которую агент не видел.

**Файлы:**
- Создать: `sync/agent/coverage.py`
- Создать: `tests/test_agent_coverage.py`
- Изменить: `sync/agent_e0.py` (отчёт), `sync/agent_e1.py` (отчёт)

**Интерфейсы:**
- Отдаёт: `blind_spend(facts, settings_rows, window_from, window_to) -> Dict[str, Any]`
  с ключами `cost_total`, `cost_blind`, `blind_share`, `campaigns_total`,
  `campaigns_blind`, `sample` (до 10 имён кампаний с наибольшим расходом).

- [ ] **Шаг 1: Написать падающий тест**

```python
# tests/test_agent_coverage.py
# -*- coding: utf-8 -*-
"""Слепая доля: сколько расхода идёт мимо настроек, которые агент читает."""

from sync.agent.coverage import blind_spend

WINDOW = ("2026-08-01", "2026-08-28")


def _fact(campaign_id, cost, name=None, day="2026-08-02"):
    return {"fact_date": day, "campaign_id": campaign_id, "cost": cost,
            "campaign_name": name or f"campaign-{campaign_id}"}


def test_share_counts_cost_not_campaigns():
    facts = [_fact("111", 850_000.0), _fact("222", 150_000.0)]
    out = blind_spend(facts, [{"campaign_id": "111"}], *WINDOW)
    assert out["cost_total"] == 1_000_000.0
    assert out["cost_blind"] == 150_000.0
    assert out["blind_share"] == 0.15
    assert out["campaigns_blind"] == 1


def test_all_covered_gives_zero_share():
    facts = [_fact("111", 100.0)]
    out = blind_spend(facts, [{"campaign_id": "111"}], *WINDOW)
    assert out["blind_share"] == 0.0
    assert out["sample"] == []


def test_no_settings_at_all_is_full_blindness():
    # Пустая витрина настроек — это не «всё видно», а «не видно ничего».
    facts = [_fact("111", 100.0)]
    out = blind_spend(facts, [], *WINDOW)
    assert out["blind_share"] == 1.0


def test_zero_cost_window_does_not_divide_by_zero():
    out = blind_spend([_fact("111", 0.0)], [], *WINDOW)
    assert out["cost_total"] == 0.0
    assert out["blind_share"] == 0.0


def test_sample_is_ordered_by_cost():
    facts = [_fact("111", 10.0, "мелкая"), _fact("222", 900.0, "крупная")]
    out = blind_spend(facts, [], *WINDOW)
    assert [s["campaign_name"] for s in out["sample"]] == ["крупная", "мелкая"]


def test_days_outside_window_are_ignored():
    facts = [_fact("111", 500.0, day="2026-07-01"), _fact("222", 100.0)]
    out = blind_spend(facts, [], *WINDOW)
    assert out["cost_total"] == 100.0
```

- [ ] **Шаг 2: Прогнать и убедиться, что падает**

Запуск: `python -m pytest tests/test_agent_coverage.py -q`
Ожидается: FAIL — `ModuleNotFoundError: No module named 'sync.agent.coverage'`.

- [ ] **Шаг 3: Написать модуль**

```python
# sync/agent/coverage.py
# -*- coding: utf-8 -*-
"""
sync/agent/coverage.py — доля расхода вне видимости агента.

Настройки кампаний агент читает из edu_campaign_settings, которую наполняет
API Директа. Мастер кампаний и часть форматов туда не попадают: замер
25.08.2026 дал 15 % расхода (3,0 млн ₽/мес) мимо витрины. Пока эта зона не
закрыта, каждое число агента обязано нести рядом её размер — доля, посчитанная
по популяции, из которой выпала шестая часть денег, не становится неверной, но
и не имеет права выглядеть полной.

Меряем деньгами, а не числом кампаний: одна невидимая кампания на миллион и
двадцать невидимых по тысяче — разные вещи, и счётчик кампаний их путает.
"""

from typing import Any, Dict, List

SAMPLE_LIMIT = 10


def blind_spend(facts: List[Dict[str, Any]], settings_rows: List[Dict[str, Any]],
                window_from: str, window_to: str) -> Dict[str, Any]:
    """Расход вне витрины настроек за окно."""
    known = {str(r.get("campaign_id")) for r in settings_rows if r.get("campaign_id")}

    cost_by_campaign: Dict[str, float] = {}
    name_by_campaign: Dict[str, str] = {}
    for row in facts:
        day = str(row.get("fact_date"))[:10]
        if day < window_from or day > window_to:
            continue
        campaign_id = str(row["campaign_id"])
        cost_by_campaign[campaign_id] = (cost_by_campaign.get(campaign_id, 0.0)
                                         + float(row.get("cost") or 0.0))
        if row.get("campaign_name"):
            name_by_campaign[campaign_id] = str(row["campaign_name"])

    total = sum(cost_by_campaign.values())
    blind = {cid: cost for cid, cost in cost_by_campaign.items() if cid not in known}
    blind_cost = sum(blind.values())
    sample = sorted(blind.items(), key=lambda kv: -kv[1])[:SAMPLE_LIMIT]

    return {
        "cost_total": round(total, 2),
        "cost_blind": round(blind_cost, 2),
        "blind_share": round(blind_cost / total, 4) if total > 0 else 0.0,
        "campaigns_total": len(cost_by_campaign),
        "campaigns_blind": len(blind),
        "sample": [{"campaign_id": cid,
                    "campaign_name": name_by_campaign.get(cid, ""),
                    "cost": round(cost, 2)}
                   for cid, cost in sample if cost > 0],
    }
```

- [ ] **Шаг 4: Прогнать тесты**

Запуск: `python -m pytest tests/test_agent_coverage.py -q`
Ожидается: PASS (6 тестов).

- [ ] **Шаг 5: Вывести в отчёты обоих тактов**

В `sync/agent_e0.py` рядом со сборкой отчёта:

```python
    from sync.agent.coverage import blind_spend

    # Слепая доля рядом с числами прогона: решения принимаются с поправкой на
    # то, чего агент не видел, а не «в целом по кабинету».
    blind = blind_spend(facts, agent_db.load_campaign_settings_raw(),
                        slice_from, date_to)
```

и в `print(json.dumps({...}))`:

```python
        "blind_spend": blind,
```

В `sync/agent_e1.py` — то же в отчёт прогона записи (данные брать теми же вызовами,
что уже используются в такте: факты окна и `load_campaign_settings_raw`).

- [ ] **Шаг 6: Прогнать весь набор**

Запуск: `python -m pytest tests/ -q`
Ожидается: PASS.

- [ ] **Шаг 7: Коммит**

```bash
git add sync/agent/coverage.py tests/test_agent_coverage.py \
        sync/agent_e0.py sync/agent_e1.py
git commit -m "feat(agent): слепая доля расхода в отчёте каждого такта"
```

---

## Задача 7: спрос как календарь, а не как фон

Спека (Ф7.8): `edu_wordstat_demand` (5338 строк) не привязан к направлениям и потому не
используется. Нужен детектор режима: сезонный подъём или спад спроса меняет ожидания от
кампаний, а не объявляется их провалом. Фразы уже сгруппированы по уровням образования
в `sync/edu_demand.py::EDU_DEMAND_PHRASES`; направления кампаний задаёт
`sync/classify.py::detect_direction` (`spo`, `vpo`, `dist`, `school`, `it`, `med`, `mti`,
`ntb`, `transfer`, `other`).

**Файлы:**
- Создать: `sync/agent/demand.py`
- Создать: `tests/test_agent_demand.py`
- Изменить: `sync/agent/db.py` (чтение спроса), `sync/agent_e0.py` (отчёт)

**Интерфейсы:**
- Отдаёт:
  - `DIRECTION_BY_PHRASE: Dict[str, str]` — фраза Wordstat → направление кампаний.
  - `weekly_demand_by_direction(rows) -> Dict[str, Dict[str, int]]` —
    `{направление: {неделя: частота}}`.
  - `demand_regime(rows, through_week, baseline_weeks=BASELINE_WEEKS)
    -> Dict[str, Dict[str, Any]]` — по направлению `{"last_week", "frequency",
    "baseline_median", "deviation", "sigma", "regime"}`, где `regime` ∈
    `"подъём" | "спад" | "норма" | "мало данных"`.
- Потребляет: строки `edu_wordstat_demand` (`week_start`, `region`, `phrase`, `frequency`).

- [ ] **Шаг 1: Написать падающий тест**

```python
# tests/test_agent_demand.py
# -*- coding: utf-8 -*-
"""Спрос Wordstat как календарь направлений: подъём, спад, норма."""

from sync.agent.demand import (
    DIRECTION_BY_PHRASE, demand_regime, weekly_demand_by_direction,
)


def _row(week, phrase, frequency, region="ru"):
    return {"week_start": week, "region": region, "phrase": phrase,
            "frequency": frequency}


def test_phrases_map_to_campaign_directions():
    assert DIRECTION_BY_PHRASE["колледж"] == "spo"
    assert DIRECTION_BY_PHRASE["вуз"] == "vpo"
    assert DIRECTION_BY_PHRASE["магистратура"] == "vpo"
    assert DIRECTION_BY_PHRASE["заочное обучение"] == "dist"
    # ДПО в классификаторе кампаний отсутствует — направление своё, и это
    # само по себе сигнал: спрос есть, кампаний под него нет.
    assert DIRECTION_BY_PHRASE["переподготовка"] == "dpo"


def test_weekly_sums_phrases_within_direction():
    rows = [_row("2026-08-17", "колледж", 100), _row("2026-08-17", "техникум", 50),
            _row("2026-08-17", "вуз", 200)]
    out = weekly_demand_by_direction(rows)
    assert out["spo"]["2026-08-17"] == 150
    assert out["vpo"]["2026-08-17"] == 200


def test_only_ru_region_counted():
    # Москва — подмножество РФ, и складывать их значит считать москвичей дважды.
    rows = [_row("2026-08-17", "колледж", 100),
            _row("2026-08-17", "колледж", 40, region="msk")]
    assert weekly_demand_by_direction(rows)["spo"]["2026-08-17"] == 100


def test_rise_detected_against_baseline():
    rows = [_row(f"2026-0{6 + w // 4}-{1 + (w % 4) * 7:02d}", "колледж", 100)
            for w in range(8)]
    rows.append(_row("2026-08-17", "колледж", 200))
    out = demand_regime(rows, through_week="2026-08-17")
    assert out["spo"]["regime"] == "подъём"
    assert out["spo"]["frequency"] == 200


def test_flat_series_is_normal():
    rows = [_row(f"2026-0{6 + w // 4}-{1 + (w % 4) * 7:02d}", "колледж", 100)
            for w in range(8)]
    rows.append(_row("2026-08-17", "колледж", 103))
    assert demand_regime(rows, through_week="2026-08-17")["spo"]["regime"] == "норма"


def test_short_history_says_not_enough_data():
    rows = [_row("2026-08-10", "колледж", 100), _row("2026-08-17", "колледж", 300)]
    assert demand_regime(rows, through_week="2026-08-17")["spo"]["regime"] == "мало данных"


def test_unknown_phrase_is_ignored_not_crashing():
    rows = [_row("2026-08-17", "автошкола", 100)]
    assert weekly_demand_by_direction(rows) == {}
```

- [ ] **Шаг 2: Прогнать и убедиться, что падает**

Запуск: `python -m pytest tests/test_agent_demand.py -q`
Ожидается: FAIL — `ModuleNotFoundError: No module named 'sync.agent.demand'`.

- [ ] **Шаг 3: Написать модуль**

```python
# sync/agent/demand.py
# -*- coding: utf-8 -*-
"""
sync/agent/demand.py — рыночный спрос как режим, а не как фон.

edu_wordstat_demand хранит недельную частоту по фразам. Без привязки к
направлениям кампаний это цифры ни о чём: агент видит падение лидов на СПО и
не знает, упал ли рынок целиком.

Режим определяется отклонением последней недели от медианы базового окна в
единицах разброса самого окна. Медиана, а не среднее: одна аномальная неделя
(праздники, сбой выгрузки) сдвигает среднее и делает следующую неделю
«подъёмом». Разброс — медианное абсолютное отклонение по той же причине.

Регион только 'ru': московские строки — подмножество российских, и сложение
считает москвичей дважды.
"""

import statistics
from typing import Any, Dict, List

# Фраза → направление кампаний (sync/classify.py::detect_direction).
# 'dpo' в классификаторе кампаний отсутствует намеренно: под этот спрос
# кампаний нет, и видеть это отдельной строкой полезнее, чем растворить его
# в 'other'.
DIRECTION_BY_PHRASE: Dict[str, str] = {
    "колледж": "spo", "техникум": "spo", "училище": "spo", "ссуз": "spo",
    "среднее профессиональное": "spo",
    "вуз": "vpo", "университет": "vpo", "институт": "vpo",
    "высшее образование": "vpo", "бакалавриат": "vpo", "специалитет": "vpo",
    "магистратура": "vpo", "аспирантура": "vpo",
    "дистанционное обучение": "dist", "дистанционное образование": "dist",
    "заочное обучение": "dist",
    "переподготовка": "dpo", "повышение квалификации": "dpo",
    "профпереподготовка": "dpo",
}

REGION = "ru"

# Базовое окно сравнения: восемь недель до последней. Короче — режим ловится
# на шуме, длиннее — сезонный сдвиг размазывается по собственной базе.
BASELINE_WEEKS = 8

# Минимум недель базы, при котором вердикт вообще выносится.
MIN_BASELINE_WEEKS = 6

# Порог режима в единицах разброса базы. 2 — обычная граница «это не шум»
# для симметричного отклонения.
REGIME_SIGMA = 2.0


def weekly_demand_by_direction(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """{направление: {неделя: суммарная частота фраз направления}}."""
    out: Dict[str, Dict[str, int]] = {}
    for row in rows:
        if str(row.get("region") or REGION) != REGION:
            continue
        direction = DIRECTION_BY_PHRASE.get(str(row.get("phrase") or "").strip())
        if direction is None:
            continue
        week = str(row.get("week_start"))[:10]
        by_week = out.setdefault(direction, {})
        by_week[week] = by_week.get(week, 0) + int(row.get("frequency") or 0)
    return out


def demand_regime(rows: List[Dict[str, Any]], through_week: str,
                  baseline_weeks: int = BASELINE_WEEKS) -> Dict[str, Dict[str, Any]]:
    """Режим спроса по направлениям на неделю through_week."""
    out: Dict[str, Dict[str, Any]] = {}
    for direction, by_week in weekly_demand_by_direction(rows).items():
        weeks = sorted(w for w in by_week if w <= through_week)
        if not weeks:
            continue
        last_week = weeks[-1]
        frequency = by_week[last_week]
        baseline = [by_week[w] for w in weeks[:-1][-baseline_weeks:]]

        row: Dict[str, Any] = {
            "last_week": last_week,
            "frequency": frequency,
            "baseline_median": None,
            "deviation": None,
            "sigma": None,
            "regime": "мало данных",
        }
        if len(baseline) >= MIN_BASELINE_WEEKS:
            median = statistics.median(baseline)
            # Медианное абсолютное отклонение, приведённое к σ нормального
            # распределения множителем 1.4826 — иначе порог в «двух сигмах»
            # означал бы разное на разных рядах.
            mad = statistics.median([abs(v - median) for v in baseline]) * 1.4826
            deviation = frequency - median
            sigma = (deviation / mad) if mad > 0 else 0.0
            row.update({
                "baseline_median": median,
                "deviation": deviation,
                "sigma": round(sigma, 2),
                "regime": ("подъём" if sigma >= REGIME_SIGMA
                           else "спад" if sigma <= -REGIME_SIGMA
                           else "норма"),
            })
        out[direction] = row
    return out
```

- [ ] **Шаг 4: Прогнать тесты**

Запуск: `python -m pytest tests/test_agent_demand.py -q`
Ожидается: PASS (7 тестов).

- [ ] **Шаг 5: Подключить чтение и вывести в отчёт Э0**

В `sync/agent/db.py` добавить чтение спроса:

```python
def load_wordstat_demand(week_from: str) -> List[Dict[str, Any]]:
    """Недельный спрос Wordstat с указанной недели (регион 'ru' и 'msk')."""
    return _fetch_dicts(
        """
        SELECT week_start, region, phrase, frequency
        FROM edu_wordstat_demand
        WHERE week_start >= %s
        """,
        (week_from,),
    )
```

В `sync/agent_e0.py`:

```python
    from sync.agent.demand import demand_regime

    # Спрос за 26 недель: базовое окно 8 недель плюс запас на дыры выгрузки.
    demand_rows = agent_db.load_wordstat_demand(
        (date.today() - timedelta(weeks=26)).isoformat())
    demand = demand_regime(demand_rows, through_week=date_to)
```

и в отчёт:

```python
        "demand_regime": demand,
```

- [ ] **Шаг 6: Прогнать весь набор**

Запуск: `python -m pytest tests/ -q`
Ожидается: PASS.

- [ ] **Шаг 7: Коммит**

```bash
git add sync/agent/demand.py sync/agent/db.py sync/agent_e0.py \
        tests/test_agent_demand.py
git commit -m "feat(agent): спрос Wordstat как режим направлений — подъём, спад, норма"
```

---

## Задача 8: свести волну в документации и прогнать боевой такт

**Файлы:**
- Изменить: `docs/AGENT-ROADMAP-2026-08-25.md`
- Изменить: `docs/AGENT-DATA-SOURCES.md`

- [ ] **Шаг 1: Прогнать Э0 в бою**

Запуск: вкладка Actions → `agent-e0` → Run workflow (или дождаться крона 09:30 МСК).
Проверить в логе четыре новые секции отчёта: `traffic_headroom`, `blind_spend`,
`demand_regime` и — в отчёте Э1 (`agent-e1`, репетиция 11:00 МСК) — `learning`.

- [ ] **Шаг 2: Записать боевые числа**

В `docs/AGENT-DATA-SOURCES.md` дописать раздел с фактическими числами прогона:
сколько кампаний с недобором трафика и на какой расход, слепая доля, режимы спроса
по направлениям, сколько сбрасывающих действий пришлось на такт и сколько заперто
кулдауном.

- [ ] **Шаг 3: Отметить закрытые пункты спеки**

В `docs/AGENT-ROADMAP-2026-08-25.md` пометить Ф7.6, Ф7.7 (минимум — счётчик), Ф7.8, Ф6.5
как выполненные со ссылкой на этот план; уточнить, что Ф7.7 закрыт счётчиком, а не
самой слепой зоной — она остаётся открытой задачей.

- [ ] **Шаг 4: Коммит и пуш**

```bash
git add docs/AGENT-ROADMAP-2026-08-25.md docs/AGENT-DATA-SOURCES.md
git commit -m "docs(agent): волна 1 — недобор трафика, слепая доля, режим спроса, обучение"
git pull --rebase
git push
```

---

## Что дальше (отдельными планами)

1. **Ф5 — контур «оптимизация → запуск»**: наряд билдеру из данных агента, паспорт
   продукта в смысловой слой, обратная связь по запущенным кампаниям.
2. **Ф8 — генератор идей**: две ветки (масштабирование доказанного и тесты гипотез),
   очередь гипотез с приоритетом «ожидаемая ценность ÷ цена теста».
3. **Ф9 — боевая работа**: первое применение узким скоупом, лестница автономии по
   классам действий через shadow-режим, пейсинг месяца вместо трейлинг-28д.

Открытые решения Павла (нужны к плану Ф9, не к этой волне): скоуп первого применения,
`target_romi` (1.0 или 2.0), поднимать ли недельный риск-бюджет (сейчас 50 000 ₽).
