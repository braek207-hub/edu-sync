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
    этого класса. Единственное, что здесь повторяет запрос, — условие поиска
    отклонений (subject_key + непустой rejected_by); оно же проверяется
    отдельно по тексту SQL, чтобы копии не разъехались.
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
    monkeypatch.setattr(registry, "_write_rows", fake.write_rows)
    return fake
