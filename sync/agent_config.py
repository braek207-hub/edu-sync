# -*- coding: utf-8 -*-
"""
sync/agent_config.py — панель настроек агента со стороны человека.

Слой настроек (sync/agent/config.py) умел только ЧИТАТЬСЯ: прогон брал строки
из edu_agent_config и накладывал их на кодовые дефолты. Записать в таблицу
было нечем — ни CLI, ни экрана, — то есть панель существовала, а ручек у неё
не было: ни один параметр физически нельзя было задать иначе, чем руками в
SQL-консоли. Этот модуль — недостающие ручки.

Три решения, ради которых он выглядит именно так:

  * **Сначала валидируем всё, потом пишем.** `--set a=1 b=99` с битым вторым
    аргументом не имеет права оставить в базе первый: половина настройки — это
    состояние, которого человек не задавал и о котором не знает. Валидация
    идёт по `config._validate` (тем же правилам, что и на чтении), запись —
    одной транзакцией после того, как проверены ВСЕ пары.
  * **Отказ вместо умолчания.** Неизвестный ключ, значение вне диапазона,
    попытка тронуть порог защиты — ненулевой код возврата и ни одной записи.
    Молча проигнорированная настройка выглядит как применённая, и человек
    считает, что агент работает иначе, чем на самом деле.
  * **Проверка у получателя.** Записанное обязано читаться обратно ровно тем
    значением, которое задали. Читает не этот модуль, а
    `sync/agent/db.py::load_agent_config`, поэтому сериализация здесь
    подогнана под ЕГО разбор, а не наоборот, и накрыта round-trip тестом
    (tests/test_agent_config_cli.py). Пустое значение — законный ответ «не
    задано» для nullable-параметра; разбор на стороне чтения был под него
    починен там же, где живёт (раньше пустая строка приезжала обратно как
    `''` и роняла валидацию, то есть очистить потолок бюджета было нельзя).

След смены настройки пишется в чёрный ящик прогонов (`sync/agent/blackbox.py`,
таблица edu_agent_runs, стадия "config") — тем же способом, каким его пишут
все остальные такты агента. Отдельной таблицы «журнал настроек» не заводится:
у чёрного ящика уже есть версия кода, ссылка на прогон и запросы разбора, а
вторая витрина того же события — гарантированное расхождение. Кто и когда
менял, дополнительно видно в самой edu_agent_config (updated_at/updated_by).

Запуск:
    python -m sync.agent_config                       # показать активный конфиг
    python -m sync.agent_config --show
    python -m sync.agent_config --set explore_share=0.12 p_sign_bid=0.85
    python -m sync.agent_config --set monthly_budget_cap_rub=   # очистить
    python -m sync.agent_config --preset conservative
    python -m sync.agent_config --unset explore_share preset
ENV: DATABASE_URL, AGENT_CONFIG_ACTOR (кто правит; по умолчанию "cli")
"""

import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sync.agent import blackbox
from sync.agent import config as agent_config
from sync.agent.db import AGENT_CONFIG_DDL, load_agent_config
from sync.db import get_connection

# Пресет лежит в той же таблице отдельной строкой с этим ключом — так его
# читает load_agent_config. Имя зарезервировано: параметра с таким именем в
# SPEC нет и быть не может.
PRESET_KEY = "preset"

DEFAULT_ACTOR = "cli"

# Код возврата на отказ. Не 1: единица приходит от питона на любом падении, а
# «настройку не приняли» — штатный исход, который скрипт вокруг должен уметь
# отличать от «модуль сломался».
EXIT_REFUSED = 2

UPSERT_SQL = """
INSERT INTO edu_agent_config (key, value, preset, updated_at, updated_by)
VALUES (%(key)s, %(value)s, %(preset)s, now(), %(actor)s)
ON CONFLICT (key) DO UPDATE SET
    value      = EXCLUDED.value,
    preset     = EXCLUDED.preset,
    updated_at = now(),
    updated_by = EXCLUDED.updated_by
"""

DELETE_SQL = "DELETE FROM edu_agent_config WHERE key = ANY(%(keys)s)"

READ_SQL = """
SELECT key, value, preset, updated_at, updated_by
  FROM edu_agent_config
 ORDER BY key
"""


class UsageError(Exception):
    """Ошибка вызова: разобранная командная строка бессмысленна."""


class Refusal(Exception):
    """Настройка не принята. Ни одной записи не сделано."""


# --------------------------------------------------------------- разбор argv

