# Агент-директолог, волна 1: полнота картины и уважение к обучению — план реализации

> **Для агентов-исполнителей:** ОБЯЗАТЕЛЬНЫЙ СУБ-СКИЛЛ: `superpowers:subagent-driven-development`
> (рекомендуется) или `superpowers:executing-plans`. Шаги отмечены чекбоксами `- [ ]`.

**Цель:** закрыть три дыры в картине мира агента (недобор трафика, слепая зона расхода,
спрос как календарь), научить его не сбивать обучение автостратегий Директа и не дать
ему сходиться к «эффективно, но мало»: каждое сокращение обязано иметь адресата роста.

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
- **Модуль без вызова из боевого кода — красный гейт.** `tests/test_no_orphan_code.py`
  требует, чтобы каждая публичная функция из `sync/agent/**` вызывалась из `sync/`,
  `scripts/` или `main.py`; вызов из тестов не считается. Поэтому интеграция в такт
  (`sync/agent_e0.py` / `sync/agent_e1.py`) — не отдельный шаг «потом», а часть той же
  задачи: без неё её нельзя закоммитить. Проверено на задаче 6 (замер: 1 failed).
- **Настройки кампаний приходят словарём, не списком.** `agent_db.load_campaign_settings_raw()`
  (`sync/agent/db.py:329`) возвращает `Dict[campaign_id, settings]`. Код, написанный под
  список строк (`for r in rows: r.get("campaign_id")`), падает `AttributeError` на первом
  боевом прогоне, а тесты с литералами-списками этого не ловят.
- **Реальная сигнатура солвера** — `portfolio_targets(campaigns, ladder_by_object,
  login_by_campaign_id, holdout_ids=..., explore_share=...)` (`sync/agent_e0.py:986`),
  а не `portfolio_targets(campaigns, budget=...)`. Задачи 9 и 11 добавляют к ней
  параметры (`budget`, `frozen`) — примеры вызовов в их тестах сокращены для
  читаемости и на шаге реализации приводятся к настоящей сигнатуре.
- Ветка: `agent-wave1` (правило параллельных сессий: `git add` только своими путями,
  никогда `git add -A`).
- Ничего из этой волны не применяется в боевой кабинет: рычаги эта волна не добавляет,
  только меняет отбор и отчётность.
- **Рост и эффективность вместе** (решение Павла 25.08): агент не имеет права сводить
  оптимизацию к сокращению. Освободившиеся деньги обязаны получить адресата, такт с
  суммарным минусом по ожидаемым лидам не применяется целиком, и каждый такт — даже
  без единого сокращения — предъявляет список того, что можно усилить (задачи 9, 10).
- **Порог обучения по бюджету** (решение Павла 25.08): изменение недельного или
  дневного лимита в пределах ±20 % обучение стратегии не сбрасывает; рост до ×2
  оправдан адресно, при доказанном недоборе трафика (задачи 5, 8).
- **Тратить больше, когда хорошо окупаемся** (решение Павла 25.08): общая сумма
  кабинета перестаёт быть константой — при предельной окупаемости выше цели с запасом
  агент растит её шагом до 20 % за такт, в пределах месячного потолка из панели
  настроек. Потолок не задан — рост только предлагается числом (задача 11).
- **Ожидание пишется рядом с действием, факт — рядом с исходом** (задачи 12, 13):
  без пары «прогноз / результат» в журнале калибровка модели неизмерима, а класс
  надёжности `A` даёт только контроль-заповедник за то же окно, но не авторство
  действия.
- **Рост не покупает мусор** (задача 14): доливка бюджета останавливается по раннему
  прокси качества когорты (средний ML-скор оплаты), не дожидаясь денежного
  чекпоинта на 35-й день.

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

| Тип кампании | Показов за 30 дней | Из них с непустым объёмом трафика |
|---|---|---|
| Поиск (TEXT_CAMPAIGN) | <из лога> | <из лога> |
| Сети / смарт / МК | <из лога> | <из лога> |

Этот разрез — обязательная часть probe, а не украшение: объём трафика определён для
поиска, и если в сетях он приходит пустым, витрина запишет ноль. Ноль, прочитанный
как «недобор 100 %», вылил бы карман разведки и шаг ×2 в сети по несуществующему
признаку. Порог `MIN_VOLUME_COVERAGE` (задача 3) настраивается по этому числу.

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


def test_zero_volume_with_live_impressions_is_no_data_not_full_headroom():
    # Объём трафика определён для ПОИСКА. У кампании в сетях поле приходит
    # пустым (в витрине — нулём), а показов при этом десятки тысяч. Прочитать
    # такой ноль как «недобор 100 %» значит объявить всю сетевую часть
    # кабинета недоливаемой и вылить в неё карман разведки и шаг x2.
    rows = [_fact("2026-08-02", "111", 40_000, 0.0)]
    out = traffic_headroom(rows, *WINDOW)
    assert out["111"]["verdict"] == "неопределённо"
    assert out["111"]["headroom_share"] is None


def test_partial_coverage_of_volume_is_undetermined():
    # Покрытие = доля показов, пришедшихся на дни с ненулевым объёмом.
    # Половина показов из дней без объёма — среднее считается по другому
    # набору дней, чем показы, и сравнивать его с порогом нельзя.
    rows = [_fact("2026-08-02", "111", 20_000, 0.0),
            _fact("2026-08-03", "111", 20_000, 40.0)]
    assert traffic_headroom(rows, *WINDOW)["111"]["verdict"] == "неопределённо"


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

# Доля показов окна, пришедшихся на дни с НЕНУЛЕВЫМ объёмом трафика. Поле
# определено для поиска; в сетях API отдаёт «--», а sync/direct.py:117
# (to_num_gas) превращает прочерк в ноль — признак «поля не было» до витрины
# не доезжает вовсе. Различать «объём ноль» и «объём не измерялся» приходится
# по косвенному: у живой поисковой кампании объём положителен почти каждый
# день, у сетевой — ноль всегда. Кампания с покрытием ниже этой доли вердикта
# не получает: недобор у неё неизвестен, а не полный.
MIN_VOLUME_COVERAGE = 0.8

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

   Оговорка, без которой разведка уходит в пустоту: надбавка — это ДЕНЬГИ, а деньги
   тратятся только там, где лимит связывает расход (9 кампаний из 62,
   `docs/AGENT-AUDIT-2026-08-23.md:214`). Кампания с недобором трафика и
   несвязывающим лимитом разведочную надбавку просто не выберет — прибавка
   зафиксируется в отчёте и не превратится ни в показы, ни в знание. Поэтому
   множитель недобора в весе применяется только к кампаниям со связывающим лимитом;
   остальным недобор трафика вес не поднимает, а попадает в список кандидатов на
   эскалацию цены (задача 10, `lever = "tcpa"`).

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
| `budget.set` | **по величине**: ≤ 20 % — безопасно, больше — сбрасывает | практика Павла: изменение недельного бюджета в пределах ±20 % обучение не сбивает; справка говорит про «изменение ограничения расхода» без порога, порог — из практики ведения кабинетов |
| `budget.set_daily` | **по величине**, тот же порог | дневной лимит — то же ограничение расхода |
| `tcpa.set` | сбрасывает | цель CPA — параметр целевого действия стратегии |
| `campaign.suspend` | сбрасывает | остановка дольше семи дней |
| `bidmodifier.set` / `bidmodifier.add` | безопасно | корректировок в списке справки нет |
| `negative.add` | безопасно | минус-фразы в списке справки нет |
| `placement.exclude` | безопасно | запрет площадок в списке справки нет |
| `schedule.set` | неизвестно | временного таргетинга в списке нет, но он меняет объём |

Неизвестное остаётся неизвестным: класс `unknown` считается сбрасывающим при отборе
(осторожная сторона), но в отчёте различим — иначе «мы не знаем» навсегда замаскируется
под «мы знаем».

**Половина этого механизма в кабинете уже работает — не строить второй.**
`sync/agent/writer/budget.py::apply_cooldown` + `BUDGET_COOLDOWN_DAYS = 14`
применяются в `sync/agent_e1.py:1157` к бюджетным действиям и в `:1293` к целевому
CPA, по факту журнала (`writer_db.recent_action_objects`). Параллельный кулдаун
обучения дал бы два независимых запрета на одно действие и две разные причины в
отчёте. Задача 5 **расширяет существующий**, а не дублирует:

- `learning_impact` даёт классификацию (что именно сбрасывает обучение и почему) —
  сейчас кулдаун применяется к видам действий списком, без объяснения;
- под кулдаун добавляются `campaign.suspend` и **возобновление после паузы дольше
  семи дней** — сейчас они не под ним вовсе, а обучение сбрасывают;
- `last_learning_reset` даёт отчёту дату и причину последнего сброса по объекту.

**И ещё одно, важнее остального:** `writer/budget.py::MAX_WRITE_STEP = 0.20` зажимает
любой бюджетный сдвиг до ±20 % от недельного расхода — то есть **сбрасывающих
бюджетных действий движок сегодня физически не порождает**. Порог из практики Павла
уже зашит в писателя. Классификация «по величине» становится не отбором, а
проверкой инварианта — до тех пор, пока задача 8 не откроет адресный шаг ×2. Это
надо понимать при приёмке: пустой список сбрасывающих бюджетных действий в первом
прогоне — ожидаемый результат, а не признак того, что гейт не работает.

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


def _budget(new_micros, old_micros):
    return {"action_kind": "budget.set",
            "payload": {"WeeklySpendLimit": new_micros},
            "previous_state": {"WeeklySpendLimit": old_micros}}


def test_tcpa_and_suspend_reset_learning():
    assert learning_impact({"action_kind": "tcpa.set"}) == "resets"
    assert learning_impact({"action_kind": "campaign.suspend"}) == "resets"


def test_small_budget_change_is_safe():
    # ±20 % недельного бюджета обучение не сбивают (практика ведения кабинетов).
    assert learning_impact(_budget(1_200_000_000, 1_000_000_000)) == "safe"
    assert learning_impact(_budget(850_000_000, 1_000_000_000)) == "safe"


def test_big_budget_change_resets():
    assert learning_impact(_budget(1_500_000_000, 1_000_000_000)) == "resets"
    assert learning_impact(_budget(500_000_000, 1_000_000_000)) == "resets"


def test_daily_budget_uses_same_threshold():
    action = {"action_kind": "budget.set_daily",
              "payload": {"DailyBudget": {"Amount": 110_000_000}},
              "previous_state": {"DailyBudget": {"Amount": 100_000_000}}}
    assert learning_impact(action) == "safe"


def test_budget_without_previous_value_is_unknown():
    # Прежнего лимита не прочитали — величину изменения не посчитать, и
    # «безопасно» здесь было бы догадкой.
    assert learning_impact({"action_kind": "budget.set",
                            "payload": {"WeeklySpendLimit": 1_000_000_000},
                            "previous_state": {}}) == "unknown"


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
    actions = [{"object_id": "111", **_budget(1_500_000_000, 1_000_000_000)}]
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
    actions = [{"object_id": "222", **_budget(1_500_000_000, 1_000_000_000)}]
    allowed, blocked = split_by_learning_cooldown(actions, {}, today=date(2026, 8, 25))
    assert len(allowed) == 1 and blocked == []


def test_small_budget_step_passes_inside_cooldown():
    # Главный смысл порога: перелив в пределах ±20 % идёт каждый такт, даже
    # если стратегию перезапускали вчера. Иначе перераспределение встало бы.
    actions = [{"object_id": "111", **_budget(1_150_000_000, 1_000_000_000)}]
    allowed, blocked = split_by_learning_cooldown(
        actions, {"111": date(2026, 8, 24)}, today=date(2026, 8, 25))
    assert len(allowed) == 1 and blocked == []


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
    "tcpa.set",          # цель CPA — параметр целевого действия стратегии
    "campaign.suspend",  # остановка дольше семи дней
}

SAFE_FOR_LEARNING = {
    "bidmodifier.set",
    "bidmodifier.add",
    "negative.add",
    "placement.exclude",
}

# Бюджетные действия судятся ВЕЛИЧИНОЙ, а не видом. Справка называет
# перезапускающим «изменение ограничения расхода» без порога, но на практике
# ведения кабинетов сдвиг лимита в пределах ±20 % стратегию не сбивает —
# а именно такими шагами и работает перераспределение (portfolio.py двигает
# бюджеты каждый такт). Записать весь класс в сбрасывающие значило бы
# запереть перелив кулдауном в две недели и остановить главный механизм.
BUDGET_KINDS = {"budget.set", "budget.set_daily"}
BUDGET_SAFE_DELTA = 0.20

# Обучение занимает недели (справка: «прежде чем стратегия покажет наилучшие
# результаты, как правило, проходит несколько недель»). Две недели — нижняя
# граница этого срока: чаще трогать значит мерить переобучение, а не эффект.
LEARNING_COOLDOWN_DAYS = 14

COOLDOWN_REASON = (
    "обучение стратегии перезапускалось меньше {days} дней назад "
    "({last}) — повторное сбрасывающее изменение мерило бы переобучение, "
    "а не эффект действия"
)


