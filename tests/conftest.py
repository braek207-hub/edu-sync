# -*- coding: utf-8 -*-
"""Общие двойники тестов.

Здесь живёт то, что нужно БОЛЬШЕ чем одному файлу тестов. Двойник реестра
идей — как раз такой случай: его просят и тесты самого реестра, и тесты
каждого генератора идей (их по плану беты пять). Копия двойника в каждом
файле разъезжалась бы с реестром по одной, и разъезд первым делом съел бы
именно тесты генераторов — те, где двойник не главный герой, а фон.
"""

import pytest

from sync.agent.ideas import registry


class FakeIdeas:
    """edu_agent_ideas в памяти: те же три примитива доступа, без БД.

    Двойник намеренно ТУПОЙ — он только хранит строки. Правило слияния
    (статус не откатывается, закрытая идея не воскресает) живёт в Python
    реестра, а не в тексте SQL, поэтому тесты проверяют его, а не свойства
    этого класса. Запросы здесь повторены ровно двумя условиями — поиск
    отклонений (subject_key + непустой rejected_by) и выборка открытых строк
    (статус из OPEN_STATUSES, при нужде — кабинет); оба проверяются отдельно
    по тексту SQL, чтобы копии не разъехались.
    """

    def __init__(self):
        self.table = {}
        self.writes = 0

    def read_rows(self, idea_ids):
        return {i: dict(self.table[i]) for i in idea_ids if i in self.table}

    def read_rejections(self, subject_keys):
        wanted = set(subject_keys)
        out = {}
        for row in self.table.values():
            key = row.get("subject_key")
            if row.get("rejected_by") and key in wanted:
                out[key] = {"subject_key": key,
                            "rejected_by": row.get("rejected_by"),
                            "rejected_at": row.get("rejected_at"),
                            "dropped_reason": row.get("dropped_reason")}
        return out

    def read_by_order(self, order_id, account):
        """Условие SELECT_BY_ORDER_SQL: наряд внутри нагрузки действия."""
        for row in self.table.values():
            order = ((row.get("action") or {}).get("payload") or {}).get("order")
            if str((order or {}).get("order_id") or "") != str(order_id):
                continue
            if account is not None and str(row.get("account")) != str(account):
                continue
            return dict(row)
        return None

    def read_by_experiment(self, experiment_id):
        """Условие SELECT_BY_EXPERIMENT_SQL: идея, связанная с этой ставкой."""
        for row in self.table.values():
            if str(row.get("experiment_id") or "") == str(experiment_id):
                return dict(row)
        return None

    def read_settled(self, bet_statuses, days, account):
        """SELECT_SETTLED_SQL: исход ставки приходит из таблицы гипотез.

        В памяти второй таблицы нет, поэтому двойник держит исход в самой
        строке (bet_status); сам JOIN проверяется по тексту запроса.
        """
        wanted = set(bet_statuses)
        out = []
        for row in self.table.values():
            if str(row.get("bet_status") or "") not in wanted:
                continue
            if account is not None and str(row.get("account")) != str(account):
                continue
            out.append(dict(row))
        return out

    def read_open(self, account=None):
        """Условие SELECT_OPEN_SQL: открытые статусы, при нужде — один кабинет.

        Нужен проходу по живому реестру (registry.sweep_open): он читает
        именно открытые строки, и без двойника его правило проверялось бы
        только живой базой — то есть не проверялось бы вовсе.
        """
        out = []
        for row in self.table.values():
            if str(row.get("status")) not in registry.OPEN_STATUSES:
                continue
            if account is not None and str(row.get("account")) != str(account):
                continue
            out.append(dict(row))
        return out

    def write_rows(self, rows):
        self.writes += 1
        for row in rows:
            self.table[row["idea_id"]] = dict(row)
        return len(rows)


@pytest.fixture
def store(monkeypatch):
    fake = FakeIdeas()
    monkeypatch.setattr(registry, "_read_rows", fake.read_rows)
    monkeypatch.setattr(registry, "_read_rejections", fake.read_rejections)
    monkeypatch.setattr(registry, "_read_by_order", fake.read_by_order)
    monkeypatch.setattr(registry, "_read_by_experiment", fake.read_by_experiment)
    monkeypatch.setattr(registry, "_read_settled", fake.read_settled)
    monkeypatch.setattr(registry, "_read_open", fake.read_open)
    monkeypatch.setattr(registry, "_write_rows", fake.write_rows)
    return fake
