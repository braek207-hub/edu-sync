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


# ------------------------------------------------- двойник таблицы и идеи

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


def _idea(campaign="123", source="consolidate", **over):
    """Полная идея генератора: всё обязательное на месте.

    Помощник намеренно отдаёт ВАЛИДНУЮ идею: тест на отказ ломает ровно одно
    поле, и тогда видно, что отказ пришёл именно из-за него, а не из-за
    случайного недостающего.
    """
    idea = {
        "source": source,
        "account": "edu-vuz",
        "subject": {"campaign_id": campaign},
        "tier": 1,
        "lane": "allocation",
        "expected_rub": 100_000.0,
        "test_cost_rub": 10_000.0,
        "horizon_days": 14,
        "success_rule": {"metric": "eff_cpl", "op": "<=", "value": 900.0},
    }
    idea.update(over)
    return idea


def _id(idea):
    return registry.idea_id(idea["source"], idea["subject"])


# ------------------------------------------- критерий успеха обязателен

def test_idea_without_success_rule_is_rejected(store):
    # Идея без проверяемого критерия неотличима от мнения: её нельзя ни
    # закрыть, ни засчитать, и она вечно висит в реестре, занимая внимание
    # человека. Значение по умолчанию тут было бы худшим решением — оно
    # придумало бы за генератор то, о чём он промолчал.
    with pytest.raises(ValueError, match="критерий успеха"):
        registry.upsert([_idea(success_rule={})])


def test_success_rule_without_a_threshold_is_rejected(store):
    # Непустой словарь ещё не критерий: «станет лучше» машина не проверит.
    # Критерий обязан называть метрику, знак сравнения и порог — иначе
    # закрывать идею придёт человек, а это и есть то, от чего реестр спасает.
    with pytest.raises(ValueError, match="критерий успеха"):
        registry.upsert([_idea(success_rule={"note": "станет лучше"})])


def test_success_rule_with_unknown_comparison_is_rejected(store):
    with pytest.raises(ValueError, match="критерий успеха"):
        registry.upsert([_idea(success_rule={"metric": "eff_cpl",
                                             "op": "около", "value": 900.0})])


def test_unknown_lane_and_tier_are_rejected(store):
    # Полоса и класс достоверности у идеи те же, что у действия
    # (writer/lanes.py, writer/tier.py). Незнакомое значение здесь означало
    # бы третью шкалу рядом с двумя существующими.
    with pytest.raises(ValueError, match="полоса"):
        registry.upsert([_idea(lane="что-нибудь")])
    with pytest.raises(ValueError, match="класс достоверности"):
        registry.upsert([_idea(tier=7)])


def test_broken_idea_leaves_the_whole_batch_unwritten(store):
    # Половина порции — состояние, которого не задавал никто: часть идей
    # такта в реестре, часть нет, и назавтра генератор досыпает остаток как
    # новые. Порция принимается целиком или не принимается вовсе.
    with pytest.raises(ValueError):
        registry.upsert([_idea(campaign="ok"), _idea(campaign="bad",
                                                     success_rule={})])
    assert store.table == {}


def test_explicit_idea_id_must_match_the_derived_one(store):
    # Идентификатор выведен из пары (источник, объект). Чужой id, принятый
    # молча, разорвал бы связь «та же находка — та же строка»: назавтра
    # генератор посчитает свой, выведенный, и заведёт вторую строку.
    with pytest.raises(ValueError, match="идентификатор"):
        registry.upsert([_idea(idea_id="самодельный")])


# ----------------------------------------------------------- запись идеи

def test_upsert_writes_the_idea_and_it_reads_back(store):
    # Проверка у получателя: читает не двойник, а сам реестр (registry.load),
    # и читает те же поля, которые в бою уедут в колонки.
    written = registry.upsert([_idea()])
    row = registry.load(_id(_idea()))

    assert [r["idea_id"] for r in written] == [_id(_idea())]
    assert row["status"] == "new"
    assert row["subject"] == {"campaign_id": "123"}
    assert row["subject_key"] == registry.subject_key({"campaign_id": "123"})
    assert row["lane"] == "allocation"
    assert row["horizon_days"] == 14


def test_upsert_of_the_same_idea_does_not_create_a_second_row(store):
    # Ровно то, ради чего реестр и заведён: детерминированный генератор
    # находит ту же связку каждым тактом.
    registry.upsert([_idea()])
    registry.upsert([_idea()])
    assert len(store.table) == 1


# --------------------------------------------------------- запрос и колонки

def test_upsert_sql_refreshes_every_column_of_the_row():
    # Правило слияния живёт в Python, а SQL пишет то, что ему дали, — значит
    # колонка, забытая в DO UPDATE, молча замораживала бы поле навсегда:
    # реестр показывал бы ценность идеи, посчитанную в день её появления.
    sql = " ".join(registry.UPSERT_SQL.split())
    for column in registry.COLUMNS:
        if column == "idea_id":
            continue
        assert f"{column} = EXCLUDED.{column}" in sql, column


def test_upsert_sql_never_rewrites_created_at():
    # Дата появления идеи — точка отсчёта её жизни. Перепиши её повторная
    # генерация, и «эта идея висит третий месяц» перестало бы существовать
    # как факт: каждая идея выглядела бы сегодняшней.
    sql = " ".join(registry.UPSERT_SQL.split())
    assert "created_at" not in sql
    assert "updated_at = now()" in sql