def parse_args(argv: Sequence[str]) -> Dict[str, Any]:
    """argv → намерение. Одно действие за вызов.

    Смешивать `--set` с `--preset` в одном вызове намеренно нельзя: пресет
    задаёт сразу семь параметров, и «поставил пресет и тут же переопределил
    два его значения» — две разные правки, которые в журнале обязаны быть
    видны раздельно. Порядок применения в одной команде пришлось бы ещё и
    угадывать.
    """
    action: Optional[str] = None
    pairs: List[str] = []
    keys: List[str] = []
    preset: Optional[str] = None

    def _claim(name: str) -> None:
        nonlocal action
        if action is not None and action != name:
            raise UsageError(
                f"за один вызов — одно действие, а указаны --{action} и --{name}")
        action = name

    index = 0
    args = list(argv)
    while index < len(args):
        arg = args[index]
        index += 1
        if arg == "--show":
            _claim("show")
        elif arg == "--set" or arg.startswith("--set="):
            _claim("set")
            if arg.startswith("--set="):
                pairs.append(arg.split("=", 1)[1])
            # Значения идут хвостом до следующего флага: пар обычно несколько,
            # и повторять --set перед каждой — лишний способ ошибиться.
            while index < len(args) and not args[index].startswith("--"):
                pairs.append(args[index])
                index += 1
        elif arg == "--unset" or arg.startswith("--unset="):
            _claim("unset")
            if arg.startswith("--unset="):
                keys.append(arg.split("=", 1)[1])
            while index < len(args) and not args[index].startswith("--"):
                keys.append(args[index])
                index += 1
        elif arg == "--preset" or arg.startswith("--preset="):
            _claim("preset")
            if arg.startswith("--preset="):
                preset = arg.split("=", 1)[1]
            elif index < len(args) and not args[index].startswith("--"):
                preset = args[index]
                index += 1
            else:
                raise UsageError("--preset требует имя пресета")
        else:
            raise UsageError(f"неизвестный аргумент: {arg}")

    if action == "set" and not pairs:
        raise UsageError("--set требует хотя бы одну пару KEY=VALUE")
    if action == "unset" and not keys:
        raise UsageError("--unset требует хотя бы один ключ")

    return {"action": action or "show", "pairs": pairs, "keys": keys,
            "preset": preset}


# ------------------------------------------------------- валидация и запись

def _refuse_locked(key: str) -> None:
    if key in agent_config.LOCKED_KEYS:
        raise Refusal(
            f"{key}: защиту нельзя ослабить настройками — это порог контура "
            f"безопасности, он меняется правкой кода (осознанно и с тестом), "
            f"а не панелью")


def to_text(value: Any) -> str:
    """Значение → строка колонки value.

    Пусто пишется пустой строкой, а не отсутствием строки: NULL колонка не
    принимает (value TEXT NOT NULL), а удалять строку нельзя — «потолок явно
    снят» и «потолок никогда не задавали» это разные факты, и первый должен
    быть виден в таблице вместе с датой и автором.
    """
    if value is None:
        return ""
    if isinstance(value, bool):                     # на будущее: bool — не число
        return "true" if value else "false"
    return str(value)


def validate_pairs(pairs: Sequence[str]) -> List[Tuple[str, Any]]:
    """Разбирает и проверяет ВСЕ пары KEY=VALUE до единой записи в базу.

    Возвращает пары (ключ, типизированное значение) — типизированное затем,
    что оно же идёт в отчёт: человек должен увидеть, каким значение стало
    после разбора, а не то, что он набрал.
    """
    out: List[Tuple[str, Any]] = []
    for item in pairs:
        if "=" not in item:
            raise Refusal(f"нужна форма KEY=VALUE, а получено: {item!r}")
        key, raw = item.split("=", 1)
        key = key.strip()
        raw = raw.strip()
        if key == PRESET_KEY:
            raise Refusal("пресет ставится флагом --preset, а не --set preset=…")
        _refuse_locked(key)
        # Пусто — это «не задано»: единственный способ вернуть nullable-параметр
        # в пустое состояние. Для остальных ключей _validate это отвергнет.
        value: Any = None if raw == "" else raw
        try:
            out.append((key, agent_config._validate(key, value)))
        except ValueError as exc:
            raise Refusal(str(exc)) from None
    return out


def validate_preset(name: Optional[str]) -> str:
    if not name or name not in agent_config.PRESETS:
        raise Refusal(
            f"неизвестный пресет: {name!r}; есть "
            f"{', '.join(sorted(agent_config.PRESETS))}")
    return name


def validate_keys(keys: Sequence[str]) -> List[str]:
    """Ключи для --unset. Снять можно только то, что вообще бывает задано."""
    out: List[str] = []
    for key in keys:
        key = key.strip()
        if key == PRESET_KEY:
            out.append(key)
            continue
        _refuse_locked(key)
        if key not in agent_config.SPEC:
            raise Refusal(f"неизвестный параметр: {key}")
        out.append(key)
    return out