def _budget_values(action: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    """Новый и прежний лимит действия (в микрорублях), если оба читаемы."""
    payload = action.get("payload") or {}
    previous = action.get("previous_state") or {}
    if str(action.get("action_kind")) == "budget.set_daily":
        new = (payload.get("DailyBudget") or {}).get("Amount")
        old = (previous.get("DailyBudget") or {}).get("Amount")
    else:
        new = payload.get("WeeklySpendLimit")
        old = previous.get("WeeklySpendLimit")
    try:
        return (float(new), float(old)) if new and old else (None, None)
    except (TypeError, ValueError):
        return None, None


def learning_impact(action: Dict[str, Any]) -> str:
    """Класс действия: 'resets' | 'safe' | 'unknown'."""
    kind = str(action.get("action_kind") or "")
    if kind in BUDGET_KINDS:
        new, old = _budget_values(action)
        if not new or not old:
            # Величину изменения не посчитать — а «безопасно» без неё догадка.
            return "unknown"
        return "safe" if abs(new / old - 1.0) <= BUDGET_SAFE_DELTA else "resets"
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
Ожидается: PASS (все тесты файла).

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

**Чего этот счётчик НЕ ловит — сказать прямо.** Знаменатель `cost_total` берётся из
`edu_agent_facts`, то есть из того же Reports API, что и вся витрина. Слепота
меряется внутри витрины: «расход, у которого агент не видел настроек». Расход, не
попавший в саму витрину, остаётся невидимым и для этого счётчика — доля выйдет
оптимистичной, а не честной. Поэтому шаг приёмки: **один раз сверить сумму расхода
витрины за календарный месяц с суммой в кабинете** (интерфейс Директа, «Общая
статистика» по всем кампаниям, с НДС/без — привести к одной базе) и записать
расхождение в `docs/AGENT-DATA-SOURCES.md`. Совпало — знаменатель полон, и
`blind_share` можно читать как есть. Не совпало — в отчёте печатается вторая строка
«расход вне витрины», и полное закрытие слепой зоны становится не отчётностью, а
работой с источником.

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

**Два ограничения, которые обязаны быть видимыми, а не молчаливыми.**

1. **Ряд есть не у всех направлений.** `EDU_DEMAND_PHRASES` покрывает СПО, ВПО,
   дистант и ДПО. Направления `school`, `it`, `med`, `mti`, `ntb`, `transfer` фраз не
   имеют вовсе — и среди них `school` — самое свежее направление кабинета (запуск
   14.08.2026). Такие направления получают `regime = "нет ряда"`, отдельно от
   «мало данных» (ряд есть, но короткий): первое лечится добавлением фраз в
   `sync/edu_demand.py`, второе — временем. Слить их в один вердикт значит навсегда
   спрятать дыру в семантике спроса. Список направлений без ряда печатается в отчёте
   Э0 отдельной строкой.
2. **Регион берётся один — `ru`.** В витрине есть и `msk`, но гео кампании нигде не
   вычисляется: `classify.normalize_city_ip_segment` работает по городу ЛИДА, а не по
   настройкам кампании, и сопоставить московский срез спроса с московскими кампаниями
   сейчас нечем. Всероссийский ряд включает Москву, поэтому режим по нему —
   консервативное приближение, а не ошибка; но у московских кампаний сезон может
   отличаться, и это записывается ограничением в `docs/AGENT-DATA-SOURCES.md`, а не
   умалчивается. Разрез по гео — работа для Ф8, когда гео кампании начнёт сниматься из
   настроек (`edu_agent_objects`).

```python
def test_direction_without_phrases_is_distinguishable():
    # «Нет ряда» и «мало данных» — разные диагнозы: первое чинится семантикой,
    # второе — временем. Один вердикт на оба прячет дыру навсегда.
    out = demand_regime([_row("2026-08-17", "колледж", 100)], through_week="2026-08-17")
    assert out["school"]["regime"] == "нет ряда"
```

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

## Задача 8: рост бюджета до ×2, когда недобор доказан

Кап шага сейчас ×1.5 за такт (`portfolio.MAX_STEP_UP`), рельса движка — ×1.6
(`guardrails.BUDGET_RATIO_MAX`). Решение Павла: рост в два раза бывает оправдан.
Открывать его всем нельзя — кап ×1.5 стоит там потому, что дальше собственных
наблюдений кривая превращается в экстраполяцию. Значит ×2 разрешается адресно, под
три условия сразу:

1. **недобор трафика доказан** — `growth_room is True` (задача 3): деньги упрутся в
   реально доступные показы, а не в выдуманный спрос;
2. **кривая не спорит** — `beta < 1` и вердикт кривой не «насыщается»;
3. **экономика с запасом** — `marginal_roi_vs_lambda >= BIG_STEP_ROI_MARGIN` (1.5):
   предельный рубль возвращает в полтора раза больше порога кабинета;
4. **лимит связывает расход** — кампания добирает до текущего недельного лимита хотя бы
   `BINDING_SHARE` (`writer/budget.py:80`). Без этого условия шаг ×2 бессмыслен по
   механике: замер рычага (`probe_budget_lever`, `docs/AGENT-AUDIT-2026-08-23.md:214`)
   показал, что «вверх» лимитом применимо к **9 кампаниям из 62** — у остальных лимит
   не связывает, они не выбирают и текущий. Поднятый им потолок не купит ни одного
   показа, а в журнал уедет отказ `NOT_APPLICABLE_UP_REASON`. Проверка в писателе уже
   стоит; условие дублируется в солвере, чтобы он не назначал заведомо неисполнимое и
   не считал эти деньги распределёнными.

Шаг ×2 всегда сбрасывает обучение (задача 5: больше 20 %), поэтому он автоматически
попадает под кулдаун 14 дней — повторить его на следующем такте не выйдет.

**Главное препятствие — не в портфеле.** Даже подняв `portfolio.MAX_STEP_UP` и
`guardrails.BUDGET_RATIO_MAX`, шага ×2 в кабинете не будет: писатель зажимает целевой
лимит по `writer/budget.py::clamp_write_step` с `MAX_WRITE_STEP = 0.20` — до ±20 % от
недельного РАСХОДА. Солвер посчитает ×2, рельса пропустит, а в кабинет уедет +20 %,
и тесты при этом останутся зелёными: они не доходят до писателя. Поэтому задача 8
обязана тронуть три уровня, а не два:

1. `portfolio.step_cap_up` — сколько солвер вправе назначить;
2. `guardrails.BUDGET_RATIO_MAX` — коридор рельсы (поднять до 2.1: рельса ловит слом
   единиц, а не политику, и должна оставаться шире политики);
3. `writer/budget.py` — `clamp_write_step` получает шаг **параметром**, а не константой
   модуля, и `budget_actions` передаёт в него шаг именно этой кампании:

```python
            # Кап записи — не глобальная константа, а решение по кампании:
            # ±20 % по умолчанию (столько можно, не сбивая обучение), больше —
            # только там, где солвер доказал недобор трафика и запас
            # окупаемости. Оставить здесь константу значило бы тихо
            # обнулять адресный шаг x2 на последнем метре.
            step = float(move.get("write_step") or MAX_WRITE_STEP)
            target_micros = clamp_write_step(target_micros, spend, max_step=step)
```

`write_step` кладёт в move солвер: `step_cap_up(campaign) - 1.0` (×1.5 → 0.5, ×2.0 →
1.0), и по умолчанию — `MAX_WRITE_STEP`. Тест обязателен именно на этом стыке:

```python
def test_double_step_reaches_the_account():
    # Без параметризации clamp_write_step шаг x2 умирал в писателе:
    # солвер назначал 200 000, а в кабинет уезжало 120 000.
    actions, _ = budget_actions(
        desired={"111": _move(target_28d=200_000.0, ratio=2.0, write_step=1.0)},
        actual_by_campaign={"111": _text_campaign(weekly_micros=25_000 * MICROS)},
        weekly_spend_no_vat={"111": 25_000.0})
    assert actions[0]["payload"]["WeeklySpendLimit"] == 50_000 * MICROS
```

**Файлы:**
- Изменить: `sync/agent/portfolio.py:79-100` (`_target_spend`), `:20-62` (константы)
- Изменить: `sync/agent/writer/guardrails.py:48-49` (`BUDGET_RATIO_MAX`)
- Изменить: `tests/test_agent_portfolio.py`, `tests/test_agent_writer_guardrails.py`

**Интерфейсы:**
- Потребляет: поля кампании `headroom_share`, `growth_room` (задачи 3–4), `beta`,
  `marginal_roi_vs_lambda`.
- Отдаёт: `step_cap_up(campaign) -> float` — ×1.5 по умолчанию, ×2.0 при трёх условиях.

- [ ] **Шаг 1: Написать падающий тест**

```python
def test_step_cap_is_1_5_by_default():
    from sync.agent.portfolio import step_cap_up

    assert step_cap_up({"growth_room": False, "beta": 0.6,
                        "marginal_roi_vs_lambda": 3.0}) == 1.5


def test_step_cap_is_2_when_headroom_and_economics_agree():
    from sync.agent.portfolio import step_cap_up

    assert step_cap_up({"growth_room": True, "beta": 0.6,
                        "marginal_roi_vs_lambda": 1.6}) == 2.0


def test_step_cap_stays_1_5_without_headroom_proof():
    # Экономика хорошая, но объём брать негде: ×2 просто поднимет цену клика.
    from sync.agent.portfolio import step_cap_up

    assert step_cap_up({"growth_room": None, "beta": 0.6,
                        "marginal_roi_vs_lambda": 3.0}) == 1.5


def test_step_cap_stays_1_5_when_curve_is_superlinear():
    from sync.agent.portfolio import step_cap_up

    assert step_cap_up({"growth_room": True, "beta": 1.2,
                        "marginal_roi_vs_lambda": 3.0}) == 1.5


def test_step_cap_stays_1_5_on_thin_margin():
    from sync.agent.portfolio import step_cap_up

    assert step_cap_up({"growth_room": True, "beta": 0.6,
                        "marginal_roi_vs_lambda": 1.1}) == 1.5
```

- [ ] **Шаг 2: Прогнать и убедиться, что падает**

Запуск: `python -m pytest tests/test_agent_portfolio.py -q`
Ожидается: FAIL — `ImportError: cannot import name 'step_cap_up'`.

- [ ] **Шаг 3: Ввести адресный кап**

В `sync/agent/portfolio.py` рядом с `MAX_STEP_UP`:

```python
# Расширенный кап шага вверх. Обычный ×1.5 стоит на том, что дальше кривая —
# экстраполяция. Но когда недобор трафика доказан замером (headroom.py),
# кривая не спорит (β<1) и предельный рубль возвращает в полтора раза больше
# порога кабинета — экстраполяции нет: деньги идут в показы, которые уже
# существуют и сейчас достаются конкурентам. Шаг ×2 сбрасывает обучение
# (writer/learning.py) и потому сам себя ограничивает кулдауном в 14 дней.
BIG_STEP_UP = 2.0
BIG_STEP_ROI_MARGIN = 1.5


def step_cap_up(campaign: Dict[str, Any]) -> float:
    """Потолок шага вверх для кампании: ×1.5 обычно, ×2 при доказанном недоборе."""
    if (campaign.get("growth_room") is True
            and float(campaign.get("beta") or 1.0) < 1.0
            and float(campaign.get("marginal_roi_vs_lambda") or 0.0) >= BIG_STEP_ROI_MARGIN):
        return BIG_STEP_UP
    return MAX_STEP_UP
```

В `_target_spend` заменить жёсткий `MAX_STEP_UP` на `step_cap_up(campaign)`.
`marginal_roi_vs_lambda` внутри `_target_spend` считается как
`campaign["value"] / (lam * campaign["marginal_cpl"])` — передать его в словарь
кампании перед вызовом (там же, где считается `log_ratio`), чтобы `step_cap_up`
получил готовое число, а не пересчитывал λ.

В `portfolio_targets` при сборке словаря кампании добавить `growth_room`:

```python
            "growth_room": curve.get("growth_room"),
```

- [ ] **Шаг 4: Поднять рельсу движка**

В `sync/agent/writer/guardrails.py`:

```python
# Верхняя граница расширена под адресный шаг ×2 (portfolio.step_cap_up):
# рельса ловит не политику, а слом единиц (недели вместо дней — ×7,
# микрорубли вместо рублей — ×10⁶), и ×2.1 от них по-прежнему далеко.
BUDGET_RATIO_MAX = 2.1
```

В `tests/test_agent_writer_guardrails.py` поправить тест, фиксирующий прежнюю
границу, и добавить:

```python
def test_double_budget_passes_but_sevenfold_does_not():
    # ×2 — политика (адресный рост), ×7 — недели вместо дней.
    assert _check_budget(_budget_action(new=200.0, cost=100.0))[0] is True
    assert _check_budget(_budget_action(new=700.0, cost=100.0))[0] is False
```

- [ ] **Шаг 5: Прогнать весь набор**

Запуск: `python -m pytest tests/ -q`
Ожидается: PASS.

- [ ] **Шаг 6: Коммит**

```bash
git add sync/agent/portfolio.py sync/agent/writer/guardrails.py \
        tests/test_agent_portfolio.py tests/test_agent_writer_guardrails.py
git commit -m "feat(agent): шаг бюджета ×2, когда недобор трафика доказан и экономика с запасом"
```

---

## Задача 9: сокращение не бывает без адресата роста

Требование Павла: агент не должен сходиться к «эффективно, но мало». Каждое
сокращение обязано иметь адресата — куда переливаются освободившиеся деньги, — а
такт целиком не имеет права ухудшать ожидаемый объём лидов и оплат.

Что уже устроено правильно: солвер портфеля сохраняет сумму кабинета
(`portfolio_targets`, «сумма целевых = бюджету»), то есть срезание одной кампании
автоматически становится доливкой другой. Чего нет:

- **выключение кампании** (`campaign_switch/suspend`) освобождает деньги, которые в
  этом же такте никому не назначаются: солвер считал цели на бюджете, куда
  выключаемая кампания ещё входила;
- **минус-фразы и запреты площадок** режут расход, и это нигде не компенсируется;
- **нетто-эффект такта** считается (`expected_leads_delta`, `expected_revenue_delta`),
  но ни на что не влияет: план с суммарным минусом по лидам применяется так же, как
  план с плюсом.

**Файлы:**
- Создать: `sync/agent/balance.py`
- Создать: `tests/test_agent_balance.py`
- Изменить: `sync/agent_e1.py` (гейт перед отбором по лимиту + секция отчёта)

**Интерфейсы:**
- Отдаёт:
  - `tact_balance(moves, suspends, cuts) -> Dict[str, Any]` — `{"freed_rub",
    "added_rub", "unassigned_rub", "expected_leads_delta",
    "expected_payments_delta", "shrinking"}`, где `shrinking` — True, если такт
    в сумме уменьшает ожидаемые лиды.
  - `require_growth_address(actions, balance) -> Tuple[List[Dict], List[Dict]]` —
    при сжимающем такте снимает сокращающие действия, начиная с самых слабых по
    ожидаемому выигрышу, пока такт не перестанет быть сжимающим; снятые получают
    `blocked_reason`.
  - `MIN_ASSIGNED_SHARE = 0.9` — доля освободившихся денег, которая обязана быть
    назначена адресатам.
  - `EMERGENCY_KINDS` — виды действий, которые гейт НЕ трогает никогда.

**Аварийные сокращения гейту не подчиняются.** Откат по красной линии, реакция на
обвал расхода, запрет вредного сегмента по вердикту сторожа — это защита денег, а не
оптимизация объёма, и требовать под них адресата роста значит запереть тормоз. В
`require_growth_address` такие действия проходят насквозь, а такт от них сжимающим
не считается:

```python
# Аварийное сокращение — не оптимизация, а тормоз. Требовать под откат
# «куда переливаем» значит держать заведомо убыточное изменение живым до
# тех пор, пока солвер не найдёт адресата. Такие действия гейт пропускает
# всегда, и в баланс такта они входят только справочно.
EMERGENCY_KINDS = frozenset({"campaign.rollback", "segment.harmful_block",
                             "budget.crash_guard"})


def _is_emergency(action):
    return (str(action.get("action_kind") or "") in EMERGENCY_KINDS
            or bool(action.get("emergency")))
```

**Снятое действие обязано вернуть свои деньги в раскладку.** Гейты стоят ПОСЛЕ
солвера: и этот, и кулдаун обучения (задача 5), и существующий `apply_cooldown`
снимают уже посчитанные сдвиги. Инвариант «Σ целевых = бюджету» при этом остаётся
верным в расчёте и ломается в кабинете: деньги, назначенные запертой кампании, не
уезжают никуда, а адресаты роста рассчитывали на её сокращение. Правило: **список
запертых объектов возвращается в солвер вторым проходом**, где их бюджеты
зафиксированы на текущем уровне, а свободные деньги раскладываются между остальными.

```python
    # Второй проход, а не пропорциональная добивка: доливать всем поровну
    # значит игнорировать предельную окупаемость, ради которой солвер и
    # существует. Проход дешёвый — это чистая функция на десятках строк.
    locked = {a["object_id"] for a in blocked_by_cooldown + blocked_by_balance}
    if locked:
        targets = portfolio_targets(campaigns, budget=budget, frozen=locked)
```

Тест:

```python
def test_locked_campaign_money_is_redistributed_not_lost():
    campaigns = [_c("111", cost=100_000.0, lam_ratio=0.5),   # заперта кулдауном
                 _c("222", cost=100_000.0, lam_ratio=3.0)]
    rows = portfolio_targets(campaigns, budget=200_000.0, frozen={"111"})
    by_id = {r["campaign_id"]: r for r in rows}
    assert by_id["111"]["target_28d"] == 100_000.0        # заморожена на месте
    assert by_id["222"]["target_28d"] == 100_000.0        # своё, а не чужое сверху
    assert sum(r["target_28d"] for r in rows) == 200_000.0
```

Точный состав аварийного множества сверить на шаге реализации с
`sync/agent/writer/apply.py::to_api_call` и путями отката в `sync/agent_e1_watchdog.py`: имя вида в журнале —
источник правды, а не этот список. Вида, которого нет в `to_api_call`, в
`EMERGENCY_KINDS` быть не должно; если аварийность в коде выражена не отдельным
видом, а флагом на действии — читать флаг (`_is_emergency` покрывает оба случая).

Тест на вычет обязателен:

```python
def test_emergency_cut_passes_gate_even_when_tact_shrinks():
    # Откат по красной линии режет расход и адресата не имеет по определению.
    # Гейт, снявший откат, оставил бы в кабинете изменение, уже признанное
    # убыточным, — это дороже сжатия объёма.
    actions = [{"action_kind": "campaign.rollback", "expected_leads_delta": -20.0}]
    kept, blocked = require_growth_address(
        actions, {"shrinking": True, "freed_rub": 50_000.0, "added_rub": 0.0})
    assert kept == actions
    assert blocked == []
```

- [ ] **Шаг 1: Написать падающий тест**

```python
# tests/test_agent_balance.py
# -*- coding: utf-8 -*-
"""Баланс такта: сокращение обязано иметь адресата, такт — не сжимать объём.

Механизм оптимизации, у которого единственный рычаг — резать неэффективное,
сходится к «дорого и мало»: каждая итерация улучшает среднее и уменьшает
объём. Требование продукта — рост И эффективность, поэтому у сокращения
обязан быть адресат, а такт целиком не имеет права уменьшать ожидаемые лиды.
"""

from sync.agent.balance import require_growth_address, tact_balance


def _move(cid, cost, target, leads_delta):
    return {"campaign_id": cid, "cost_28d": cost, "target_28d": target,
            "expected_leads_delta": leads_delta}


def test_freed_money_is_matched_by_additions():
    moves = [_move("111", 100_000.0, 60_000.0, -8.0),
             _move("222", 100_000.0, 140_000.0, +12.0)]
    balance = tact_balance(moves, suspends=[], cuts=[])
    assert balance["freed_rub"] == 40_000.0
    assert balance["added_rub"] == 40_000.0
    assert balance["unassigned_rub"] == 0.0
    assert balance["shrinking"] is False


def test_suspend_without_reassignment_leaves_money_unassigned():
    # Выключение кампании освобождает её расход, и он обязан быть кому-то отдан.
    moves = [_move("222", 100_000.0, 100_000.0, 0.0)]
    balance = tact_balance(moves, suspends=[{"campaign_id": "111",
                                             "cost_28d": 50_000.0,
                                             "expected_leads_delta": -4.0}],
                           cuts=[])
    assert balance["freed_rub"] == 50_000.0
    assert balance["unassigned_rub"] == 50_000.0
    assert balance["shrinking"] is True


def test_negative_and_placement_cuts_count_as_shrink():
    balance = tact_balance([], suspends=[],
                           cuts=[{"kind": "negative.add", "cost_saved": 12_000.0,
                                  "expected_leads_delta": -1.0}])
    assert balance["freed_rub"] == 12_000.0
    assert balance["shrinking"] is True


def test_growth_gate_drops_weakest_cut_until_tact_grows():
    # Сжимающий такт: два сокращения и одна доливка. Снимается то сокращение,
    # чей выигрыш меньше, — пока баланс не перестанет быть отрицательным.
    actions = [
        {"action_kind": "campaign.suspend", "object_id": "111",
         "expected_leads_delta": -9.0, "expected_gain_rub": 1_000.0},
        {"action_kind": "negative.add", "object_id": "333",
         "expected_leads_delta": -3.0, "expected_gain_rub": 200.0},
        {"action_kind": "budget.set", "object_id": "222",
         "expected_leads_delta": +8.0, "expected_gain_rub": 5_000.0},
    ]
    allowed, blocked = require_growth_address(
        actions, {"expected_leads_delta": -4.0, "shrinking": True})
    assert [a["object_id"] for a in blocked] == ["333"]
    assert len(allowed) == 2


def test_growth_gate_is_noop_when_tact_grows():
    actions = [{"action_kind": "budget.set", "object_id": "222",
                "expected_leads_delta": +8.0, "expected_gain_rub": 5_000.0}]
    allowed, blocked = require_growth_address(
        actions, {"expected_leads_delta": 8.0, "shrinking": False})
    assert allowed == actions and blocked == []


def test_growth_gate_keeps_cut_when_nothing_else_left():
    # Единственное действие такта — сокращение, компенсировать нечем. Оно
    # снимается: пустой такт честнее такта, который только сжимает.
    actions = [{"action_kind": "campaign.suspend", "object_id": "111",
                "expected_leads_delta": -9.0, "expected_gain_rub": 1_000.0}]
    allowed, blocked = require_growth_address(
        actions, {"expected_leads_delta": -9.0, "shrinking": True})
    assert allowed == []
    assert "адресат" in blocked[0]["blocked_reason"]
```

- [ ] **Шаг 2: Прогнать и убедиться, что падает**

Запуск: `python -m pytest tests/test_agent_balance.py -q`
Ожидается: FAIL — `ModuleNotFoundError: No module named 'sync.agent.balance'`.

- [ ] **Шаг 3: Написать модуль**

```python
# sync/agent/balance.py
# -*- coding: utf-8 -*-
"""
sync/agent/balance.py — баланс такта: рост и эффективность, а не эффективность вместо роста.

Механизм, у которого единственный рычаг — резать неокупающееся, монотонно
улучшает средние и монотонно уменьшает объём: через полгода кабинет
эффективен и вдвое меньше. Продукту нужен рост ПРИ эффективности, поэтому:

  • освободившиеся деньги обязаны иметь адресата (солвер портфеля это уже
    делает переливом, выключение кампаний и минус-фразы — нет);
  • такт, который в сумме уменьшает ожидаемые лиды, не применяется целиком:
    слабейшие сокращения снимаются, пока баланс не станет неотрицательным.

Считаем в ожидаемых ЛИДАХ, а не в рублях расхода: рубль, снятый с плохой
кампании и отданный хорошей, — это плюс, а рубль, просто снятый, — минус,
и различает их только ожидаемый результат.
"""

from typing import Any, Dict, List, Tuple

# Доля освободившихся денег, которая обязана быть назначена адресатам.
# Не 100 %: округления шага и капы оставляют хвост, придираться к нему значит
# блокировать такт из-за копеек.
MIN_ASSIGNED_SHARE = 0.9

NO_ADDRESS_REASON = (
    "сокращение без адресата роста: такт в сумме уменьшает ожидаемые лиды "
    "({delta:+.1f}), а компенсировать нечем — усиление не найдено"
)


def tact_balance(moves: List[Dict[str, Any]], suspends: List[Dict[str, Any]],
                 cuts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Сколько такт освобождает, сколько назначает и куда идёт объём."""
    freed = 0.0
    added = 0.0
    leads_delta = 0.0
    for move in moves:
        delta = float(move.get("target_28d") or 0.0) - float(move.get("cost_28d") or 0.0)
        if delta < 0:
            freed += -delta
        else:
            added += delta
        leads_delta += float(move.get("expected_leads_delta") or 0.0)
    for suspend in suspends:
        freed += float(suspend.get("cost_28d") or 0.0)
        leads_delta += float(suspend.get("expected_leads_delta") or 0.0)
    for cut in cuts:
        freed += float(cut.get("cost_saved") or 0.0)
        leads_delta += float(cut.get("expected_leads_delta") or 0.0)

    unassigned = max(0.0, freed - added)
    return {
        "freed_rub": round(freed, 2),
        "added_rub": round(added, 2),
        "unassigned_rub": round(unassigned, 2),
        "assigned_share": round(added / freed, 4) if freed > 0 else 1.0,
        "expected_leads_delta": round(leads_delta, 1),
        "shrinking": bool(leads_delta < 0
                          or (freed > 0 and added / freed < MIN_ASSIGNED_SHARE)),
    }


def require_growth_address(
    actions: List[Dict[str, Any]], balance: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Снимает слабейшие сокращения, пока такт остаётся сжимающим.

    Слабейшее — с наименьшим ожидаемым выигрышем в рублях: снимать сначала то,
    что меньше всего даёт. Порядок детерминирован (выигрыш, затем object_id),
    иначе два прогона на одних данных дали бы разные планы.
    """
    if not balance.get("shrinking"):
        return actions, []

    delta = float(balance.get("expected_leads_delta") or 0.0)
    shrinkers = sorted(
        (a for a in actions if float(a.get("expected_leads_delta") or 0.0) < 0),
        key=lambda a: (float(a.get("expected_gain_rub") or 0.0), str(a.get("object_id"))))

    blocked: List[Dict[str, Any]] = []
    for action in shrinkers:
        if delta >= 0:
            break
        delta -= float(action.get("expected_leads_delta") or 0.0)
        blocked.append({**action,
                        "blocked_reason": NO_ADDRESS_REASON.format(
                            delta=float(balance.get("expected_leads_delta") or 0.0))})

    blocked_ids = {id(a) for a in shrinkers[:len(blocked)]}
    allowed = [a for a in actions if id(a) not in blocked_ids]
    return allowed, blocked
```

- [ ] **Шаг 4: Прогнать тесты**

Запуск: `python -m pytest tests/test_agent_balance.py -q`
Ожидается: PASS (6 тестов).

- [ ] **Шаг 5: Встроить в такт Э1**

В `sync/agent_e1.py` сразу после кулдауна обучения (задача 5) и до `cap_actions`:

```python
    # Баланс такта: сокращение без адресата роста не применяется. Стоит до
    # отбора по лимиту по той же причине, что кулдауны: снятое здесь не должно
    # занимать слот прогона.
    balance = tact_balance(moves_of_run, suspends_of_run, cuts_of_run)
    allowed, without_address = require_growth_address(allowed, balance)
    blocked += without_address
```

`moves_of_run` — строки `budget_threshold["accounts"][login]["moves"]` этого кабинета;
`suspends_of_run` — действия `campaign.suspend` этого прогона с расходом кампании;
`cuts_of_run` — минус-фразы и площадки с полем `cost_saved` (оно уже считается в
отчёте Э0: `cost_burned` у кандидатов).

В отчёт прогона:

```python
        "balance": balance,
```

- [ ] **Шаг 6: Прогнать весь набор**

Запуск: `python -m pytest tests/ -q`
Ожидается: PASS.

- [ ] **Шаг 7: Коммит**

```bash
git add sync/agent/balance.py tests/test_agent_balance.py sync/agent_e1.py
git commit -m "feat(agent): сокращение без адресата роста не применяется — баланс такта"
```

---

## Задача 10: каждый такт отвечает «что усилить»

Вторая половина того же требования: даже когда агент ничего не режет, он обязан
предъявить, что можно усилить. Полный генератор идей — Ф8 (отдельный план); здесь
собирается список из данных, которые уже есть после задач 3–7.

Четыре источника кандидатов на усиление:

1. **недобор трафика при хорошей экономике** — `growth_room is True` и
   `marginal_roi_vs_lambda >= 1`: показы существуют, окупаемость есть, мешает ставка;
2. **упор в кап шага** — целевой бюджет уткнулся в потолок ×1.5/×2: солвер хотел дать
   больше, чем разрешено за такт;
3. **направления в режиме «подъём»** (задача 7): рынок растёт — время добавлять, а не
   удерживать;
4. **`expansion_candidates`** — конверсионные запросы без своей группы (уже считаются
   в Э0, `sync/agent/objects.py`).

**У каждого кандидата обязан быть рычаг, а не только повод.** Асимметрия рычага —
свойство механики Директа, а не наша политика: вниз лимит связывает всегда, вверх —
только там, где расход уже упирается в лимит (9 из 62 кампаний,
`docs/AGENT-AUDIT-2026-08-23.md:214`). Остальным доливка бюджета не даст ничего:
единственный способ вырасти для них — **поднять целевой CPA**, то есть разрешить
стратегии платить дороже за конверсию. Поэтому у кандидата стоит поле
`lever ∈ {"budget", "tcpa"}`:

- `budget` — лимит связывает расход (`spend >= BINDING_SHARE × limit`);
- `tcpa` — лимит не связывает; кампания недобирает трафик из-за цены, и рычаг —
  `tcpa.set` вверх, со всеми его последствиями (сбрасывает обучение, задача 5, и потому
  под кулдауном 14 дней).

Список усиления без рычага был бы списком благих пожеланий: на 53 кампаниях из 62 он
предлагал бы долить денег туда, где деньги не тратятся.

Плюс отдельной строкой — `room_rub_total`: суммарный запас по кампаниям, у которых
предельная окупаемость выше λ и есть недобор трафика. Он **разбит по рычагам**
(`room_rub_budget`, `room_rub_tcpa`): первое — деньги, которые кабинет освоит сразу
поднятием лимитов; второе — деньги, которые освоятся только после эскалации цены, и
считать их доступными сегодня нельзя. Это число потребляет задача 11
(рост общей суммы кабинета) как ответ на вопрос «есть ли куда потратить прибавку», и
оно же печатается человеку как основание поднять месячный план освоения. Внутримесячный
пейсинг остаётся за Ф9.

**Файлы:**
- Создать: `sync/agent/growth.py`
- Создать: `tests/test_agent_growth.py`
- Изменить: `sync/agent_e0.py` (секция отчёта)

**Интерфейсы:**
- Отдаёт: `growth_candidates(portfolio_section, headroom_section, demand_section,
  expansion, quality_drift=None) -> Dict[str, Any]` с ключами `candidates` (список
  `{campaign_id, source, reason, headroom_share, roi_vs_lambda, room_rub}`),
  `capped_by_step` (сколько кампаний уткнулись в кап), `room_rub_total`,
  `room_rub_budget`, `room_rub_tcpa`, `directions_rising`, `skipped_by_quality`.
  У каждого кандидата — поле `lever` (`"budget"` при связывающем лимите, иначе
  `"tcpa"`).
  `quality_drift` — словарь из задачи 14; параметр объявляется здесь пустым по
  умолчанию, чтобы задача 14 не переписывала сигнатуру и вызовы.

- [ ] **Шаг 1: Написать падающий тест**

```python
# tests/test_agent_growth.py
# -*- coding: utf-8 -*-
"""Кандидаты на усиление: что агент предлагает УСИЛИТЬ на каждом такте."""

from sync.agent.growth import growth_candidates

PORTFOLIO = {"accounts": {"acc": {"lambda": 1.0, "moves": {
    "111": {"campaign_id": "111", "cost_28d": 100_000.0, "target_28d": 150_000.0,
            "marginal_roi_vs_lambda": 2.0, "step_capped": True},
    "222": {"campaign_id": "222", "cost_28d": 100_000.0, "target_28d": 90_000.0,
            "marginal_roi_vs_lambda": 0.8, "step_capped": False},
}}}}
HEADROOM = {"111": {"headroom_share": 0.5, "verdict": "есть куда расти"},
            "222": {"headroom_share": 0.02, "verdict": "выкуплен"}}
DEMAND = {"vpo": {"regime": "подъём"}, "spo": {"regime": "норма"}}


def test_campaign_with_room_and_economics_is_candidate():
    out = growth_candidates(PORTFOLIO, HEADROOM, DEMAND, expansion=[])
    ids = [c["campaign_id"] for c in out["candidates"]]
    assert "111" in ids
    assert "222" not in ids


def test_step_capped_campaigns_counted():
    out = growth_candidates(PORTFOLIO, HEADROOM, DEMAND, expansion=[])
    assert out["capped_by_step"] == 1


def test_rising_directions_listed():
    out = growth_candidates(PORTFOLIO, HEADROOM, DEMAND, expansion=[])
    assert out["directions_rising"] == ["vpo"]


def test_room_rub_counts_only_profitable_with_headroom():
    out = growth_candidates(PORTFOLIO, HEADROOM, DEMAND, expansion=[])
    # 111: цель выше факта на 50 000 и упёрлась в кап — запас считается по ней.
    assert out["room_rub_total"] == 50_000.0


def test_expansion_candidates_carried_through():
    out = growth_candidates(PORTFOLIO, HEADROOM, DEMAND,
                            expansion=[{"query": "колледж заочно", "headroom": 3_000.0}])
    sources = {c["source"] for c in out["candidates"]}
    assert "expansion" in sources


def test_empty_inputs_give_empty_answer_not_crash():
    out = growth_candidates({"accounts": {}}, {}, {}, expansion=[])
    assert out["candidates"] == []
    assert out["room_rub_total"] == 0.0
```

- [ ] **Шаг 2: Прогнать и убедиться, что падает**

Запуск: `python -m pytest tests/test_agent_growth.py -q`
Ожидается: FAIL — `ModuleNotFoundError: No module named 'sync.agent.growth'`.

- [ ] **Шаг 3: Написать модуль**

```python
# sync/agent/growth.py
# -*- coding: utf-8 -*-
"""
sync/agent/growth.py — что усилить: ответ такта на вторую половину задачи.

Агент, который умеет только сокращать, честно ведёт кабинет к «эффективно и
мало». Поэтому каждый такт обязан предъявить список усиления — даже когда
сокращать нечего.

Полный генератор гипотез — отдельная работа (Ф8 роадмапа). Здесь собираются
кандидаты из уже посчитанного: недобор трафика при живой экономике, упор в
кап шага, направления с растущим спросом, конверсионные запросы без своей
группы.

room_rub_total — сколько агент долил бы, если бы общий бюджет кабинета не был
прибит к трейлинг-28д. Сам он его не двигает: план освоения задаёт человек.
Число нужно для того, чтобы это решение принималось с цифрой на руках.
"""

from typing import Any, Dict, List

# Порог экономики для кандидата на усиление: предельный рубль должен как
# минимум окупаться относительно порога кабинета.
MIN_ROI_VS_LAMBDA = 1.0


def growth_candidates(portfolio_section: Dict[str, Any],
                      headroom_section: Dict[str, Dict[str, Any]],
                      demand_section: Dict[str, Dict[str, Any]],
                      expansion: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Кандидаты на усиление и денежный запас роста."""
    candidates: List[Dict[str, Any]] = []
    capped = 0
    room_total = 0.0

    for account in (portfolio_section.get("accounts") or {}).values():
        for campaign_id, move in (account.get("moves") or {}).items():
            headroom = headroom_section.get(str(campaign_id)) or {}
            roi = float(move.get("marginal_roi_vs_lambda") or 0.0)
            has_room = headroom.get("verdict") == "есть куда расти"
            if move.get("step_capped"):
                capped += 1
            if not has_room or roi < MIN_ROI_VS_LAMBDA:
                continue
            room = max(0.0, float(move.get("target_28d") or 0.0)
                       - float(move.get("cost_28d") or 0.0))
            room_total += room
            candidates.append({
                "campaign_id": str(campaign_id),
                "source": "headroom",
                "reason": "недобор трафика при окупающемся предельном рубле",
                "headroom_share": headroom.get("headroom_share"),
                "roi_vs_lambda": round(roi, 2),
                "room_rub": round(room, 2),
            })

    for item in expansion or []:
        candidates.append({
            "campaign_id": None,
            "source": "expansion",
            "reason": f"конверсионный запрос без своей группы: {item.get('query')}",
            "headroom_share": None,
            "roi_vs_lambda": None,
            "room_rub": round(float(item.get("headroom") or 0.0), 2),
        })

    rising = sorted(d for d, row in (demand_section or {}).items()
                    if row.get("regime") == "подъём")
    return {
        "candidates": sorted(candidates, key=lambda c: -(c["room_rub"] or 0.0)),
        "capped_by_step": capped,
        "room_rub_total": round(room_total, 2),
        "directions_rising": rising,
    }
```

- [ ] **Шаг 4: Прогнать тесты**

Запуск: `python -m pytest tests/test_agent_growth.py -q`
Ожидается: PASS (6 тестов).

- [ ] **Шаг 5: Пометить упор в кап и вывести секцию в Э0**

В `sync/agent/portfolio.py::_move_row` добавить поле (кап берётся из `step_cap_up`
задачи 8):

```python
        # Уткнулись ли в потолок шага: солвер хотел дать больше, чем разрешено
        # за такт. Это кандидат на усиление, а не законченное решение.
        "step_capped": bool(target >= campaign["cost"] * step_cap_up(campaign) - 1e-6),
```

В `sync/agent_e0.py` после расчёта портфеля:

```python
    from sync.agent.growth import growth_candidates

    growth = growth_candidates(budget_threshold, headroom_section, demand, expansion)
```

и в отчёт:

```python
        "growth": growth,
```

- [ ] **Шаг 6: Прогнать весь набор**

Запуск: `python -m pytest tests/ -q`
Ожидается: PASS.

- [ ] **Шаг 7: Коммит**

```bash
git add sync/agent/growth.py sync/agent/portfolio.py sync/agent_e0.py \
        tests/test_agent_growth.py
git commit -m "feat(agent): каждый такт отвечает, что усилить, и во сколько это оценивается"
```

---

## Задача 11: бюджет кабинета растёт, когда окупаемость с запасом

Сейчас сумма по кабинету — константа: солвер держит инвариант «Σ целевых = бюджету»,
а бюджет берётся из трейлинг-28д факта. Значит агент физически не может потратить
больше, сколько бы хорошо ни окупался. Решение Павла: **можно и больше тратить, если
хорошо окупаемся.**

Правило роста общей суммы:

- растём, только когда λ кабинета (предельная окупаемость) **выше цели с запасом**:
  `lambda_breakeven` истинно и `λ ≥ target_romi · GROWTH_LAMBDA_MARGIN` (1.2);
- шаг роста — **не больше 20 % за такт**: ровно тот порог, за которым начинается
  перезапуск обучения (задача 5), поэтому рост суммы сам по себе стратегии не сбивает;
- есть куда потратить **сегодня**: `room_rub_budget` (задача 10) больше нуля — именно
  он, а не `room_rub_total`. Запас, доступный только через поднятие целевого CPA
  (`room_rub_tcpa`), в рост общей суммы не входит: деньги, которые кабинет физически
  не выберет, превратятся в невязку и будут каждый такт изображать доступный резерв.
  Он печатается отдельной строкой как основание для отдельного решения — эскалировать
  цену;
- потолок — **месячный план освоения из панели настроек** (`edu_agent_config`,
  ключ `monthly_budget_cap_rub`). Пока ключ пуст, рост не применяется, а только
  предлагается в отчёте: сколько и почему. Общий бюджет — деньги Павла, и цифру
  потолка ставит он.

**Невязка прибавки.** Бюджет кабинета вырос, а капы шага по кампаниям
(`MAX_STEP_UP`, кап записи ±`MAX_WRITE_STEP` в `writer/budget.py`) могут не дать
разложить прибавку целиком: сумма целевых окажется меньше нового бюджета, и инвариант
«Σ целевых = бюджету» сломается — тихо, без единого исключения. Правило: **остаток
не размазывается, а снимается.** После раскладки солвер сверяет сумму и, если
назначено меньше, уменьшает бюджет кабинета до фактически назначенного, а разницу
показывает в отчёте отдельным числом:

```python
    assigned = sum(t["target_28d"] for t in targets.values())
    if assigned < budget - 1.0:
        # Прибавка, которую капы шага не дают разложить, — не деньги в работе,
        # а невязка. Оставить её в бюджете значит каждый такт «дораспределять»
        # призрак: солвер видел бы недоданные рубли и задирал цели тем, кто и
        # так упёрся в кап. Признаём назначенное и переносим остаток на
        # следующий такт, когда капы отсчитаются от нового расхода.
        out["deferred_growth_rub"] = round(budget - assigned, 2)
        budget = assigned
```

Тест:

```python
def test_growth_beyond_step_caps_is_deferred_not_smeared():
    # Одна кампания, кап шага +50 %: прибавку +100 % разложить некуда.
    rows = portfolio_targets(_one_campaign(cost=100_000.0), budget=200_000.0)
    assert rows[0]["target_28d"] == 150_000.0
    assert rows[0]["deferred_growth_rub"] == 50_000.0
```

**Файлы:**
- Изменить: `sync/agent/portfolio.py::portfolio_targets` (бюджет кабинета как параметр)
- Изменить: `sync/agent/config.py` (ключ `monthly_budget_cap_rub`)
- Изменить: `sync/agent_e0.py` (передать потолок, вывести предложение в отчёт)
- Изменить: `tests/test_agent_portfolio.py`, `tests/test_agent_config.py`

**Интерфейсы:**
- Отдаёт: `account_budget(current_cost, lam, target_romi, room_rub, monthly_cap)
  -> Dict[str, Any]` — `{"budget": float, "growth_rub": float, "capped_by": str}`,
  где `capped_by` ∈ `"step" | "monthly_cap" | "room" | "lambda" | "none"`.
- Потребляет: в параметр `room_rub` подаётся **`room_rub_budget`** (задача 10) — запас
  кампаний со связывающим лимитом, а не `room_rub_total`. Плюс `lambda` и `budget_28d`
  кабинета. Подать сюда total значит вырастить бюджет на сумму, которую кабинет
  физически не выберет.

- [ ] **Шаг 1: Написать падающий тест**

```python
def test_budget_grows_when_lambda_has_margin():
    from sync.agent.portfolio import account_budget

    out = account_budget(current_cost=1_000_000.0, lam=2.5, target_romi=2.0,
                         room_rub=500_000.0, monthly_cap=5_000_000.0)
    assert out["budget"] == 1_200_000.0     # шаг +20 %
    assert out["growth_rub"] == 200_000.0
    assert out["capped_by"] == "step"


def test_no_growth_without_lambda_margin():
    from sync.agent.portfolio import account_budget

    out = account_budget(current_cost=1_000_000.0, lam=2.1, target_romi=2.0,
                         room_rub=500_000.0, monthly_cap=5_000_000.0)
    assert out["budget"] == 1_000_000.0
    assert out["capped_by"] == "lambda"


def test_no_growth_without_place_to_spend():
    # Окупаемость есть, а недобора нет: прибавка купит те же показы дороже.
    from sync.agent.portfolio import account_budget

    out = account_budget(current_cost=1_000_000.0, lam=3.0, target_romi=2.0,
                         room_rub=0.0, monthly_cap=5_000_000.0)
    assert out["budget"] == 1_000_000.0
    assert out["capped_by"] == "room"


def test_growth_limited_by_room():
    from sync.agent.portfolio import account_budget

    out = account_budget(current_cost=1_000_000.0, lam=3.0, target_romi=2.0,
                         room_rub=50_000.0, monthly_cap=5_000_000.0)
    assert out["growth_rub"] == 50_000.0
    assert out["capped_by"] == "room"


def test_monthly_cap_is_hard_ceiling():
    from sync.agent.portfolio import account_budget

    out = account_budget(current_cost=1_000_000.0, lam=3.0, target_romi=2.0,
                         room_rub=500_000.0, monthly_cap=1_050_000.0)
    assert out["budget"] == 1_050_000.0
    assert out["capped_by"] == "monthly_cap"


def test_without_cap_growth_is_proposed_not_applied():
    # Потолок не задан — сумма не меняется, но предложение посчитано.
    from sync.agent.portfolio import account_budget

    out = account_budget(current_cost=1_000_000.0, lam=3.0, target_romi=2.0,
                         room_rub=500_000.0, monthly_cap=None)
    assert out["budget"] == 1_000_000.0
    assert out["growth_rub"] == 0.0
    assert out["proposed_growth_rub"] == 200_000.0
```

- [ ] **Шаг 2: Прогнать и убедиться, что падает**

Запуск: `python -m pytest tests/test_agent_portfolio.py -q`
Ожидается: FAIL — `ImportError: cannot import name 'account_budget'`.

- [ ] **Шаг 3: Написать расчёт бюджета кабинета**

В `sync/agent/portfolio.py`:

```python
# Шаг роста ОБЩЕЙ суммы кабинета за такт. Та же граница, что у безопасного
# бюджетного шага кампании (writer/learning.BUDGET_SAFE_DELTA): рост суммы
# раскладывается солвером по кампаниям, и держать его в тех же 20 % значит
# не перезапускать обучение ради самого роста.
ACCOUNT_GROWTH_STEP = 0.20

# Запас предельной окупаемости, при котором вообще имеет смысл доливать
# сверху. Ровно на цели растить нечего: предельный рубль там уже равен
# порогу, и прибавка сдвинет кабинет за него.
GROWTH_LAMBDA_MARGIN = 1.2


def account_budget(current_cost: float, lam: float, target_romi: float,
                   room_rub: float, monthly_cap: Optional[float]) -> Dict[str, Any]:
    """Бюджет кабинета на такт: держим или растим, и чем ограничены.

    Рост применяется только при заданном потолке месяца (панель настроек).
    Без потолка предложение считается и печатается, но сумма не меняется:
    решение «тратить больше» принимает человек, а агент приносит ему цифру.
    """
    wanted = current_cost * (1.0 + ACCOUNT_GROWTH_STEP)
    if lam < target_romi * GROWTH_LAMBDA_MARGIN:
        return {"budget": current_cost, "growth_rub": 0.0,
                "proposed_growth_rub": 0.0, "capped_by": "lambda"}
    if room_rub <= 0:
        return {"budget": current_cost, "growth_rub": 0.0,
                "proposed_growth_rub": 0.0, "capped_by": "room"}

    growth = min(wanted - current_cost, room_rub)
    capped_by = "step" if growth >= room_rub - 1e-6 and room_rub >= wanted - current_cost else "room"
    if growth >= wanted - current_cost - 1e-6:
        capped_by = "step"

    if monthly_cap is None:
        return {"budget": current_cost, "growth_rub": 0.0,
                "proposed_growth_rub": round(growth, 2), "capped_by": capped_by}

    budget = min(current_cost + growth, float(monthly_cap))
    if budget < current_cost + growth - 1e-6:
        capped_by = "monthly_cap"
    return {"budget": round(budget, 2),
            "growth_rub": round(budget - current_cost, 2),
            "proposed_growth_rub": round(growth, 2),
            "capped_by": capped_by}
```

В `portfolio_targets` заменить `budget = sum(c["cost"] for c in campaigns)` на вызов
`account_budget(...)` и передавать в `solve_threshold` полученный `budget`. Параметры
`target_romi`, `room_rub_by_login`, `monthly_cap` приходят аргументами функции —
модуль не читает ни конфиг, ни БД.

Инвариант суммы при этом сохраняется в новом виде: Σ целевых = бюджету кабинета,
где бюджет теперь может быть больше факта — и разница названа явно (`growth_rub`).

- [ ] **Шаг 4: Завести ключ в панели настроек**

В `sync/agent/config.py` добавить ключ `monthly_budget_cap_rub` (по умолчанию `None`,
не входит в `LOCKED_KEYS` — Павел меняет его сам) с комментарием: «потолок месячного
освоения на кабинет; пусто — агент общий бюджет не растит, только предлагает».

- [ ] **Шаг 5: Вывести в отчёт Э0**

```python
        "budget_growth": {
            login: {"budget_28d": acc["budget_28d"],
                    "growth_rub": acc.get("growth_rub", 0.0),
                    "proposed_growth_rub": acc.get("proposed_growth_rub", 0.0),
                    "capped_by": acc.get("capped_by"),
                    "lambda": acc["lambda"]}
            for login, acc in budget_threshold["accounts"].items()
        },
```

- [ ] **Шаг 6: Прогнать весь набор**

Запуск: `python -m pytest tests/ -q`
Ожидается: PASS.

- [ ] **Шаг 7: Коммит**

```bash
git add sync/agent/portfolio.py sync/agent/config.py sync/agent_e0.py \
        tests/test_agent_portfolio.py tests/test_agent_config.py
git commit -m "feat(agent): бюджет кабинета растёт шагом 20%, когда предельная окупаемость с запасом"
```

---

## Задача 12: ожидание и факт лидов доезжают до журнала

Петля обучения следующей задачи меряет смещение прогноза — насколько модель
систематически завышает или занижает эффект своих рычагов. Мерить нечем: **ни
ожидания, ни факта в журнале нет.**

- `expected_leads_delta` считается в солвере (`sync/agent/portfolio.py:221`) и умирает
  там же: в `edu_agent_actions` ни одна колонка и ни один ключ `payload` его не несёт
  (проверено grep по `sync/agent_e1.py`, `sync/agent_e1_watchdog.py`, `sync/agent/writer/`).
- Наблюдаемой дельты нет тем более: сторож считает уровень (`observed_metrics` — расход,
  лиды, CPA за окно), а сравнить его не с чем, потому что **объём базы нигде не
  записан**. `load_baseline_cpa` отдаёт только цену; комментарий в `action_experiment`
  это фиксирует прямым текстом: «объём базы в красной линии не записан, поэтому
  rel_error — нижняя граница».

Без этой задачи `forecast_bias` из задачи 13 вернёт пустой словарь на живой базе, а
тесты при этом будут зелёными — они кормят функцию словарями. Поэтому задача идёт
раньше петли.

**Файлы:**
- Изменить: `sync/agent/db.py` (новая `load_baseline_volume`)
- Изменить: `sync/agent_e1.py::build_red_line` (объём базы в красную линию)
- Изменить: `sync/agent/writer/rollback.py::red_line_for` (плоский ключ)
- Изменить: `sync/agent/writer/budget.py` (ожидание в payload действия)
- Изменить: `sync/agent/writer/db.py` (колонка `observed_leads_delta` + запись при закрытии)
- Изменить: `sync/agent_e1_watchdog.py` (считать факт при закрытии наблюдения)
- Изменить: `tests/test_agent_e1.py`, `tests/test_agent_e1_watchdog.py`, `tests/test_agent_writer_budget.py`

**Интерфейсы:**
- Отдаёт: `load_baseline_volume(date_from, date_to) -> Dict[str, Dict[str, float]]` —
  по `campaign_id`: `{"leads", "days", "leads_per_day"}`.
- Отдаёт: плоский ключ `baseline_leads_per_day` в `red_line`.
- Отдаёт: `payload["expected_leads_delta"]` у бюджетных действий.
- Отдаёт: колонка `observed_leads_delta` в `edu_agent_actions`, заполняемая при закрытии.
- Потребляет: задача 13 (`forecast_bias`).

- [ ] **Шаг 1: Написать падающий тест на объём базы**

```python
# tests/test_agent_e1.py
def test_red_line_carries_baseline_volume():
    """Красная линия несёт не только цену базы, но и её темп.

    Цена без объёма не даёт сравнить ожидание с фактом: сторож знает лиды за
    окно наблюдения, а сколько их было до изменения — неизвестно. Темп, а не
    сумма: окна базы и наблюдения разной длины, суммы несопоставимы.
    """
    action = {"action_kind": "budget.set", "object_id": "111", "object_level": "campaign"}
    red = build_red_line(action, {"111": 1000.0}, None, ("2026-07-01", "2026-07-28"),
                         None, {"111": {"leads": 56.0, "days": 28, "leads_per_day": 2.0}})
    assert red["baseline_leads_per_day"] == 2.0
```

- [ ] **Шаг 2: Прогнать — упадёт**

Запуск: `python -m pytest tests/test_agent_e1.py::test_red_line_carries_baseline_volume -q`
Ожидается: FAIL — `build_red_line() takes 5 positional arguments but 6 were given`.

- [ ] **Шаг 3: Объём базы: запрос, красная линия, плоский ключ**

`sync/agent/db.py` — рядом с `load_baseline_cpa`, отдельной функцией, чтобы не менять
сигнатуру существующей (у неё четыре потребителя, и все ждут `Dict[str, float]`):

```python
def load_baseline_volume(date_from: str, date_to: str) -> Dict[str, Dict[str, float]]:
    """Объём базы кампании за окно: сколько эффективных лидов в день.

    Зеркало load_baseline_cpa со сменой вопроса: там «сколько стоил лид», здесь
    «сколько их было». Темп (лиды в день), а не сумма за окно, — окно базы и
    окно наблюдения разной длины, и суммы сравнивались бы через разные
    знаменатели.
    """
    rows = _fetch_dicts(
        """
        SELECT campaign_id,
               SUM(eff_leads)            AS leads,
               COUNT(DISTINCT fact_date) AS days
        FROM edu_agent_facts
        WHERE fact_date BETWEEN %s AND %s
        GROUP BY campaign_id
        HAVING COUNT(DISTINCT fact_date) > 0
        """,
        (date_from, date_to),
    )
    return {
        str(r["campaign_id"]): {
            "leads": float(r["leads"] or 0.0),
            "days": int(r["days"] or 0),
            "leads_per_day": round(float(r["leads"] or 0.0) / int(r["days"]), 4),
        }
        for r in rows
    }
```

`sync/agent_e1.py::build_red_line` — новый необязательный параметр
`baseline_volume: Optional[Dict[str, Dict[str, float]]] = None`, и перед возвратом:

```python
    volume = (baseline_volume or {}).get(str(action["object_id"])) or {}
    if volume.get("leads_per_day"):
        # Темп базы едет в линию ради сверки ожидания с фактом (learning_loop).
        # Красная линия его не читает: откатывают по цене, а не по объёму.
        baseline["leads_per_day"] = volume["leads_per_day"]
```

`sync/agent/writer/rollback.py::red_line_for` — плоским ключом, как `baseline_cpo`:

```python
    if baseline.get("leads_per_day"):
        window["baseline_leads_per_day"] = float(baseline["leads_per_day"])
```

Вызов в `sync/agent_e1.py` (рядом с `load_baseline_cpa`/`load_baseline_cpo`) —
`agent_db.load_baseline_volume(...)` на том же окне, результат передать в
`build_red_line` шестым аргументом.

- [ ] **Шаг 4: Прогнать тест**

Запуск: `python -m pytest tests/test_agent_e1.py -q`
Ожидается: PASS.

- [ ] **Шаг 5: Ожидание в payload действия**

Тест:

```python
# tests/test_agent_writer_budget.py
def test_budget_action_carries_expectation():
    """Ожидаемая дельта лидов едет в payload действия.

    Солвер её считает, а журнал терял: сравнить прогноз с исходом было
    невозможно, и калибровка модели держалась на вере в модель.
    """
    actions, _ = budget_actions(desired={"111": _move(ratio=1.2, leads_delta=6.0)}, ...)
    assert actions[0]["payload"]["expected_leads_delta"] == 6.0
```

В `sync/agent/writer/budget.py` в оба места сборки действия (недельный лимит и
дневной бюджет) добавить в `payload`:

```python
                    # Ожидание солвера едет вместе с действием: через 7–14 дней
                    # сторож положит рядом факт, и разница этих двух чисел —
                    # единственный способ узнать, что модель врёт систематически.
                    "expected_leads_delta": round(
                        float(move.get("expected_leads_delta") or 0.0), 2),
```

- [ ] **Шаг 6: Факт при закрытии наблюдения**

Тест:

```python
# tests/test_agent_e1_watchdog.py
def test_observed_leads_delta_is_measured_against_base_rate():
    """Наблюдаемая дельта = (темп наблюдения − темп базы) × длина окна.

    Не разность сумм: окно базы 28 дней, окно наблюдения 7–14, и голая
    разность сумм показала бы обвал там, где темп вырос.
    """
    action = {"red_line": {"baseline_leads_per_day": 2.0}}
    observed = {"leads": 21, "days": 7}          # темп 3.0 против базовых 2.0
    assert observed_leads_delta(observed, action) == 7.0
```

В `sync/agent_e1_watchdog.py`:

```python
def observed_leads_delta(observed: Dict[str, Any],
                         action: Dict[str, Any]) -> Optional[float]:
    """Сколько лидов действие принесло сверх базового темпа за окно наблюдения.

    None, когда темпа базы в красной линии нет (действие спланировано до
    задачи 12) или окно пустое: отсутствие числа честнее нуля, который
    learning_loop прочитал бы как «прогноз сбылся ровно наполовину».
    """
    base_rate = float((action.get("red_line") or {}).get("baseline_leads_per_day") or 0.0)
    days = int(observed.get("days") or 0)
    if base_rate <= 0 or days <= 0:
        return None
    return round(float(observed.get("leads") or 0) - base_rate * days, 2)
```

`observed_metrics` уже считает `days` — отдать его в возвращаемый словарь, если
его там ещё нет.

- [ ] **Шаг 7: Колонка и запись при закрытии**

`sync/agent/writer/db.py` — ALTER в `WRITER_DDL` (отдельным оператором, не правкой
CREATE TABLE: таблица в базе уже есть, и `CREATE TABLE IF NOT EXISTS` её не тронет):

```python
    # Наблюдаемая дельта лидов против базового темпа — вторая половина пары
    # «ожидание / факт». Ожидание лежит в payload действия, факт появляется
    # только при закрытии наблюдения, поэтому колонка, а не payload: payload —
    # то, что собирались сделать, и дописывать в него постфактум значит
    # смешивать намерение с исходом.
    """
    ALTER TABLE edu_agent_actions
      ADD COLUMN IF NOT EXISTS observed_leads_delta DOUBLE PRECISION
    """,
```

`MARK_OBSERVATION_CLOSED_SQL` — третье присваивание и параметр:

```python
    UPDATE edu_agent_actions
       SET observation_closed_at  = now(),
           observation_verdict    = %(verdict)s,
           observed_leads_delta   = %(leads_delta)s
```

`mark_observation_closed(action_id, verdict, leads_delta=None)` — новый
необязательный параметр, чтобы существующие вызовы не сломались; в стороже
передать `observed_leads_delta(observed, action)`.

- [ ] **Шаг 8: Прогнать весь набор**

Запуск: `python -m pytest tests/test_agent_e1.py tests/test_agent_e1_watchdog.py tests/test_agent_writer_budget.py -q`
Ожидается: PASS.

- [ ] **Шаг 9: Коммит**

```bash
git add sync/agent/db.py sync/agent_e1.py sync/agent/writer/rollback.py \
        sync/agent/writer/budget.py sync/agent/writer/db.py sync/agent_e1_watchdog.py \
        tests/test_agent_e1.py tests/test_agent_e1_watchdog.py tests/test_agent_writer_budget.py
git commit -m "feat(agent): ожидание и факт лидов в журнале — вход калибровки прогноза"
```

---

## Задача 13: замкнуть петлю обучения на собственных действиях

Сейчас агент учится **косвенно**: применённое изменение меняет расход, детектор скачков
(`mining.detect_change_points`) видит это в фактах как квазиэксперимент, DiD даёт
эластичность, она входит в кривую насыщения следующего такта. Работает, но с тремя
дырами:

1. **У собственных действий нет контроля.** Свои исходы в историю уже попадают:
   `agent_e1_watchdog.action_experiment` пишет закрытое действие в
   `edu_agent_experiments` с `mechanism="before_after"` и классом `B` — отличить их
   от чужих скачков можно. Дыра в другом: `before_after` не вычитает сезон, поэтому
   класс `A` этим строкам не положен, и комментарий в самом коде это фиксирует
   («A появится, когда исход будет меряться против заповедника за то же окно»).
   Заповедник (holdout) в портфеле есть — значит контрольная группа за то же окно
   доступна, и класс `A` берётся ею, а не пометкой «это сделал я».
2. **Вердикты никуда не идут.** `closing_verdict` (по заявкам) и `money_verdict`
   (по деньгам, 35 дней) пишутся в журнал и читаются только кулдауном вредных
   сегментов. Доля попаданий по рычагам не считается — значит нет track record,
   на котором строится лестница автономии (Ф9).
3. **Смещение прогноза не измеряется.** Каждое действие несёт ожидание
   (`expected_leads_delta`), сторож знает факт — но систематическая разница
   («модель завышает эффект бюджетных сдвигов в полтора раза») не выводится и не
   вычитается из следующих оценок.

4. **Мера успеха однобока.** `closing_verdict` считает один эффект — относительное
   изменение CPA против базы (`agent_e1_watchdog.py:521`). Для сокращения это почти
   гарантированный успех: срезав объём, кампания почти всегда дешевеет. Для доливки —
   почти гарантированный провал: за объём и платят более высокой ценой конверсии.
   Значит `hit_rate`, посчитанный по этой мере, систематически хвалит резаков и ругает
   ростовиков, а лестница автономии (Ф9), построенная на нём, выдаст свободу именно
   сокращениям. Это ровно тот исход, ради предотвращения которого написаны задачи 9–11.

Эта задача закрывает все четыре и служит фундаментом Ф9.

**Как чинится четвёртая.** Красную линию и откат не трогаем: защита денег обязана
оставаться консервативной, и «CPA вырос» — правильный повод притормозить. Меняется
мера ОБУЧЕНИЯ: у действия появляется экономический исход, который судит рост по его
собственному обещанию.

```python
GROWTH_KINDS = frozenset({"budget.set", "budget.set_daily", "tcpa.set"})


def economic_outcome(action, observed, lam):
    """Исход действия в терминах его же намерения.

    Сокращение обещало цену — с него и спрашивается цена (closing_verdict).
    Доливка обещала ОБЪЁМ при цене не выше предельно допустимой: лиды выросли,
    а CPA остался ниже порога кабинета (lambda) — обещание выполнено, даже
    если цена поднялась. Судить доливку по «подешевело ли» значит требовать
    от неё того, ради чего её не делали, и получить механизм, который умеет
    только резать.
    """
    up = float(action.get("payload", {}).get("expected_leads_delta") or 0.0) > 0
    if not up or str(action.get("action_kind")) not in GROWTH_KINDS:
        return closing_verdict(observed, action)
    delta = observed_leads_delta(observed, action)
    if delta is None or lam <= 0:
        return "unknown"
    cpa = float(observed.get("cpa") or 0.0)
    if delta > 0 and 0 < cpa <= lam:
        return "improved"
    if delta <= 0:
        return "worsened"
    return "unchanged"
```

`track_record` считает долю попаданий **по этой мере**, и печатает `hit_rate`
раздельно для растящих и сокращающих действий: сравнивать их между собой нельзя даже
после поправки, потому что у них разные обещания.

**Файлы:**
- Создать: `sync/agent/learning_loop.py`
- Создать: `tests/test_agent_learning_loop.py`
- Изменить: `sync/agent_e1_watchdog.py::action_experiment` (класс A против заповедника)
- Изменить: `sync/agent/writer/db.py` (чтение закрытых действий с вердиктами)
- Изменить: `sync/agent_e0.py` (секция отчёта + подача калибровки в портфель)

**Интерфейсы:**
- Отдаёт:
  - `track_record(actions) -> Dict[str, Dict[str, Any]]` — по ключу
    `action_kind`: `{"closed", "improved", "worsened", "unchanged", "hit_rate",
    "money_confirmed", "money_contradicted"}`.
  - `forecast_bias(actions) -> Dict[str, Dict[str, float]]` — ключ **не
    `action_kind`, а `f"{action_kind}:{up|down}"`**: модель может завышать эффект
    доливки и занижать эффект срезания, и одно усреднённое число по `budget.set`
    спрятало бы обе ошибки друг за другом. Значение —
    `{"ratio", "n", "shrunk_ratio"}`, где `ratio` — медиана факт/ожидание,
    `shrunk_ratio` — то же с усадкой к 1.0 по объёму наблюдений.
  - `BIAS_PRIOR_N = 10` — сила приора усадки.
  - `MIN_EXPECTED_LEADS = 1.0` — действия с ожиданием меньше одного лида по модулю
    в калибровку не берутся: отношение к почти нулю даёт сотни, и даже медиана
    поплывёт, если таких действий наберётся половина.
- Потребляет: строки журнала `edu_agent_actions` со статусом `applied`. **Имена
  колонок сверены с DDL** (`sync/agent/writer/db.py`): вердикт по заявкам лежит в
  `observation_verdict` (`closing_verdict` — это функция сторожа, не колонка),
  вердикт по деньгам — `money_verdict`, ожидание — `payload->>'expected_leads_delta'`,
  факт — колонка `observed_leads_delta`. Последние два появляются задачей 12; до неё
  `forecast_bias` вернёт пустой словарь, и это ожидаемо, а не поломка.

- [ ] **Шаг 1: Написать падающий тест**

```python
# tests/test_agent_learning_loop.py
# -*- coding: utf-8 -*-
"""Петля обучения: агент учится на СВОИХ закрытых действиях, а не только на фактах."""

from sync.agent.learning_loop import BIAS_PRIOR_N, forecast_bias, track_record


def _action(kind, verdict, money=None, expected=None, observed=None):
    return {"action_kind": kind, "closing_verdict": verdict, "money_verdict": money,
            "expected_leads_delta": expected, "observed_leads_delta": observed}


def test_track_record_counts_by_lever():
    actions = [_action("budget.set", "improved"), _action("budget.set", "worsened"),
               _action("bidmodifier.set", "improved")]
    out = track_record(actions)
    assert out["budget.set"]["closed"] == 2
    assert out["budget.set"]["improved"] == 1
    assert out["budget.set"]["hit_rate"] == 0.5
    assert out["bidmodifier.set"]["hit_rate"] == 1.0


def test_money_contradiction_counted_separately():
    # Заявка подешевела, оплата подорожала — успех по заявкам, промах по деньгам.
    actions = [_action("tcpa.set", "improved", money="worsened"),
               _action("tcpa.set", "improved", money="improved")]
    out = track_record(actions)
    assert out["tcpa.set"]["money_confirmed"] == 1
    assert out["tcpa.set"]["money_contradicted"] == 1


def test_unknown_verdicts_do_not_count_as_success():
    actions = [_action("budget.set", "unknown"), _action("budget.set", "improved")]
    out = track_record(actions)
    assert out["budget.set"]["closed"] == 1
    assert out["budget.set"]["hit_rate"] == 1.0


def test_forecast_bias_is_median_of_fact_over_expectation():
    actions = [_action("budget.set", "improved", expected=10.0, observed=5.0),
               _action("budget.set", "improved", expected=10.0, observed=6.0),
               _action("budget.set", "improved", expected=10.0, observed=4.0)]
    out = forecast_bias(actions)
    assert out["budget.set"]["ratio"] == 0.5
    assert out["budget.set"]["n"] == 3


def test_bias_is_shrunk_towards_one_on_thin_evidence():
    # Три наблюдения не повод верить, что модель завышает вдвое: усадка к 1.0
    # по объёму (тот же приём, что эмпирический Байес в history.combine).
    actions = [_action("budget.set", "improved", expected=10.0, observed=5.0)] * 3
    out = forecast_bias(actions)
    shrunk = out["budget.set"]["shrunk_ratio"]
    assert 0.5 < shrunk < 1.0
    assert round(shrunk, 4) == round((0.5 * 3 + 1.0 * BIAS_PRIOR_N) / (3 + BIAS_PRIOR_N), 4)


def test_zero_expectation_is_skipped_not_infinite():
    actions = [_action("budget.set", "improved", expected=0.0, observed=5.0)]
    assert forecast_bias(actions) == {}


def test_empty_journal_gives_empty_answer():
    assert track_record([]) == {}
    assert forecast_bias([]) == {}
```

- [ ] **Шаг 2: Прогнать и убедиться, что падает**

Запуск: `python -m pytest tests/test_agent_learning_loop.py -q`
Ожидается: FAIL — `ModuleNotFoundError: No module named 'sync.agent.learning_loop'`.

- [ ] **Шаг 3: Написать модуль**

```python
# sync/agent/learning_loop.py
# -*- coding: utf-8 -*-
"""
sync/agent/learning_loop.py — обучение на СВОИХ закрытых действиях.

Косвенная петля уже работает: применённое изменение меняет расход, детектор
скачков видит его в фактах, DiD даёт эластичность, она входит в кривые
следующего такта. Здесь замыкается прямая петля — по журналу действий:

  • track_record — доля попаданий по каждому рычагу: сколько закрытых
    действий улучшили метрику, сколько ухудшили, и подтвердили ли это деньги
    на втором чекпоинте (agent_e1_watchdog.money_checkpoint). Это же —
    фундамент лестницы автономии: класс действий с доказанной долей попаданий
    получает больше свободы, недоказанный остаётся в тени.

  • forecast_bias — систематическое смещение прогноза: медиана отношения
    «факт / ожидание». Медиана, а не среднее: одно действие с ожиданием
    близким к нулю даёт отношение в сотни и утаскивает среднее.
    Усадка к 1.0 по объёму наблюдений — тот же приём, что эмпирический Байес
    в history.combine: три наблюдения не повод верить, что модель завышает
    вдвое.

Модуль ничего не решает и никуда не пишет: считает два числа, потребители —
портфель (поправка ожиданий) и отчёт (track record).
"""

import statistics
from typing import Any, Dict, List

# Сила приора усадки смещения: при n наблюдениях вес факта n/(n+BIAS_PRIOR_N).
# Десять — примерно такт-два боевой работы: до этого поправку применять рано.
BIAS_PRIOR_N = 10

SUCCESS = "improved"
FAILURE = "worsened"
NEUTRAL = "unchanged"


def track_record(actions: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Доля попаданий по видам действий среди ЗАКРЫТЫХ наблюдений."""
    out: Dict[str, Dict[str, Any]] = {}
    for action in actions:
        verdict = str(action.get("closing_verdict") or "")
        if verdict not in (SUCCESS, FAILURE, NEUTRAL):
            continue  # 'unknown' и незакрытые — не свидетельство ни за, ни против
        kind = str(action.get("action_kind") or "")
        slot = out.setdefault(kind, {"closed": 0, "improved": 0, "worsened": 0,
                                     "unchanged": 0, "money_confirmed": 0,
                                     "money_contradicted": 0})
        slot["closed"] += 1
        slot[verdict] += 1
        money = str(action.get("money_verdict") or "")
        if verdict == SUCCESS and money == SUCCESS:
            slot["money_confirmed"] += 1
        elif verdict == SUCCESS and money == FAILURE:
            slot["money_contradicted"] += 1
    for slot in out.values():
        slot["hit_rate"] = round(slot["improved"] / slot["closed"], 4) if slot["closed"] else 0.0
    return out


def forecast_bias(actions: List[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    """Систематическое смещение прогноза по видам действий."""
    ratios: Dict[str, List[float]] = {}
    for action in actions:
        expected = action.get("expected_leads_delta")
        observed = action.get("observed_leads_delta")
        if not expected or observed is None:
            continue
        try:
            ratio = float(observed) / float(expected)
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        ratios.setdefault(str(action.get("action_kind") or ""), []).append(ratio)

    out: Dict[str, Dict[str, float]] = {}
    for kind, values in ratios.items():
        median = statistics.median(values)
        n = len(values)
        out[kind] = {
            "ratio": round(median, 4),
            "n": n,
            "shrunk_ratio": round((median * n + 1.0 * BIAS_PRIOR_N) / (n + BIAS_PRIOR_N), 4),
        }
    return out
```

- [ ] **Шаг 4: Прогнать тесты**

Запуск: `python -m pytest tests/test_agent_learning_loop.py -q`
Ожидается: PASS (7 тестов).

- [ ] **Шаг 5: Класс A — там, где есть контроль, а не там, где есть авторство**

Соблазн пометить свои действия классом `A` потому, что дата и намерение известны, —
ошибка, и код `action_experiment` прямо предупреждает о ней: `before_after` не
вычитает сезон, а знание даты сезон не отменяет. Августовский спад накроет и
изменение, и его базу; «чистым» наблюдение делает контроль за то же окно, а не
запись в журнале.

Контроль у нас есть — заповедник портфеля (`EXPLORATION`/holdout: кампании, к
которым в это окно сознательно не применялось ничего). Меняем
`sync/agent_e1_watchdog.py::action_experiment`: когда контроль за окно доступен и
набрал минимум наблюдений — считаем разность разностей и повышаем класс; когда
недоступен — оставляем `before_after` и класс `B` как сейчас.

```python
MIN_CONTROL_LEADS = 20


def _did_effect(observed, baseline_cpa, control):
    """Эффект действия против контроля за то же окно (разность разностей).

    Контроль — кампании заповедника: в окно наблюдения к ним не применялось
    ничего, значит их изменение CPA относительно своей базы и есть сезон плюс
    рынок. Вычитая его, получаем эффект самого действия. Без контроля вернуть
    None и остаться на before_after: завышенный класс надёжности дороже
    отсутствующего, потому что на классах строится автономия (Ф9).
    """
    if not control or control.get("leads", 0) < MIN_CONTROL_LEADS:
        return None
    base_c, obs_c = control.get("baseline_cpa", 0.0), control.get("cpa", 0.0)
    if baseline_cpa <= 0 or obs_c <= 0 or base_c <= 0:
        return None
    treated = (observed.get("cpa", 0.0) - baseline_cpa) / baseline_cpa
    control_shift = (obs_c - base_c) / base_c
    return round(treated - control_shift, 4)
```

В теле `action_experiment` подменяются три поля: `effect` на разность разностей,
`mechanism` на `"did_holdout"`, `reliability_class` на `"A"`. Остальные — как есть.

Тесты дописать в `tests/test_agent_e1_watchdog.py`:

```python
def test_class_a_only_with_control():
    action = _applied_action(baseline_cpa=1000.0)
    observed = {"cpa": 1200.0, "leads": 40}

    without = action_experiment(action, observed, _window(), "worsened", control=None)
    assert without["reliability_class"] == "B"
    assert without["mechanism"] == "before_after"

    # Контроль подорожал ровно так же — значит это сезон, а не действие.
    control = {"baseline_cpa": 1000.0, "cpa": 1200.0, "leads": 50}
    with_control = action_experiment(action, observed, _window(), "worsened",
                                     control=control)
    assert with_control["reliability_class"] == "A"
    assert with_control["mechanism"] == "did_holdout"
    assert with_control["params"]["effect"] == 0.0


def test_thin_control_does_not_upgrade_class():
    control = {"baseline_cpa": 1000.0, "cpa": 1200.0, "leads": 3}
    out = action_experiment(_applied_action(baseline_cpa=1000.0),
                            {"cpa": 1200.0, "leads": 40}, _window(), "worsened",
                            control=control)
    assert out["reliability_class"] == "B"
```

- [ ] **Шаг 6: Читать закрытые действия и выводить петлю в отчёт**

В `sync/agent/writer/db.py`:

```python
def closed_actions(days: int = 180) -> List[Dict[str, Any]]:
    """Применённые действия с вынесенным вердиктом — вход петли обучения."""
    return _fetch_dicts(
        """
        SELECT action_kind, object_id, applied_at::date AS applied_on,
               -- В журнале колонка называется observation_verdict; closing_verdict
               -- — функция сторожа, которая её и заполняет. Алиас оставлен, чтобы
               -- learning_loop читал одно имя независимо от происхождения строки.
               observation_verdict AS closing_verdict, money_verdict,
               (payload->>'expected_leads_delta')::float AS expected_leads_delta,
               observed_leads_delta
        FROM edu_agent_actions
        WHERE applied_at IS NOT NULL
          AND applied_at >= now() - make_interval(days => %s)
        """,
        (int(days),),
    )
```

В `sync/agent_e0.py`:

```python
    from sync.agent.learning_loop import forecast_bias, track_record

    closed = writer_db.closed_actions()
    loop = {"track_record": track_record(closed), "forecast_bias": forecast_bias(closed)}
```

и в отчёт — `"learning_loop": loop,`.

Поправка ожиданий применяется в портфеле: `expected_leads_delta` умножается на
`shrunk_ratio` своего вида действия, когда он есть. Пока наблюдений мало, усадка
держит множитель около единицы — то есть поправка включается сама собой по мере
накопления истории, без отдельного переключателя.

- [ ] **Шаг 7: Прогнать весь набор**

Запуск: `python -m pytest tests/ -q`
Ожидается: PASS.

- [ ] **Шаг 8: Коммит**

```bash
git add sync/agent/learning_loop.py sync/agent/mining.py sync/agent/writer/db.py \
        sync/agent_e0.py tests/test_agent_learning_loop.py tests/test_agent_mining.py
git commit -m "feat(agent): петля обучения на своих действиях — track record, смещение прогноза, класс A"
```

---

## Задача 14: рост объёма не покупает мусор

Задачи 8–11 учат агента доливать деньги. У доливки есть цена, которой нет у
сокращения: **новый трафик холоднее старого**. Кампания расширяет охват, CPA держится
или даже падает, вердикт по заявкам через 7–14 дней говорит «improved» — а оплаты
приходят хуже. Узнаём мы об этом только на денежном чекпоинте, через 35 дней, потратив
месяц бюджета на когорту, которая не платит.

Ранний прокси качества у нас уже лежит и никем не читается: `sum_p_pay` — сумма
ML-скоров вероятности оплаты по лидам кампании (`edu_agent_facts.sum_p_pay`,
`sync/agent/db.py:56`, заполняется `sync/agent/facts.py:89`). Скор доступен **на
следующий день** после лида, а не через месяц, — этим он и ценен. Сейчас он попадает
только в вывод `computed.py:244` и ни на что не влияет.

**Знаменатель — не `eff_leads`.** Скор есть не у каждого лида: `load_score_rows`
джойнит `edu_lead_scores` с `crm_lead_details`, а скоринг ведётся по поведению
визита и требует `client_id` — он проставлен не на всех лендах. Лид без скора входит
в `sum_p_pay` нулём, а в `eff_leads` — единицей, поэтому отношение
`sum_p_pay / eff_leads` меряет не качество когорты, а **долю лидов со скором**.
Кампания, у которой выросла доля мобильного трафика или приложения, немедленно
покажет «падение качества», не изменившись ни на грамм. Поэтому задача начинается с
третьего счётчика в фактах:

- новая колонка `edu_agent_facts.scored_leads` (ALTER, как `conversions`);
- инкремент в `sync/agent/facts.py` рядом с `sum_p_pay`: `if r["lead_id"] in
  p_pay_by_lead: slot["scored_leads"] += 1`;
- поле в INSERT/UPDATE `upsert_facts` (`sync/agent/db.py:758-780`);
- средний скор считается как `sum_p_pay / scored_leads`, а покрытие
  `scored_leads / eff_leads` печатается рядом: падение ПОКРЫТИЯ — отдельный сигнал
  (сломался ingest поведения), и путать его с падением качества нельзя.

Родственный механизм, который не надо переписывать: `sync/agent/segment_quality.py`
считает качество лида по СЕГМЕНТАМ внутри кампании через мост client_id × Метрика и
лестницу событий. Здесь другая ось — когорта одной кампании во времени, до и после
доливки; общего кода у них нет, но термин «качество лида» обязан значить одно и то
же, поэтому в докстринге модуля стоит ссылка на соседа.

**Файлы:**
- Создать: `sync/agent/quality.py`
- Создать: `tests/test_agent_quality.py`
- Изменить: `sync/agent/db.py` (ALTER `scored_leads`, поле в `upsert_facts`)
- Изменить: `sync/agent/facts.py` (счётчик лидов со скором)
- Изменить: `sync/agent/growth.py` (кандидат с падающим качеством не усиливается)
- Изменить: `sync/agent_e0.py` (секция `lead_quality` в отчёте)
- Изменить: `tests/test_agent_facts.py` (счётчик), `tests/test_agent_growth.py`

**Интерфейсы:**
- Отдаёт:
  - `lead_quality(rows, date_from, date_to) -> Dict[str, Dict[str, float]]` — по
    `campaign_id`: `{"avg_p_pay", "scored_leads", "leads", "coverage"}`.
  - `quality_drift(before, after) -> Dict[str, Dict[str, Any]]` — по `campaign_id`:
    `{"drop", "flagged", "reason"}`.
  - `QUALITY_DROP_LIMIT = 0.2` — относительное падение среднего скора, после
    которого доливка останавливается.
  - `MIN_QUALITY_LEADS = 20` — минимум лидов в каждом окне; на меньшем объёме
    средний скор шумит сильнее, чем падает.
- Потребляет: `edu_agent_facts` (`sum_p_pay`, `eff_leads`), кандидаты усиления
  (задача 10).

- [ ] **Шаг 1: Написать падающий тест**

```python
# tests/test_agent_quality.py
# -*- coding: utf-8 -*-
"""Качество когорты как ранний тормоз роста.

Доливка бюджета расширяет охват, и новый трафик холоднее старого. CPA этого
не показывает — заявка остаётся заявкой; деньги показывают, но через 35 дней.
Средний ML-скор оплаты меняется на следующий день после того, как когорта
испортилась, и это единственный сигнал в системе, успевающий остановить
доливку до того, как месяц бюджета уйдёт в неплатящих.
"""

from sync.agent.quality import (MIN_QUALITY_LEADS, QUALITY_DROP_LIMIT,
                                lead_quality, quality_drift)


def _facts(cid, day, leads, p_pay, scored=None):
    return {"campaign_id": cid, "fact_date": day, "eff_leads": leads,
            "sum_p_pay": p_pay, "scored_leads": leads if scored is None else scored}


def test_avg_score_is_per_scored_lead_not_per_day():
    rows = [_facts("111", "2026-08-01", 10, 3.0), _facts("111", "2026-08-02", 30, 3.0)]
    out = lead_quality(rows, "2026-08-01", "2026-08-02")
    assert out["111"]["scored_leads"] == 40
    assert out["111"]["avg_p_pay"] == 0.15     # 6.0 / 40, а не среднее из 0.3 и 0.1


def test_unscored_leads_do_not_dilute_quality():
    # 40 лидов, скор есть у 20. Отношение к eff_leads дало бы 0.15 вместо
    # честных 0.30 — и «падение качества» при любом росте доли лендов без
    # client_id. Покрытие при этом видно отдельным числом.
    rows = [_facts("111", "2026-08-01", 40, 6.0, scored=20)]
    out = lead_quality(rows, "2026-08-01", "2026-08-02")
    assert out["111"]["avg_p_pay"] == 0.30
    assert out["111"]["coverage"] == 0.5


def test_quality_drop_flags_campaign():
    before = {"111": {"avg_p_pay": 0.20, "scored_leads": 40}}
    after = {"111": {"avg_p_pay": 0.14, "scored_leads": 40}}      # −30 %
    out = quality_drift(before, after)
    assert out["111"]["flagged"] is True
    assert out["111"]["drop"] == 0.3


def test_small_drop_is_not_a_flag():
    before = {"111": {"avg_p_pay": 0.20, "scored_leads": 40}}
    after = {"111": {"avg_p_pay": 0.18, "scored_leads": 40}}      # −10 %, шум
    out = quality_drift(before, after)
    assert out["111"]["flagged"] is False


def test_thin_cohort_is_not_judged():
    # Пять лидов дадут разброс среднего скора больше любого порога.
    before = {"111": {"avg_p_pay": 0.20, "scored_leads": 5}}
    after = {"111": {"avg_p_pay": 0.10, "scored_leads": 5}}
    out = quality_drift(before, after)
    assert out["111"]["flagged"] is False
    assert out["111"]["reason"] == "мало наблюдений"
```

- [ ] **Шаг 2: Прогнать — упадёт**

Запуск: `python -m pytest tests/test_agent_quality.py -q`
Ожидается: FAIL — `ModuleNotFoundError: No module named 'sync.agent.quality'`.

- [ ] **Шаг 3: Реализация**

```python
# sync/agent/quality.py
# -*- coding: utf-8 -*-
"""Качество когорты лидов как ранний тормоз доливки бюджета.

Механизм роста без контроля качества оптимизирует то, что видит: заявки.
Заявка при расширении охвата дешевеет, оплата — дорожает, и разрыв виден
только на денежном чекпоинте (35 дней). Средний ML-скор оплаты по лидам
кампании доступен на следующий день и играет роль предохранителя: доливка
кампании, чья когорта портится, ставится на паузу до вердикта по деньгам.
"""

from typing import Any, Dict, Iterable

QUALITY_DROP_LIMIT = 0.2
MIN_QUALITY_LEADS = 20


def lead_quality(rows: Iterable[Dict[str, Any]], date_from: str,
                 date_to: str) -> Dict[str, Dict[str, float]]:
    """Средний скор оплаты на лид по кампаниям за окно.

    Взвешивание — по лидам, а не по дням: день с тремя лидами и день с
    тридцатью не равноправны, среднее из дневных средних дало бы вес
    случайности.
    """
    acc: Dict[str, Dict[str, float]] = {}
    for row in rows:
        day = str(row.get("fact_date") or "")
        if not (date_from <= day <= date_to):
            continue
        slot = acc.setdefault(str(row.get("campaign_id") or ""),
                              {"leads": 0.0, "scored_leads": 0.0, "sum_p_pay": 0.0})
        slot["leads"] += float(row.get("eff_leads") or 0.0)
        slot["scored_leads"] += float(row.get("scored_leads") or 0.0)
        slot["sum_p_pay"] += float(row.get("sum_p_pay") or 0.0)
    out: Dict[str, Dict[str, float]] = {}
    for cid, slot in acc.items():
        scored, leads = slot["scored_leads"], slot["leads"]
        out[cid] = {
            "leads": leads,
            "scored_leads": scored,
            # Делим на лиды СО СКОРОМ: у лида без client_id скора нет, и в
            # сумме он ноль. Знаменателем eff_leads эта функция мерила бы
            # долю скоренных лидов, а не качество когорты.
            "avg_p_pay": round(slot["sum_p_pay"] / scored, 4) if scored else 0.0,
            # Покрытие — отдельный сигнал: его падение означает поломку
            # ingest'а поведения, а не ухудшение трафика.
            "coverage": round(scored / leads, 4) if leads else 0.0,
        }
    return out


def quality_drift(before: Dict[str, Dict[str, float]],
                  after: Dict[str, Dict[str, float]]) -> Dict[str, Dict[str, Any]]:
    """Падение среднего скора между окнами: до доливки и после.

    Флаг ставится только при достаточном объёме в ОБОИХ окнах: на десятке
    лидов средний скор гуляет сильнее порога, и тормоз срабатывал бы на шуме,
    останавливая рост там, где его надо продолжать.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for cid, now in after.items():
        was = before.get(cid) or {}
        base = float(was.get("avg_p_pay") or 0.0)
        cur = float(now.get("avg_p_pay") or 0.0)
        thin = (float(was.get("scored_leads") or 0.0) < MIN_QUALITY_LEADS
                or float(now.get("scored_leads") or 0.0) < MIN_QUALITY_LEADS)
        drop = round((base - cur) / base, 4) if base > 0 else 0.0
        if thin:
            out[cid] = {"drop": drop, "flagged": False, "reason": "мало наблюдений"}
            continue
        flagged = drop >= QUALITY_DROP_LIMIT
        out[cid] = {"drop": drop, "flagged": flagged,
                    "reason": "качество когорты упало" if flagged else ""}
    return out
```

- [ ] **Шаг 4: Прогнать тесты**

Запуск: `python -m pytest tests/test_agent_quality.py -q`
Ожидается: PASS (4 теста).

- [ ] **Шаг 5: Кандидат с падающим качеством не усиливается**

В `sync/agent/growth.py` (задача 10) перед выдачей кандидатов усиления:

```python
    # Кампания, чья когорта портится, из списка усиления выбывает — но не из
    # кабинета: это пауза роста до вердикта по деньгам, а не сокращение.
    # Резать её сейчас значило бы судить о деньгах по прокси, а прокси для
    # того и ранний, что менее точен.
    flagged = {cid for cid, d in (quality_drift or {}).items() if d.get("flagged")}
    candidates = [c for c in candidates if c["campaign_id"] not in flagged]
    out["skipped_by_quality"] = sorted(flagged)
```

Тест дописать в `tests/test_agent_growth.py`:

```python
def test_growth_skips_campaign_with_falling_lead_quality():
    out = growth_candidates(_portfolio_with_room(), _headroom(), _demand(), [],
                            quality_drift={"111": {"flagged": True}})
    assert [c["campaign_id"] for c in out["candidates"]] == ["222"]
    assert out["skipped_by_quality"] == ["111"]
```

- [ ] **Шаг 6: Секция отчёта**

В `sync/agent_e0.py` — секция `lead_quality`: по каждой кампании с флагом строка
«скор упал с X до Y (−Z %), рост приостановлен до денежного чекпоинта».
Строк нет — секция всё равно печатается пустым списком, как и остальные:
отсутствие секции неотличимо от отсутствия падений, а различать их нужно.

- [ ] **Шаг 7: Коммит**

```bash
git add sync/agent/quality.py sync/agent/growth.py sync/agent_e0.py \
        tests/test_agent_quality.py tests/test_agent_growth.py
git commit -m "feat(agent): ранний тормоз роста по качеству когорты лидов"
```

---

## Задача 15: свести волну в документации и прогнать боевой такт

**Файлы:**
- Изменить: `docs/AGENT-ROADMAP-2026-08-25.md`
- Изменить: `docs/AGENT-DATA-SOURCES.md`

- [ ] **Шаг 1: Прогнать Э0 в бою**

Запуск: вкладка Actions → `agent-e0` → Run workflow (или дождаться крона 09:30 МСК).
Проверить в логе новые секции отчёта Э0: `traffic_headroom`, `blind_spend`,
`demand_regime`, `growth` — и в отчёте Э1 (`agent-e1`, репетиция 11:00 МСК):
`learning`, `balance`.

Плюс секции задач 11–14: `budget_growth`, `learning_loop`, `lead_quality` (Э0).

Ключевая проверка боевого прогона: `balance.shrinking` при непустом плане должен быть
`false` (кроме аварийных сокращений — они вне гейта), а `growth.candidates` — непустым. Сжимающий такт с пустым списком усиления
значит, что механизм роста не заработал, и это повод остановиться, а не применять.
`learning_loop.track_record` в первые недели будет пуст — закрытых вердиктов ещё нет;
это нормально и должно быть видно нулём, а не отсутствием секции. То же у
`forecast_bias`: пара «ожидание / факт» появляется только у действий, спланированных
ПОСЛЕ задачи 12, — по ранее применённым она пуста навсегда, и это не дефект.
Отдельно проверить, что у свежих действий `payload.expected_leads_delta` непустой:
пустой у всех означает, что задача 12 доехала только до тестов.

- [ ] **Шаг 2: Записать боевые числа**

В `docs/AGENT-DATA-SOURCES.md` дописать раздел с фактическими числами прогона:
сколько кампаний с недобором трафика и на какой расход, слепая доля, режимы спроса
по направлениям, сколько сбрасывающих действий пришлось на такт и сколько заперто
кулдауном.

- [ ] **Шаг 3: Отметить закрытые пункты спеки**

В `docs/AGENT-ROADMAP-2026-08-25.md` пометить Ф7.6, Ф7.7 (минимум — счётчик), Ф7.8, Ф6.5
как выполненные со ссылкой на этот план; уточнить, что Ф7.7 закрыт счётчиком, а не
самой слепой зоной — она остаётся открытой задачей. Отдельным абзацем внести принцип
«рост и эффективность»: сокращение с адресатом, такт не сжимает объём, каждый такт
предъявляет усиление — и пометить, что полный генератор идей (Ф8) обязан продолжать
именно эту линию, а не добавлять ещё один способ резать.

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
   Лестница строится прямо на `learning_loop.track_record` (задача 13): класс
   действий с доказанной долей попаданий получает автономию, недоказанный остаётся
   в тени. Рост общего бюджета уже введён задачей 11; в Ф9 к нему добавляется
   пейсинг внутри месяца (сейчас потолок — плоское число из панели настроек).

Открытые решения Павла (нужны к плану Ф9, не к этой волне): скоуп первого применения,
`target_romi` (1.0 или 2.0), поднимать ли недельный риск-бюджет (сейчас 50 000 ₽).
