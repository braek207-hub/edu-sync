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
