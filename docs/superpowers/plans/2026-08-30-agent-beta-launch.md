# Запуск беты агента-директолога — план доделок

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Закрыть блокеры аудита 29.08.2026 и выпустить первый боевой прогон `--prod --apply` 30.08, а за первую неделю беты — сделать выгоду агента измеримой в рублях.

**Architecture:** Правки точечные, в существующих модулях edu-sync: конфиг панели (тень денежных полос), прокидка `target_romi` в tCPA, свежесть расчёта, сторож откатов, fail-closed панели, tCPA как адресат роста в балансе, уведомления в Telegram. Новых карманов денег и новых рычагов — ноль. Замер выгоды — поверх уже существующего `tact_effect` (DiD против заповедника) переводом в рубли.

**Tech Stack:** Python 3.11, psycopg2, pytest, GitHub Actions, Telegram Bot API (urllib, без новых зависимостей).

**Spec:** аудит 29.08 — память `edu-agent-beta-readiness-audit-2026-08-29`; `docs/AGENT-BETA-PROTOCOL.md`; `docs/AGENT-BETA-MASTER-PLAN.md` (инварианты, Часть 6).

## Global Constraints

- `LOCKED_KEYS` в `sync/agent/config.py` не расширять и не ослаблять.
- Удаление объектов Директа запрещено; заповедник `edu_agent_holdout` неприкосновенен.
- Каждая правка — со своим тестом: красный прогон показан, зелёный показан.
- Коммит только своих файлов явными путями (`git add <файл>`), `git pull --rebase` перед push.
- Даты UTC в хранении, Europe/Moscow в показе; расход Директа уже с НДС.
- Секреты — только в секретах Actions (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` добавить туда же).
- После задач 1–8 — **заморозка кода агента на время беты**: правки только по находкам беты, каждая через отдельный PR с тестом.

---

## Часть A. День 0 (30.08 до первого прогона) — блокеры

### Task 1: Тень для денежных полос + ступени по решению Павла (конфиг, без кода)

**Files:**
- Modify (через workflow): таблица `edu_agent_config` — ключи `shadow_lanes`, `lane_steps`
- Workflow: `.github/workflows/agent-config.yml`

**Interfaces:**
- Consumes: `sync/agent_config.py --set KEY=VALUE`, валидаторы `config._lane_steps` / `KIND_LANE_LIST`
- Produces: `steps_by_lane` в отчёте Э1: `launch/allocation/suspend/exploration/proposal` → `{"step":0,"source":"shadow"}`, `tuning` → `{"step":3,"source":"config"}`

Решение Павла 29.08: «процент надо увеличивать». Ступень 3 = 6 % недельного расхода кабинета на полосу (`autonomy.STEPS`, верхняя; решение Павла 29.08 «бюджет побольше»). Первые три дня — только обратимые рычаги (tuning, hygiene); денежные полосы в тени, их намерения читаем на экране «Полосы и идеи».

- [ ] **Step 1: Выставить тень и ступени**

Actions → `agent-config` → `action=set`, `args`:
```
shadow_lanes=allocation,suspend,exploration,proposal,launch lane_steps=tuning:3,hygiene:1
```
(Формат `lane_steps` — как парсит `config._lane_steps`; если разбор отвергнет строку, ран красный с текстом — исправить формат по сообщению, а не гадать.)

- [ ] **Step 2: Проверить чтение**

Actions → `agent-config` → `action=show`. Ожидание: `shadow_lanes` = 5 полос, `lane_steps` = `{tuning: 3, hygiene: 1}`, `preset=balanced`, `target_romi=2.0`, `autonomy=full`.

- [ ] **Step 3: Прогнать probe**

Actions → `probe-beta-readiness`. Ожидание: `ready: true`; в `warnings` — «полоса tuning на ступени 3 по решению человека»; блокеров ноль. Если остались блокеры «ступень выдана полом» — конфиг не доехал, вернуться к шагу 1.

### Task 2: `target_romi` панели доезжает до целевого CPA

**Files:**
- Modify: `sync/agent_e0.py:1622-1624`
- Test: `tests/test_agent_e0.py`

**Interfaces:**
- Consumes: `active_config["target_romi"]` (уже в области видимости `main()`, определён на строке 917), `tcpa.tcpa_targets(campaigns, ..., target_romi=...)`
- Produces: `tcpa_section["target_romi"] == active_config["target_romi"]`

- [ ] **Step 1: Failing test** — в `tests/test_agent_e0.py`, рядом с `test_monthly_cap_from_the_panel_reaches_the_solver`:

```python
def test_target_romi_from_the_panel_reaches_tcpa(monkeypatch, capsys):
    # Протокол беты обещает: target_romi «прямо влияет на допустимый CPA».
    # До 29.08 tcpa_targets звался без него и считал на 1.0 (безубыточность).
    _patch_e0_run(monkeypatch)
    monkeypatch.setattr(
        agent_e0.agent_db, "load_agent_config",
        lambda *a, **k: {"preset": None, "overrides": {"target_romi": 2.0}})
    captured = []
    real = agent_e0.tcpa_targets

    def spy(*args, **kwargs):
        captured.append(kwargs)
        return real(*args, **kwargs)
    monkeypatch.setattr(agent_e0, "tcpa_targets", spy)

    assert agent_e0.main() == 0
    capsys.readouterr()
    assert captured and captured[0]["target_romi"] == 2.0
```

- [ ] **Step 2: Run** `python -m pytest tests/test_agent_e0.py -k target_romi_from_the_panel_reaches_tcpa -q` → FAIL (`KeyError: 'target_romi'`).

- [ ] **Step 3: Implementation** — `sync/agent_e0.py:1622`:

```python
    tcpa_section = tcpa_targets(build_tcpa_inputs(
        facts, saturation["campaigns"], campaign_settings,
        ladder_section["window_from"], ladder_section["window_to"]),
        target_romi=active_config["target_romi"])
```

- [ ] **Step 4: Run** тот же тест → PASS; `python -m pytest tests/test_agent_e0.py tests/test_agent_tcpa.py -q` → зелёный.

- [ ] **Step 5: Commit**
```bash
git add sync/agent_e0.py tests/test_agent_e0.py
git commit -m "fix(agent): target_romi панели доезжает до целевого CPA, а не только до портфеля"
```

### Task 3: Расчёт старше двух дней не применяется

**Files:**
- Modify: `sync/agent_e1.py:430`
- Test: `tests/test_agent_e1.py` (существующие тесты на строках ~531, ~1461–1483 читают константу — они останутся зелёными)

Крон GitHub опаздывает на 6–12 ч, Э0 27.08 не отработал вовсе; при семи днях Э1 применил бы недельный план молча.

- [ ] **Step 1: Failing test** — в `tests/test_agent_e1.py`:

```python
def test_computed_age_limit_is_two_days():
    # Замер 27–28.08: крон опаздывает на 6–12 ч, Э0 пропускается целиком.
    # Расчёт трёхдневной давности — уже не сегодняшний кабинет.
    assert agent_e1.MAX_COMPUTED_AGE_DAYS == 2
```

- [ ] **Step 2: Run** → FAIL (7 != 2).
- [ ] **Step 3:** `MAX_COMPUTED_AGE_DAYS = 2` с комментарием-источником (`# 27–28.08.2026: задержки крона до 12 ч, пропуск Э0 27.08 — недельный расчёт применять нельзя`).
- [ ] **Step 4: Run** `python -m pytest tests/test_agent_e1.py -q` → зелёный.
- [ ] **Step 5: Commit** `git add sync/agent_e1.py tests/test_agent_e1.py && git commit -m "fix(agent): расчёт старше 2 дней не применяется"`.

### Task 4: Сторож не хоронит строку при временной ошибке кабинета

**Files:**
- Modify: `sync/agent_e1_watchdog.py:1122-1158` (`resolve_added_modifier_id`), `:1235-1250` (`rollback_one`)
- Test: `tests/test_agent_e1_watchdog.py`

**Interfaces:**
- Produces: `resolve_added_modifier_id` возвращает `("ok", id | None)` или `("unreachable", reason)`; `rollback_one` при `unreachable` → `_fail(..., permanent=False)` с причиной `READ_FAILED_REASON`.

- [ ] **Step 1: Failing test**

```python
def test_transient_read_error_does_not_bury_the_action(monkeypatch):
    # 5xx кабинета в момент отката — не «Id не существует». Строка обязана
    # остаться в наблюдении и попробовать откат на следующем прогоне.
    action = _stale_bidmodifier_add()  # фикстура: status='stale', payload без Id
    client = _client_write_ok()
    monkeypatch.setattr(watchdog, "read_actual_modifiers",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("HTTP 502")))
    marked = []
    db = _db_stub(mark_rollback_failed=lambda aid, reason, permanent: marked.append(permanent) or {})
    out = watchdog.rollback_one(client, db, action, holdout_ids=set())
    assert out["result"] == "rollback_failed"
    assert out["permanent"] is False
    assert marked == [False]
    assert "кабинет не ответил" in out["reason"]
```
(имена фикстур `_stale_bidmodifier_add`, `_client_write_ok`, `_db_stub` — взять из уже существующих в файле тестов сторожа; если названы иначе, использовать существующие с тем же смыслом.)

- [ ] **Step 2: Run** → FAIL (`permanent` True).

- [ ] **Step 3: Implementation**

В константах рядом с `NO_ID_REASON`:
```python
READ_FAILED_REASON = "кабинет не ответил при восстановлении Id корректировки — повтор на следующем прогоне"
```
`resolve_added_modifier_id` — вместо `return None` в `except` вернуть кортеж; сигнатура:
```python
def resolve_added_modifier_id(client, action) -> Tuple[str, Optional[Any]]:
    ...
    try:
        actual = read_actual_modifiers(client, str(action.get("object_id")))
    except Exception as exc:  # noqa: BLE001
        return ("unreachable", f"{type(exc).__name__}: {exc}"[:200])
    ...
    return ("ok", matched[0] if len(matched) == 1 else None)
```
`rollback_one`:
```python
        if request is None:
            state, recovered = resolve_added_modifier_id(client, action)
            if state == "unreachable":
                return _fail(db_module, action, f"{READ_FAILED_REASON} ({recovered})",
                             False, journal_ok)
            if recovered is not None:
                request = rollback_payload({...})
```
Все прочие вызовы `resolve_added_modifier_id` (grep) перевести на кортеж.

- [ ] **Step 4: Run** `python -m pytest tests/test_agent_e1_watchdog.py -q` → зелёный.
- [ ] **Step 5: Commit** `git add sync/agent_e1_watchdog.py tests/test_agent_e1_watchdog.py && git commit -m "fix(watchdog): ошибка чтения кабинета не хоронит откат навсегда"`.

### Task 5: Панель недоступна → боевая запись не идёт (fail-closed)

**Files:**
- Modify: `sync/agent_e1.py:2673-2684`
- Test: `tests/test_agent_config_wired.py` (рядом с проверкой `CONFIG_UNAVAILABLE`, строка ~364)

- [ ] **Step 1: Failing test**

```python
def test_config_unavailable_blocks_live_write(monkeypatch, capsys):
    # Выключатель autonomy живёт в панели. Панель не прочиталась —
    # значит слово человека неизвестно, и писать в кабинет нельзя.
    monkeypatch.setattr(agent_e1.agent_db, "load_agent_config",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    rc = agent_e1._run_all(clients=[], sandbox=False, dry_run=False, today="2026-08-30")
    out = capsys.readouterr().out
    assert rc == 1
    assert "CONFIG_UNAVAILABLE" in out
```
(Существующий тест на `CONFIG_UNAVAILABLE` проверяет репетицию — он остаётся: в `dry_run=True` прогон продолжается на дефолтах.)

- [ ] **Step 2: Run** → FAIL (rc 0 / прогон пошёл дальше).

- [ ] **Step 3: Implementation** — после `print(... "CONFIG_UNAVAILABLE" ...)`:
```python
        if not dry_run:
            # Боевая запись без прочитанного слова человека — fail-open на
            # выключателе. Репетиция на дефолтах допустима: она ничего не пишет.
            return 1
```

- [ ] **Step 4: Run** `python -m pytest tests/test_agent_config_wired.py tests/test_agent_e1.py -q` → зелёный.
- [ ] **Step 5: Commit** `git add sync/agent_e1.py tests/test_agent_config_wired.py && git commit -m "fix(agent): недоступная панель запрещает боевую запись"`.

### Task 6: Уведомления в Telegram о применениях, откатах, тревогах

**Files:**
- Create: `sync/agent/notify.py`
- Modify: `sync/agent_e1.py:2922` (после `blackbox.save_run`), `sync/agent_e1_watchdog.py:2100` (после `blackbox.save_run`), `.github/workflows/agent-e1.yml`, `.github/workflows/agent-e1-watchdog.yml` (env `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`)
- Test: `tests/test_agent_notify.py`

**Interfaces:**
- Produces: `notify.send(text: str) -> dict` — `{"sent": bool, "reason": str|None}`; никогда не бросает; без env-переменных возвращает `{"sent": False, "reason": "not_configured"}`.
- Produces: `notify.e1_summary(report: dict, dry_run: bool) -> str`, `notify.watchdog_summary(out: dict) -> str`.

- [ ] **Step 1: Failing tests**

```python
from sync.agent import notify

def test_not_configured_is_silent(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert notify.send("x") == {"sent": False, "reason": "not_configured"}

def test_transport_error_never_raises(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t"); monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setattr(notify, "_post", lambda *a, **k: (_ for _ in ()).throw(OSError("net")))
    out = notify.send("x")
    assert out["sent"] is False and "OSError" in out["reason"]

def test_e1_summary_names_applied_and_rejected():
    report = {"verdict": "GREEN", "accounts": [{"account": "acc", "planned": 240,
               "result": {"applied": 3, "failed": 0, "stale": 0},
               "rejects": {"lane_limit": 174, "budget": 6},
               "lanes": {"taken": {"tuning": 20, "hygiene": 10}}}]}
    text = notify.e1_summary(report, dry_run=False)
    assert "acc" in text and "применено 3" in text and "lane_limit 174" in text
    assert "БОЕВАЯ" in text
```

- [ ] **Step 2: Run** `python -m pytest tests/test_agent_notify.py -q` → FAIL (module not found).

- [ ] **Step 3: Implementation** `sync/agent/notify.py`:

```python
# -*- coding: utf-8 -*-
"""Уведомления человеку о боевых тактах. Молчание — тоже сигнал, поэтому
шлём и «применено 0». Транспорт — Bot API через urllib; отказ сети не
роняет прогон: результат возвращается полем и уходит в чёрный ящик."""
import json, os, urllib.request
from typing import Any, Dict

def _post(url: str, data: bytes) -> None:
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    urllib.request.urlopen(req, timeout=10).read()

def send(text: str) -> Dict[str, Any]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN"); chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return {"sent": False, "reason": "not_configured"}
    try:
        _post(f"https://api.telegram.org/bot{token}/sendMessage",
              json.dumps({"chat_id": chat, "text": text[:4000]},
                         ensure_ascii=False).encode("utf-8"))
        return {"sent": True, "reason": None}
    except Exception as exc:  # noqa: BLE001
        return {"sent": False, "reason": f"{type(exc).__name__}: {exc}"[:200]}

def e1_summary(report: Dict[str, Any], dry_run: bool) -> str:
    mode = "репетиция" if dry_run else "БОЕВАЯ ЗАПИСЬ"
    lines = [f"Агент Э1 · {mode} · {report.get('verdict')}"]
    for acc in report.get("accounts") or []:
        res = acc.get("result") or {}
        rej = acc.get("rejects") or {}
        taken = (acc.get("lanes") or {}).get("taken") or {}
        lines.append(
            f"{acc.get('account')}: план {acc.get('planned')}, применено {res.get('applied', 0)}, "
            f"сбой {res.get('failed', 0)}, stale {res.get('stale', 0)}; полосы "
            + ", ".join(f"{k} {v}" for k, v in taken.items()) + "; отказы "
            + ", ".join(f"{k} {v}" for k, v in sorted(rej.items(), key=lambda kv: -kv[1])[:4]))
    return "\n".join(lines)

def watchdog_summary(out: Dict[str, Any]) -> str:
    lines = [f"Сторож · {out.get('verdict')}"]
    if out.get("alarms"):
        lines += ["ТРЕВОГИ: " + "; ".join(out["alarms"])]
    lines.append(f"откатов {out.get('rolled_back', 0)}, пробоев {len(out.get('breached') or [])}, "
                 f"закрыто наблюдений {sum((out.get('closed_verdicts') or {}).values())}, "
                 f"неоткатываемых {out.get('needs_manual_rollback', 0)}")
    return "\n".join(lines)
```
Ключи `result.applied/failed/stale`, `rolled_back`, `breached`, `closed_verdicts`, `needs_manual_rollback` — сверить с фактическими именами в отчётах (`edu_agent_runs.report`, лог сторожа 27.08); подставить реальные.

В `agent_e1.py` после `saved = blackbox.save_run(...)`:
```python
    report["notify"] = notify.send(notify.e1_summary(report, dry_run))
```
В сторожe после `out["blackbox"] = ...`:
```python
    out["notify"] = notify.send(notify.watchdog_summary(out))
```
В оба workflow добавить в `env`: `TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}`, `TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}`. Секреты завести через `gh secret set` из файла (`< file`), не через пайп PowerShell (память `ps-pipe-bom-into-secrets`).

- [ ] **Step 4: Run** `python -m pytest tests/test_agent_notify.py tests/test_agent_e1.py tests/test_agent_e1_watchdog.py -q` → зелёный.
- [ ] **Step 5: Commit** `git add sync/agent/notify.py tests/test_agent_notify.py sync/agent_e1.py sync/agent_e1_watchdog.py .github/workflows/agent-e1.yml .github/workflows/agent-e1-watchdog.yml && git commit -m "feat(agent): уведомления в Telegram о тактах и откатах"`.
- [ ] **Step 6: Проверка у получателя** — репетиция Э1 (Actions, prod без apply) → сообщение пришло в чат, кириллица цела.

### Task 7: Живые SQL-тесты журнала и переоценка риска

**Files:** нет правок кода.

- [ ] **Step 1:** Actions → `agent-tests-db` (workflow_dispatch). Ожидание: зелёный. Красный → чинить до запуска, это журнал записи.
- [ ] **Step 2:** `scripts/reprice_actions.py` без `--apply` — прочитать план переоценки (5 строк от 22.08, 38 876 ₽ → цены дельта-модели).
- [ ] **Step 3:** С `--apply` в проде (`DATABASE_URL` из секрета; запуск руками Павла или через `agent-config`-подобный одноразовый workflow_dispatch). Проверка: `spent_risk` за текущую неделю в отчёте следующей репетиции Э1 заметно ниже 38 876 ₽.

### Task 8: Заморозка и «зелёная сборка» перед стартом

- [ ] **Step 1:** `python -m pytest tests -q` → 0 failed.
- [ ] **Step 2:** `git pull --rebase && git push`; убедиться, что последний коммит `main` — из этого плана.
- [ ] **Step 3:** Actions → `probe-beta-readiness` → `ready: true`.
- [ ] **Step 4:** Репетиция Э1 (prod, без apply) на свежем `main`: `DATA_GATE` нет, `lane_steps` в отчёте как в Task 1, Telegram пришёл.
- [ ] **Step 5:** Объявить заморозку: в `docs/AGENT-BETA-PROTOCOL.md` дописать дату старта и sha кода.

---

## Часть B. День 1 (30.08) — первый боевой прогон

- [ ] Actions → `agent-e1`: `prod=true`, `apply=true`, `max_campaigns=1`.
- [ ] Сразу после: `SELECT action_id, action_kind, status, response FROM edu_agent_actions WHERE created_at::date = current_date` — у `bidmodifier.add` есть `AddResults[0].Id`; статусов `stale` нет.
- [ ] Actions → `agent-drift` вручную: расхождений с кабинетом ноль.
- [ ] Глазами в кабинете Директа: изменённая кампания, значения совпадают с `payload`.
- [ ] Стоп-условие дня (протокол, этап 1): любое расхождение, `unknown_outcome`, любое незапланированное изменение → `agent-config set autonomy=off`, разбор.
- [ ] Дни 2–3: `max_campaigns` снять; полосы те же. Дни 4–7: Павел решает по экрану «Полосы и идеи», выпускать ли `allocation` из тени (`shadow_lanes` без неё, `lane_steps=allocation:1`) — только после Task 9.

---

## Часть C. Неделя 1 беты (параллельно наблюдению)

### Task 9: tCPA вверх — адресат роста в балансе такта

**Files:**
- Modify: `sync/agent/balance.py:286-335` (`balance_inputs`)
- Test: `tests/test_agent_balance.py`

Без этого allocation-полоса заперта по построению: «вверх» лимитом не работает у 53/62 кампаний, а единственный работающий up-рычаг (tCPA) в `added_rub` не входит → каждый такт «сжатие без адресата».

- [ ] **Step 1: Failing test**
```python
def test_tcpa_up_counts_as_growth_address():
    actions = [
        {"action_kind": "budget.set", "object_id": "1", "idempotency_key": "a",
         "payload": {"expected_leads_delta": -5}},
        {"action_kind": "tcpa.set", "object_id": "2", "idempotency_key": "b",
         "payload": {"TargetCpa": 1_200_000_000, "expected_leads_delta": 6,
                     "expected_rub_delta": 60_000.0},
         "previous_state": {"TargetCpa": 1_000_000_000}},
    ]
    moves = {"1": {"cost_28d": 100_000.0, "target_28d": 60_000.0}}
    inputs = balance.balance_inputs(actions, moves, {"2": 200_000.0}, {})
    assert [m["campaign_id"] for m in inputs["moves"]] == ["1", "2"]
    tcpa = inputs["moves"][1]
    assert tcpa["target_28d"] - tcpa["cost_28d"] == 60_000.0
    out = balance.tact_balance(**inputs)
    assert out["added_rub"] == 60_000.0 and out["freed_rub"] == 40_000.0
```

- [ ] **Step 2: Run** → FAIL (moves содержит только "1").

- [ ] **Step 3: Implementation** — в `balance_inputs` добавить ветку после `BUDGET_KINDS`:
```python
        elif kind == "tcpa.set":
            # Рост целью: расход следует за целью (expectation._tcpa). Прирост
            # расхода — обещание рычага, и оно же — адресат для освобождённых
            # денег. Снижение цели (rub_delta < 0) — сокращение, идёт в freed.
            rub = _num((action.get("payload") or {}).get("expected_rub_delta"))
            if rub == 0.0:
                continue
            cost = _num(cost_28d_by_campaign.get(cid))
            moves.append({**common, "cost_28d": cost, "target_28d": cost + rub,
                          "expected_leads_delta": _leads_delta(action)})
```
Единицы: `expected_rub_delta` пишется `expectation._tcpa` за `days` наблюдения, а `moves` — за 28 дн. Привести к 28 дням: `rub * 28.0 / max(1, int(payload.get("measure_days") or 28))` — проверить имя ключа периода в `expectation.py` (`measure_days`) и использовать его.

- [ ] **Step 4: Run** `python -m pytest tests/test_agent_balance.py tests/test_agent_e1.py -q` → зелёный.
- [ ] **Step 5: Commit** `git add sync/agent/balance.py tests/test_agent_balance.py && git commit -m "fix(agent): рост целевым CPA — адресат сокращений в балансе такта"`.

### Task 10: Ошибка ценности конверсии в p_sign целевого CPA

**Files:** `sync/agent/tcpa.py:360-380`; `tests/test_agent_tcpa.py`.

`rel = √(value_rel_error² + 1/conversions)` не знает пуассона по оплатам: 100 конверсий и 4 оплаты дают rel≈0.1 вместо ≥0.5 — единственный up-рычаг будет уверен там, где данных нет.

- [ ] **Step 1: Failing test** — `tcpa_target` с `payments=4`, `conversions=100`, без кривой → `rel_error >= 0.5`.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** В `build_tcpa_inputs` пробрасывать число оплат когорты (`payments_fact` из `edu_agent_facts`), в `tcpa_target`: `value_rel = sqrt(value_rel_error**2 + 1.0/max(1, payments))`.
- [ ] **Step 4:** зелёный. **Step 5:** commit.

### Task 11: Гигиена откатывается автоматически

**Files:** `sync/agent/writer/rollback.py:261-339` (ветка для `negative.add` / `placement.exclude` через уже существующий `negatives.REMOVE_KIND`), `tests/test_agent_writer_rollback.py`.

- [ ] Тест: пробой красной линии на `negative.add` → `rollback_payload` строит `keywords`/`negativekeywords` запрос на возврат прежнего списка из `previous_state`; `placement.exclude` — прежний список площадок.
- [ ] Реализация по образцу `launch.py:290` (`negative.remove_added`).
- [ ] Commit.

### Task 12: Пол ступени не выше заработанной

**Files:** `sync/agent/writer/lanes.py:351`, `tests/test_agent_writer_lanes.py` (переписать `test_demotion_never_pushes_into_shadow` под новую норму: провал 20 % попаданий → ступень 0).

- [ ] Правило: `if record and record["closed"] >= autonomy.STEPS[1].min_closed: step = earned` (лестница высказалась — она главнее пола); пол только пока `closed < 12`.
- [ ] Commit.

### Task 13: Выгода агента в рублях

**Files:**
- Create: `sync/agent/value.py`
- Modify: `sync/agent_e0.py` (секция отчёта `agent_value`), экран «Полосы и идеи» в EDU v2 (карточка «Выгода за период»)
- Test: `tests/test_agent_value.py`

**Метод.** Два слоя, оба уже посчитаны, не хватает перевода в рубли и суммы по периоду:

1. **Такт против заповедника** (`tact_effect.py`): `did` по цене эфф. лида, интервал из плацебо. Рубли: `saved_rub = −did × cost_treated_after` (сколько стоили бы те же лиды по цене «без агента»). Считается только при вердикте `improved`/`worsened`; `inconclusive` → 0 с пометкой «не измерено», не ноль выгоды.
2. **Действие против ожидания** (`learning_loop`, `observed_leads_delta` в журнале): `earned_rub = observed_leads_delta × value_per_eff_lead(направление)` по лестнице; отдельно «сэкономлено» по сокращениям класса 0 (`cut_cost_by_kind`, фактическое падение расхода на отсечённых фразах/площадках без падения лидов).

Итог периода: `{"saved_rub", "earned_rub", "unmeasured_share", "did_interval", "n_tacts"}` — и **честная граница**: при 1–3 % расхода в неделю интервал такта за месяц будет включать ноль; число печатается с интервалом, а не точкой.

- [ ] **Step 1: Failing test**
```python
def test_value_converts_did_into_rubles():
    tact = {"verdict": "improved", "did": -0.10, "cost_treated_after": 1_000_000.0}
    out = value.tact_value(tact)
    assert out["saved_rub"] == 100_000.0 and out["measured"] is True

def test_inconclusive_is_unmeasured_not_zero():
    out = value.tact_value({"verdict": "inconclusive", "did": -0.03, "cost_treated_after": 1e6})
    assert out["saved_rub"] == 0.0 and out["measured"] is False
```
- [ ] **Step 2:** FAIL. **Step 3:** `value.py` с `tact_value`, `period_value(tacts, actions)`. **Step 4:** зелёный. **Step 5:** commit; секция в Э0 и карточка на экране — отдельным коммитом с тестом рендера.

---

## Self-review

- Блокеры аудита: полосы (T1), target_romi (T2), свежесть (T3), сторож (T4), fail-open (T5), уведомления (T6), живые тесты + репрайс (T7) — все закрыты в Части A.
- HIGH недели: баланс/tCPA (T9), rel_error оплат (T10), откат гигиены (T11), пол ступени (T12), выгода в рублях (T13).
- Не вошло сознательно: наряд→`builder.run` (В9), один потолок на все кабинеты, суточный сторож молчания тактов (частично закрыт T6 — «применено 0» тоже приходит) — после недели беты.