class DbStore:
    """Доступ к edu_agent_config. Отделён от логики, чтобы тесты шли без БД.

    Таблицу создаёт сам, тем же DDL, что и чтение: модуль запускают руками и
    из workflow, и «сначала сходи прогоном, чтобы появилась таблица» — способ
    получить непонятную ошибку на ровном месте.
    """

    def ensure(self) -> None:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(AGENT_CONFIG_DDL)
            conn.commit()

    def rows(self) -> List[Dict[str, Any]]:
        import psycopg2.extras

        self.ensure()
        with get_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(READ_SQL)
                return [dict(row) for row in cur.fetchall()]

    def apply(self, upserts: Dict[str, str], deletes: Sequence[str],
              actor: str) -> Dict[str, int]:
        """Правки одной транзакцией: либо применились все, либо ни одна."""
        self.ensure()
        preset_name = upserts.get(PRESET_KEY)
        with get_connection() as conn:
            with conn.cursor() as cur:
                for key, value in upserts.items():
                    cur.execute(UPSERT_SQL, {
                        "key": key, "value": value, "actor": actor,
                        # Колонку preset читатель игнорирует (правда — строка с
                        # ключом preset), но в строке самого пресета она делает
                        # таблицу читаемой глазами без джойна.
                        "preset": preset_name if key == PRESET_KEY else None,
                    })
                if deletes:
                    cur.execute(DELETE_SQL, {"keys": list(deletes)})
                deleted = cur.rowcount if deletes else 0
            conn.commit()
        return {"written": len(upserts), "deleted": max(0, deleted)}


# ------------------------------------------------------------------- печать

def _format_value(value: Any) -> str:
    return "—" if value is None else str(value)


def render_show(stored: Dict[str, Any], rows: List[Dict[str, Any]]) -> str:
    """Активный конфиг с источником каждого значения + сырьё таблицы.

    Сырьё печатается отдельно намеренно: «в таблице лежит explore_share=0.12»
    и «агент работает с explore_share=0.12» — разные утверждения, и расходятся
    они ровно тогда, когда это важнее всего (правку записали не тем ключом,
    строка осталась от прошлого пресета).
    """
    lines: List[str] = []
    preset = stored.get("preset")
    lines.append(f"Пресет: {preset or '— (кодовые дефолты)'}")
    lines.append("")
    lines.append("Активный конфиг (дефолты → пресет → переопределения):")
    for row in agent_config.describe(preset, stored.get("overrides")):
        lines.append(f"  {row['key']:<24} {_format_value(row['value']):<12} "
                     f"{row['source']:<9} {row['about']}")
    lines.append("")
    if rows:
        lines.append("В таблице edu_agent_config:")
        for row in rows:
            value = row.get("value")
            shown = "— (пусто)" if str(value or "") == "" else str(value)
            lines.append(f"  {str(row['key']):<24} {shown:<12} "
                         f"{row.get('updated_at')} {row.get('updated_by') or ''}")
    else:
        lines.append("В таблице edu_agent_config пусто — агент на кодовых дефолтах.")
    return "\n".join(lines)


# --------------------------------------------------------------- точка входа

def main(argv: Optional[Sequence[str]] = None,
         store: Optional[DbStore] = None) -> int:
    args_source = sys.argv[1:] if argv is None else list(argv)
    store = store or DbStore()

    try:
        intent = parse_args(args_source)
    except UsageError as exc:
        print(f"ОТКАЗ: {exc}", file=sys.stderr)
        print(__doc__.split("Запуск:", 1)[-1].strip(), file=sys.stderr)
        return EXIT_REFUSED

    action = intent["action"]

    if action == "show":
        rows = store.rows()
        print(render_show(load_agent_config(), rows))
        return 0

    # Всё, что ниже, пишет. Сначала — валидация целиком: частичная запись
    # оставляет настройку в состоянии, которого человек не задавал.
    try:
        if action == "set":
            typed = validate_pairs(intent["pairs"])
            upserts = {key: to_text(value) for key, value in typed}
            deletes: List[str] = []
            change = {"set": {key: value for key, value in typed}}
        elif action == "preset":
            name = validate_preset(intent["preset"])
            upserts = {PRESET_KEY: name}
            deletes = []
            change = {"preset": name}
        else:                                            # unset
            keys = validate_keys(intent["keys"])
            upserts = {}
            deletes = keys
            change = {"unset": keys}
    except Refusal as exc:
        print(f"ОТКАЗ: {exc}", file=sys.stderr)
        print("Ничего не записано.", file=sys.stderr)
        return EXIT_REFUSED

    actor = os.environ.get("AGENT_CONFIG_ACTOR") or DEFAULT_ACTOR
    result = store.apply(upserts, deletes, actor)

    # Читаем обратно ТЕМ ЖЕ разборщиком, которым читает прогон: подтверждение
    # у получателя, а не «запрос вернул OK». Расхождение здесь означало бы, что
    # агент увидит не то, что человек задал, — и это надо видеть сразу.
    stored = load_agent_config()
    active = agent_config.describe(stored.get("preset"), stored.get("overrides"))

    trail = blackbox.save_run(
        blackbox.new_run_id(), stage="config", mode=blackbox.MODE_COMPUTE,
        report={"verdict": "CONFIG_CHANGED", "actor": actor, "change": change,
                "written": result.get("written"), "deleted": result.get("deleted"),
                "active": active})

    print(f"Применено: записей {result.get('written')}, "
          f"удалено {result.get('deleted')} (автор: {actor})")
    print(render_show(stored, store.rows()))
    if not trail.get("saved"):
        # Не роняем: настройка уже применена, и падение на записи следа
        # оставило бы человека в уверенности, что правка не прошла.
        print(f"ВНИМАНИЕ: след смены не записан в чёрный ящик: {trail.get('error')}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
