# -*- coding: utf-8 -*-
"""
tests/test_agent_ideas_registry.py — реестр идей (sync/agent/ideas/registry.py).

Идея до этого модуля жила внутри такта и умирала вместе с ним: генератор
детерминирован, назавтра он находит ровно то же самое, и человек получает тот
же список второй раз, третий, десятый. Реестр даёт идее срок жизни, историю и
приоритет, а человеку — право сказать «нет» один раз навсегда.

Проверяется здесь ровно то, что ломается молча:

  * идентификатор идеи детерминирован — иначе каждый такт заводит новую строку
    на тот же объект и реестр вырождается в журнал повторов;
  * приоритет — ценность на рубль проверки, а не абсолютная ценность;
  * идея без машинно проверяемого критерия успеха не пишется вовсе;
  * статус не откатывается назад: генератор — не хозяин статуса;
  * отклонение человеком помнится ПО ОБЪЕКТУ, а не по строке.

БД подменяется двойником (три частные функции доступа), поэтому DATABASE_URL
тесты не требуют — конвенция tests/test_agent_config_cli.py.
"""

import pytest

from sync.agent.db import AGENT_DDL
from sync.agent.ideas import registry


def test_idea_id_is_deterministic():
    # Недетерминированный идентификатор — это новая строка реестра на каждый
    # такт: тот же объект, тот же повод, и десять строк за десять дней.
    s = {"campaign_id": "123", "query": "колледж заочно"}
    assert registry.idea_id("consolidate", s) == registry.idea_id("consolidate", s)


def test_idea_id_ignores_key_order_in_subject():
    # Словарь генератора собирается в произвольном порядке (разные ветки кода
    # кладут ключи по-разному). Зависимость идентификатора от порядка ключей
    # дала бы ту же болезнь, что и случайный id, но заметную не сразу.
    a = {"campaign_id": "123", "query": "колледж заочно"}
    b = {"query": "колледж заочно", "campaign_id": "123"}
    assert registry.idea_id("consolidate", a) == registry.idea_id("consolidate", b)


def test_idea_id_separates_sources():
    # Одна и та же связка, найденная разными генераторами, — разные идеи с
    # разными рычагами. Схлопни их в один id — и вторая идея молча затрёт
    # первую вместе с её статусом.
    s = {"campaign_id": "123"}
    assert registry.idea_id("proven", s) != registry.idea_id("consolidate", s)


def test_subject_key_is_the_same_across_sources():
    # Ключ объекта, наоборот, обязан НЕ зависеть от источника: на нём держится
    # отклонение человеком («этот объект не трогаем»), и генератор, сменивший
    # источник, не должен уметь обойти это «нет».
    s = {"campaign_id": "123"}
    assert registry.subject_key(s) == registry.subject_key({"campaign_id": "123"})
    assert registry.idea_id("proven", s) != registry.idea_id("abtest", s)


# ------------------------------------------------------------------ таблица

def test_ideas_table_is_in_agent_ddl():
    # Модуль без таблицы — код, который в бою падает на первом же прогоне.
    ddl = "\n".join(AGENT_DDL)
    assert "CREATE TABLE IF NOT EXISTS edu_agent_ideas" in ddl


def test_ideas_ddl_has_no_destructive_statements():
    # Схема агента накатывается на каждом прогоне (ensure_agent_tables): любой
    # разрушительный оператор здесь стирал бы историю идей раз в сутки.
    ddl = "\n".join(AGENT_DDL).upper()
    for forbidden in ("DROP TABLE", "TRUNCATE", "DELETE FROM"):
        assert forbidden not in ddl


def test_ideas_table_carries_subject_key_and_human_rejection():
    # Три колонки, без которых «отклонено человеком» негде хранить: ключ
    # объекта (по нему ищется отклонение), автор отказа (машинный признак
    # «это сказал человек») и его момент. Без subject_key поиск отклонения
    # шёл бы по idea_id — то есть по строке, а не по объекту.
    ddl = "\n".join(AGENT_DDL)
    body = ddl.split("CREATE TABLE IF NOT EXISTS edu_agent_ideas", 1)[1]
    for column in ("subject_key", "rejected_by", "rejected_at"):
        assert column in body, column


# ---------------------------------------------------------------- приоритет

def test_rank_puts_cheap_high_value_first():
    # Порядок по абсолютной ценности разорил бы очередь: дорогая проверка
    # съедает и риск-бюджет, и горизонт, за который можно было закрыть три
    # дешёвых. Сортировка — по ценности НА РУБЛЬ проверки.
    a = {"idea_id": "a", "expected_rub": 100_000.0, "test_cost_rub": 10_000.0}
    b = {"idea_id": "b", "expected_rub": 200_000.0, "test_cost_rub": 100_000.0}
    assert [i["idea_id"] for i in registry.rank([b, a])] == ["a", "b"]


def test_rank_puts_free_check_first():
    # Проверка, не стоящая ничего (идея опирается на уже собранные данные),
    # обязана идти впереди любой платной: деньги за неё не берутся вовсе.
    free = {"idea_id": "free", "expected_rub": 1_000.0, "test_cost_rub": 0.0}
    paid = {"idea_id": "paid", "expected_rub": 500_000.0, "test_cost_rub": 1_000.0}
    assert [i["idea_id"] for i in registry.rank([paid, free])] == ["free", "paid"]


def test_rank_puts_unpriced_ideas_after_priced_ones():
    # Идея без цены проверки — не бесплатная, а НЕПОСЧИТАННАЯ. Пустое
    # значение, прочитанное как ноль, вынесло бы её на первое место очереди и
    # выдавило посчитанные: незнание оказалось бы сильнейшим аргументом.
    priced = {"idea_id": "priced", "expected_rub": 10.0, "test_cost_rub": 100.0}
    unpriced = {"idea_id": "unpriced", "expected_rub": 1_000_000.0,
                "test_cost_rub": None}
    assert [i["idea_id"] for i in registry.rank([unpriced, priced])] == [
        "priced", "unpriced"]


def test_rank_is_stable_on_equal_value():
    # Генератор детерминирован, значит и очередь обязана быть детерминирована:
    # плавающий порядок равноценных идей — это разный экран предложений на
    # одних и тех же данных, и человек перестаёт верить списку.
    a = {"idea_id": "a", "expected_rub": 100.0, "test_cost_rub": 10.0}
    b = {"idea_id": "b", "expected_rub": 200.0, "test_cost_rub": 20.0}
    assert [i["idea_id"] for i in registry.rank([b, a])] == ["a", "b"]
    assert [i["idea_id"] for i in registry.rank([a, b])] == ["a", "b"]


def test_rank_does_not_mutate_the_list_it_was_given():
    # Отчёт и план записи читают один и тот же список идей. Сортировка на
    # месте переставила бы его под ногами второго читателя.
    a = {"idea_id": "a", "expected_rub": 1.0, "test_cost_rub": 1.0}
    b = {"idea_id": "b", "expected_rub": 100.0, "test_cost_rub": 1.0}
    given = [a, b]
    registry.rank(given)
    assert given == [a, b]
