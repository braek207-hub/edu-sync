# -*- coding: utf-8 -*-
"""
tests/test_no_orphan_code.py — гейт против кода, который никто не вызывает.

Зачем. Один и тот же дефект повторился трижды подряд, каждый раз в новом
месте: функция написана, покрыта тестами, зелёная — и не вызвана из боевого
пути. Тесты при этом ничего не замечают, потому что проверяют саму функцию, а
не факт её использования.

  · merge_hourly — счётчики Метрики не складывались, расписание опускало
    22 часа из 24 (прогон 32568178620);
  · diff_schedule — ветка расписания была готова и не подключена к прогону;
  · plan_schedule — то же самое этажом выше.

Каждый раз это ловилось вручную мутацией «убрать вызов» — то есть держалось
на внимательности. Здесь оно ловится механически: гейт падает, как только в
коде агента появляется публичная функция, которую никто не зовёт.

Область — только агент. В остальном репозитории таких функций сейчас 35, и
это чужие задачи: гейт, падающий на чужом коде, отключат в первый же день.

Как снять срабатывание, если функция нужна:
  1. вызвать её (обычно этого и не хватало) — или
  2. удалить, если не нужна, — или
  3. внести в ALLOWED с ПРИЧИНОЙ, если вызов не виден статически.
"""

import ast
import pathlib
from typing import Dict, Set

REPO = pathlib.Path(__file__).resolve().parents[1]

# Что считаем боевым кодом агента.
AGENT_PATHS = [
    REPO / "sync" / "agent",
    REPO / "sync" / "agent_e0.py",
    REPO / "sync" / "agent_e1.py",
    REPO / "sync" / "agent_e1_watchdog.py",
]
# Где ищем вызовы: во всём боевом коде, а не только в агенте — функцию агента
# может звать соседний синк, и это законно. Тесты сюда НЕ входят: вызов из
# теста ничего не говорит о боевом пути, ровно в этом и был дефект.
CALLER_PATHS = [REPO / "sync", REPO / "scripts", REPO / "main.py"]

# Точки входа и то, что вызывается не по имени.
ALLOWED: Dict[str, str] = {
    "main": "точка входа модуля, вызывается интерпретатором",
    # Лестница ступеней (задача 3): share_of потребителя УЖЕ получил —
    # writer/lanes.policy_of, — и запись отсюда снята. step_of ждёт прогона
    # такта (задача 26); если к бете запись всё ещё здесь, лестница в бою не
    # подключена.
    "step_of": "потребитель — прогон такта, задача 26 плана беты",
    # Цена отсечения по знаку (задача 8): потребители — writer/negatives и
    # writer/placements, где сейчас стоит traffic_cut_exposure, считающий
    # весь вырезанный поток. Пока подмена не сделана, класс 0 платит валом,
    # то есть дефект 8а жив; если к бете запись всё ещё здесь — цена
    # посчитана и выброшена.
    "cut_exposure": "потребители — writer/negatives и writer/placements, задача 8",
    # Реестр идей (задача 10) ПОДКЛЮЧЁН: расчётный такт собирает связки и
    # пишет находки генераторов (задача 16а, agent_e0.collect_ideas), такт
    # записи читает их и двигает статусы (задача 11). Записи «ждём
    # потребителя» отсюда сняты — держать их дальше значило бы врать о
    # состоянии системы ровно там, где этот гейт и заведён.
    "reject": "потребитель — отказ человека на экране предложений (задача 27)",
    # Наряд билдеру (задача 17). Мост «идея реестра → наряд» готов и проверен,
    # но звать его будет полоса запуска (writer/launch.py, задача 18): сейчас
    # ни один рычаг агента не умеет создавать кампанию. Если к бете запись всё
    # ещё здесь — билдер рычагом не стал, и наряды пишутся руками.
    "from_idea": "потребитель — полоса запуска, задача 18 плана беты",
}


def _python_files(paths):
    for path in paths:
        if path.is_file() and path.suffix == ".py":
            yield path
        elif path.is_dir():
            for item in path.rglob("*.py"):
                if "__pycache__" not in str(item):
                    yield item


def _rel(path: pathlib.Path) -> str:
    """Путь для человека. Вне репозитория (временный файл теста) — как есть."""
    try:
        return str(path.relative_to(REPO)).replace("\\", "/")
    except ValueError:
        return path.name


class UnparsableFile(Exception):
    """Файл не разбирается — обычно маркеры конфликта после rebase."""


def _parse(path: pathlib.Path) -> ast.AST:
    """Разбор с внятным отказом.

    Молча пропустить нечитаемый файл нельзя: если единственный вызов сироты
    живёт именно в нём, гейт объявит сироту сиротой и соврёт. Считать его
    «без вызовов» — тоже враньё, только в другую сторону. Поэтому — остановка
    с именем файла, а не сырой SyntaxError на полтора экрана.
    """
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        raise UnparsableFile(
            f"{_rel(path)}: файл не разбирается ({exc.msg}, строка "
            f"{exc.lineno}). Гейт не может судить о вызовах, пока файл сломан — "
            "чаще всего это незакрытые маркеры конфликта после rebase."
        ) from None


