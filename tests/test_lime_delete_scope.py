import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync import lime, lime_gcc, lime_kz_metrika

CANDIDATES = ["ru", "kz", "gcc", "kz_metrika", None]


def _regions_deleted_by(sql: str, candidates=CANDIDATES) -> set:
    """Какие регионы удалит предикат DELETE_SQL — проверяем СЕМАНТИКУ, не подстроку.

    Предикат (`region IS NULL OR region NOT IN (...)` у синка-витрины, `region = 'x'` у
    синка одного среза) вычисляется на списке кандидатов через sqlite: диалект тут не
    важен, важна булева логика. Проверка вида `assert "gcc" in sql` пропустила бы ровно
    тот баг, ради которого этот файл и существует, — отсутствующий регион в исключениях.
    """
    where = sql.split("WHERE", 1)[1]
    # Рамка по датам к владению срезом отношения не имеет — нейтрализуем её, не полагаясь
    # на порядок условий: у синка-витрины даты идут первыми, у остальных — после региона.
    where = re.sub(r"date\s*(>=|<=)\s*%s", "1=1", where).strip()

    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE TABLE lime_stats (region TEXT)")
        con.executemany("INSERT INTO lime_stats VALUES (?)", [(c,) for c in candidates])
        hit = con.execute(f"SELECT region FROM lime_stats WHERE {where}").fetchall()
    finally:
        con.close()
    return {r[0] for r in hit}


def test_showcase_sync_deletes_only_its_own_regions():
    """Синк витрины партнёра владеет ru/kz (и строками без региона), но обязан не трогать
    срезы, которые пишут отдельные синки."""
    assert _regions_deleted_by(lime.DELETE_SQL) == {"ru", "kz", None}


def test_kz_metrika_sync_deletes_only_its_own_region():
    assert _regions_deleted_by(lime_kz_metrika.DELETE_SQL) == {"kz_metrika"}


def test_gcc_sync_deletes_only_its_own_region():
    assert _regions_deleted_by(lime_gcc.DELETE_SQL) == {"gcc"}


def test_every_foreign_sync_is_registered_in_showcase_exclusions():
    """Структурная защита: регион каждого отдельного синка обязан быть в списке исключений
    синка-витрины, иначе тот сотрёт его при ежедневном прогоне (баг 2026-07-18)."""
    assert lime_kz_metrika.REGION in lime.FOREIGN_REGIONS
    assert "gcc" in lime.FOREIGN_REGIONS
