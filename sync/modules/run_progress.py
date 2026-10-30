# -*- coding: utf-8 -*-
"""Ход прогона модулей Директа → public.sync_runs.progress (через sync/progress.py).

Тот же канал, что у синков: строку sync_runs юнита panda-sync@<slug>_modules вставляет
runlog.sh, ход пишет sync.progress (троттлинг 2 с, без PANDA_SYNC_UNIT — ничего). Общие поля
(stage, label, done, total, since, updated_at) Panda-BI читает как у любого синка
(lib/project/sync-requests.ts, parseRunProgress); свои поля модулей разбирает
lib/modules/run-progress.ts:

    kind          "modules"
    modules       ключи модулей этого прогона по порядку (кого ждать), ставится до сканов
    modules_done  сколько сканов уже закончено
    module        ключ текущего модуля; None — фаза вне скана (подготовка, применение, превью)
    module_label  «Площадки» / «Запросы» / «Аудит»
    module_index  номер текущего модуля (1…modules_total)
    modules_total сколько модулей в прогоне
    login         кабинет, который читается сейчас (None — модуль кончил обход)
    part, part_done, part_total — подшаг кабинета (у аудита: «объявления», 3 из 8)

done/total — кабинеты текущего модуля (или шаги фазы). Доля прогона:
(modules_done + (done + part_done/part_total) / total) / modules_total.
"""

from typing import Callable, List, Optional, Sequence

from sync import progress as _progress

KIND = "modules"
LABELS = {"placements": "Площадки", "queries": "Запросы", "audit": "Аудит"}
# Каждое поле модулей пишется в каждом ходе: колонка сливается через ||, и поле прошлого
# этапа без явного None осталось бы висеть (кабинет «Площадок» в фазе применения).
_FIELDS = ("module", "module_label", "module_index", "login", "part", "part_done",
           "part_total")


def label_of(key: str) -> str:
    return LABELS.get(key, key)


class RunProgress:
    def __init__(self, report: Callable[..., None] = _progress.report) -> None:
        self._report = report
        self._plan: List[str] = []
        self._done = 0

    def plan(self, keys: Sequence[str]) -> None:
        """Модули, которые этот прогон будет сканировать, — до первого скана."""
        self._plan = list(keys)
        self._done = 0

    def phase(self, stage: str, label: str, done: Optional[int] = None,
              total: Optional[int] = None) -> None:
        """Фаза вне скана модуля: prepare (кабинеты, инвентарь), apply, preview."""
        self._emit("modules." + stage, "Модули · " + label, done, total, {})

    def module(self, key: str, accounts: Sequence) -> Callable[[dict], None]:
        """Начало скана модуля; возвращает ctx.progress для module.run.

        Модули зовут ctx.progress({"step", "logins_done", "logins_total"}) после кабинета
        (placements, queries) или и до него с подшагом part (audit). Текущий кабинет — по
        порядку ctx.accounts: после i пройденных читается accounts[i]."""
        logins = [a.login for a in accounts]
        index = self._plan.index(key) + 1 if key in self._plan else None
        stage, label = "modules." + key, label_of(key) + " · кабинеты"

        def emit(done: int, total: int, part: Optional[dict] = None) -> None:
            fields = {"module": key, "module_label": label_of(key), "module_index": index,
                      "login": logins[done] if done < len(logins) else None}
            if part and done < total:
                fields.update(part)
            self._emit(stage, label, done, total, fields)

        def on_progress(p: dict) -> None:
            total = _int(p.get("logins_total"), len(logins))
            done = min(_int(p.get("logins_done"), 0), total)
            part = None
            if p.get("part"):
                part = {"part": str(p["part"]), "part_done": _int(p.get("part_done"), None),
                        "part_total": _int(p.get("part_total"), None)}
            emit(done, total, part)

        emit(0, len(logins))
        return on_progress

    def module_done(self) -> None:
        self._done += 1

    def _emit(self, stage: str, label: str, done: Optional[int], total: Optional[int],
              fields: dict) -> None:
        extra = {k: fields.get(k) for k in _FIELDS}
        extra.update(kind=KIND, modules=list(self._plan), modules_done=self._done,
                     modules_total=len(self._plan))
        self._report(stage, done, total, label, extra)


def _int(v, default):
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default