def test_rejections_are_looked_up_by_subject_not_by_idea():
    # Тот же запрет, что проверяется поведением ниже, — но здесь по тексту
    # запроса: двойник в тестах повторяет это условие, и разъехаться копии не
    # должны. Поиск по idea_id вернул бы отказ только той же строке.
    sql = " ".join(registry.SELECT_REJECTIONS_SQL.split())
    assert "subject_key = ANY(%s)" in sql
    assert "rejected_by IS NOT NULL" in sql
    assert "idea_id" not in sql


# ------------------------------------------------- статус не едет назад

def test_regenerating_a_running_idea_keeps_its_status(store):
    # Генератор детерминирован и находит ту же идею каждым тактом. Сбрось
    # повторная находка статус — идея, УЖЕ взятая в работу, каждое утро
    # возвращалась бы в очередь новой и заводилась бы вторым действием.
    registry.upsert([_idea()])
    registry.mark(_id(_idea()), registry.STATUS_RUNNING, action_id="act-1")

    registry.upsert([_idea()])

    assert registry.load(_id(_idea()))["status"] == registry.STATUS_RUNNING


def test_generator_cannot_declare_a_status_for_an_existing_idea(store):
    # Статус двигают применение и человек, а не автор идеи. Объявленный
    # генератором статус на существующей строке игнорируется целиком — иначе
    # у реестра оказалось бы два хозяина жизненного цикла.
    registry.upsert([_idea()])
    registry.mark(_id(_idea()), registry.STATUS_RUNNING)

    registry.upsert([_idea(status=registry.STATUS_QUEUED)])

    assert registry.load(_id(_idea()))["status"] == registry.STATUS_RUNNING


def test_closed_status_cannot_be_declared_at_insert(store):
    # Закрытие — событие, а не начальное состояние. Идея, заведённая сразу
    # закрытой, никогда не была предложена: в реестре появилась бы запись о
    # решении, которого никто не принимал.
    with pytest.raises(ValueError, match="статус"):
        registry.upsert([_idea(status=registry.STATUS_DONE)])


def test_a_finished_idea_does_not_come_back(store):
    # done и dropped терминальны. Воскресни закрытая идея повторной
    # генерацией — реестр перестал бы быть памятью: он показывал бы только то,
    # что генератор нашёл сегодня, то есть ровно исходную болезнь.
    registry.upsert([_idea()])
    registry.mark(_id(_idea()), registry.STATUS_DONE, reason="раскатано")

    registry.upsert([_idea(status=registry.STATUS_NEW)])

    assert registry.load(_id(_idea()))["status"] == registry.STATUS_DONE


def test_a_dropped_idea_does_not_come_back(store):
    registry.upsert([_idea()])
    registry.mark(_id(_idea()), registry.STATUS_DROPPED, reason="объект исчез")

    registry.upsert([_idea(status=registry.STATUS_NEW)])

    assert registry.load(_id(_idea()))["status"] == registry.STATUS_DROPPED


def test_closed_idea_keeps_the_numbers_it_was_closed_with(store):
    # Переписать смету закрытой идеи значит задним числом переписать
    # основание уже принятого решения: разбор увидел бы не те числа, по
    # которым идею закрывали.
    registry.upsert([_idea(expected_rub=100_000.0)])
    registry.mark(_id(_idea()), registry.STATUS_DONE)

    registry.upsert([_idea(expected_rub=7.0)])

    assert registry.load(_id(_idea()))["expected_rub"] == 100_000.0


def test_regeneration_refreshes_the_numbers_of_a_live_idea(store):
    # Обратная сторона того же правила: у ЖИВОЙ идеи свежая смета обязана
    # доезжать. Заморозь её — и очередь ранжировалась бы по ценности,
    # посчитанной в день появления идеи, месяц назад.
    registry.upsert([_idea(expected_rub=100_000.0, test_cost_rub=10_000.0)])

    registry.upsert([_idea(expected_rub=20_000.0, test_cost_rub=1_000.0)])
    row = registry.load(_id(_idea()))

    assert (row["expected_rub"], row["test_cost_rub"]) == (20_000.0, 1_000.0)


def test_regeneration_keeps_the_link_to_the_action_and_the_bet(store):
    # Связь «идея → действие → ставка» ставит такт записи. Затри её повторная
    # генерация — и замер закрывал бы ставку, не зная, чью идею он проверял.
    registry.upsert([_idea()])
    registry.mark(_id(_idea()), registry.STATUS_RUNNING,
                  action_id="act-1", experiment_id="exp-1")

    registry.upsert([_idea()])
    row = registry.load(_id(_idea()))

    assert (row["action_id"], row["experiment_id"]) == ("act-1", "exp-1")


def test_mark_refuses_to_reopen_a_closed_idea(store):
    # Тихий отказ здесь опаснее падения: прогон отчитался бы об успехе, а
    # идея осталась бы в прежнем статусе — расхождение всплыло бы на разборе,
    # когда причину восстановить уже нечем (тот же довод, что у
    # experiments.check_transition).
    registry.upsert([_idea()])
    registry.mark(_id(_idea()), registry.STATUS_DONE)

    with pytest.raises(ValueError, match="закрыт"):
        registry.mark(_id(_idea()), registry.STATUS_RUNNING)


def test_mark_of_the_same_closed_status_is_idempotent(store):
    # Повтор отметки — обычное дело при переотправке; падать на нём значит
    # ронять прогон на том, что уже верно.
    registry.upsert([_idea()])
    registry.mark(_id(_idea()), registry.STATUS_DONE)

    assert registry.mark(_id(_idea()), registry.STATUS_DONE)["status"] == (
        registry.STATUS_DONE)


def test_mark_of_an_unknown_idea_is_refused(store):
    with pytest.raises(ValueError, match="нет в реестре"):
        registry.mark("такой-идеи-нет", registry.STATUS_RUNNING)