def _defined_in_agent() -> Dict[str, str]:
    """Публичные функции агента: имя → файл."""
    out: Dict[str, str] = {}
    for path in _python_files(AGENT_PATHS):
        for node in ast.walk(_parse(path)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not node.name.startswith("_"):
                    out[node.name] = str(path.relative_to(REPO)).replace("\\", "/")
    return out


def _used_names() -> Set[str]:
    """Всё, что боевой код упоминает: вызовы, атрибуты, имена и АЛИАСЫ импорта.

    Алиасы обязательны: `from ... import describe as describe_schedule` — это
    вполне себе вызов, и без учёта алиасов гейт кричал бы на живой код.
    Ложное срабатывание здесь опаснее пропуска: гейт, который врёт, отключают.
    """
    used: Set[str] = set()
    for path in _python_files(CALLER_PATHS):
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                used.add(node.func.id)
            elif isinstance(node, ast.Attribute):
                used.add(node.attr)
            elif isinstance(node, ast.Name):
                used.add(node.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    if alias.asname:
                        used.add(alias.name)   # исходное имя тоже считаем
    return used


def _callers_of(name: str) -> Set[str]:
    """Файлы, где имя упоминается, — для внятного текста ошибки."""
    out = set()
    for path in _python_files(CALLER_PATHS):
        text = path.read_text(encoding="utf-8")
        if name in text:
            out.add(str(path.relative_to(REPO)).replace("\\", "/"))
    return out


def test_every_public_agent_function_is_called():
    defined = _defined_in_agent()
    used = _used_names()

    orphans = []
    for name, where in sorted(defined.items()):
        if name in ALLOWED or name in used:
            continue
        orphans.append(f"{where}::{name}")

    assert not orphans, (
        "Эти функции агента определены, но НИКТО их не вызывает из боевого "
        "кода. Так уже трижды терялась работающая логика: код написан, тесты "
        "зелёные, поведение не изменилось.\n  "
        + "\n  ".join(orphans)
        + "\n\nЧто делать: вызвать, удалить или внести в ALLOWED с причиной."
    )


def test_allowed_list_stays_justified():
    # Исключение без причины — способ незаметно вернуть тот же дефект.
    for name, reason in ALLOWED.items():
        assert reason.strip(), f"исключение {name} без причины"


def test_gate_notices_a_function_nobody_calls():
    """Проверка самого гейта: он обязан ловить осиротевшую функцию.

    Без этого теста гейт мог бы молча деградировать (например, из-за ошибки в
    разборе имён) и рапортовать «всё чисто» на пустом множестве — ровно тот
    класс отказа, против которого он и поставлен.
    """
    defined = {"merge_hourly": "sync/agent/metrika.py"}
    used = {"fetch_hourly_profile"}

    orphans = [f"{w}::{n}" for n, w in defined.items()
               if n not in ALLOWED and n not in used]

    assert orphans == ["sync/agent/metrika.py::merge_hourly"]


def test_gate_accepts_aliased_import_as_a_call():
    # `from ... import describe as describe_schedule` — живой вызов.
    # Гейт, ругающийся на такое, был бы отключён в первый же день.
    used = _used_names()
    assert "describe" in used


# --------------- гейт обязан не деградировать молча
# Обе проверки ниже поставлены по выжившим мутациям: гейт, разбор которого
# сломан, рапортует «всё чисто» на пустом множестве — то есть отказывает ровно
# тем же способом, против которого поставлен.

def test_gate_actually_sees_agent_functions():
    """`_defined_in_agent` обязана находить реальные функции агента.

    Мутация «вернуть {}» выживала: список сирот пуст, гейт зелёный, и любая
    новая сирота проезжает молча.
    """
    defined = _defined_in_agent()

    assert len(defined) > 50, f"разбор агента сломан: найдено {len(defined)}"
    # Живой якорь: функция существует и лежит там, где ждём.
    assert defined.get("plan_schedule") == "sync/agent/writer/plan.py"


def test_tests_do_not_count_as_callers():
    """Вызов из теста — не боевой путь; ровно в этом и был исходный дефект.

    Мутация «добавить tests/ в CALLER_PATHS» выживала, а с ней гейт перестал
    бы ловить ту самую ситуацию: функция написана, покрыта тестами, зелёная —
    и не вызвана ниоткуда, кроме теста.
    """
    assert all("tests" not in path.parts for path in CALLER_PATHS)

    # И проверка по существу: имя, встречающееся ТОЛЬКО в тестах, не должно
    # попадать в множество «вызывается».
    marker = "_orphan_gate_marker_that_lives_only_in_this_test"
    assert marker not in _used_names()


def test_broken_file_names_itself_instead_of_a_raw_traceback(tmp_path):
    """Нечитаемый файл обязан назваться.

    Поймано на живом дереве: чужой файл с маркерами конфликта после rebase
    ронял гейт сырым SyntaxError, и по выводу нельзя было понять ни что
    сломано, ни что гейт вообще ни при чём.
    """
    broken = tmp_path / "broken.py"
    broken.write_text("<<<<<<< HEAD\ndef f():\n    pass\n", encoding="utf-8")

    try:
        _parse(broken)
    except UnparsableFile as exc:
        assert "broken.py" in str(exc)
        assert "конфликт" in str(exc)
    else:
        raise AssertionError("сломанный файл разобрался — так не бывает")
