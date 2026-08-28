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

ACCOUNT = "edu-vuz"


def test_idea_id_is_deterministic():
    # Недетерминированный идентификатор — это новая строка реестра на каждый
    # такт: тот же объект, тот же повод, и десять строк за десять дней.
    s = {"campaign_id": "123", "query": "колледж заочно"}
    assert (registry.idea_id("consolidate", s, ACCOUNT)
            == registry.idea_id("consolidate", s, ACCOUNT))


def test_idea_id_ignores_key_order_in_subject():
    # Словарь генератора собирается в произвольном порядке (разные ветки кода
    # кладут ключи по-разному). Зависимость идентификатора от порядка ключей
    # дала бы ту же болезнь, что и случайный id, но заметную не сразу.
    a = {"campaign_id": "123", "query": "колледж заочно"}
    b = {"query": "колледж заочно", "campaign_id": "123"}
    assert (registry.idea_id("consolidate", a, ACCOUNT)
            == registry.idea_id("consolidate", b, ACCOUNT))


def test_idea_id_separates_sources():
    # Одна и та же связка, найденная разными генераторами, — разные идеи с
    # разными рычагами. Схлопни их в один id — и вторая идея молча затрёт
    # первую вместе с её статусом.
    s = {"campaign_id": "123"}
    assert (registry.idea_id("proven", s, ACCOUNT)
            != registry.idea_id("consolidate", s, ACCOUNT))


def test_subject_key_is_the_same_across_sources():
    # Ключ объекта, наоборот, обязан НЕ зависеть от источника: на нём держится
    # отклонение человеком («этот объект не трогаем»), и генератор, сменивший
    # источник, не должен уметь обойти это «нет».
    s = {"campaign_id": "123"}
    assert (registry.subject_key(s, ACCOUNT)
            == registry.subject_key({"campaign_id": "123"}, ACCOUNT))
    assert (registry.idea_id("proven", s, ACCOUNT)
            != registry.idea_id("abtest", s, ACCOUNT))


def test_identity_is_scoped_to_the_cabinet():
    # Адрес объекта у половины генераторов — НАПРАВЛЕНИЕ (consolidate, market),
    # а одно и то же направление есть сразу в нескольких кабинетах. Без
    # кабинета в идентичности две разные находки схлопывались бы в одну строку
    # внутри одной же порции.
    s = {"kind": "demand", "direction": "vuz"}
    assert (registry.idea_id("market", s, "edu-vuz")
            != registry.idea_id("market", s, "edu-spo"))


def test_a_human_no_in_one_cabinet_does_not_silence_another():
    # Ключ объекта тоже с кабинетом: на нём держится отказ человека, и
    # общий ключ означал бы, что «нет» по направлению в одном кабинете
    # навсегда закрыло его во всех остальных.
    s = {"kind": "demand", "direction": "vuz"}
    assert (registry.subject_key(s, "edu-vuz")
            != registry.subject_key(s, "edu-spo"))


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

# Двойник таблицы (фикстура store) — в tests/conftest.py: его просят и
# тесты генераторов идей.



def _idea(campaign="123", source="consolidate", **over):
    """Полная идея генератора: всё обязательное на месте.

    Помощник намеренно отдаёт ВАЛИДНУЮ идею: тест на отказ ломает ровно одно
    поле, и тогда видно, что отказ пришёл именно из-за него, а не из-за
    случайного недостающего.
    """
    idea = {
        "source": source,
        "account": ACCOUNT,
        "subject": {"campaign_id": campaign},
        "tier": 1,
        "lane": "allocation",
        "expected_rub": 100_000.0,
        "test_cost_rub": 10_000.0,
        "horizon_days": 14,
        "success_rule": {"metric": "eff_cpl", "op": "<=", "value": 900.0},
        # Полезная нагрузка рычага. У применимого класса она обязательна:
        # идея, у которой её нет, доедет до такта записи и будет отвергнута
        # там — через сутки после того, как генератор ушёл.
        "action": _action(campaign),
    }
    idea.update(over)
    return idea


def _action(campaign="123"):
    return {
        "action_kind": "bidmodifier.add",
        "object_level": "campaign",
        "object_id": str(campaign),
        "direct_type": "DEVICE_MULTIPLIER",
        "key": "MOBILE",
        "idempotency_key": f"idea-{campaign}-bidmodifier.add",
    }


def _id(idea):
    return registry.idea_id(idea["source"], idea["subject"], idea["account"])


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
    assert row["subject_key"] == registry.subject_key({"campaign_id": "123"}, ACCOUNT)
    assert row["lane"] == "allocation"
    assert row["horizon_days"] == 14


def test_upsert_of_the_same_idea_does_not_create_a_second_row(store):
    # Ровно то, ради чего реестр и заведён: детерминированный генератор
    # находит ту же связку каждым тактом.
    registry.upsert([_idea()])
    registry.upsert([_idea()])
    assert len(store.table) == 1


# ------------------------------------------------- нагрузка действия

def _proposal(campaign="123", **over):
    """Идея класса 3: повод для человека, рычага записи у неё нет."""
    idea = _idea(campaign, tier=3, lane="proposal",
                 status=registry.STATUS_PROPOSED)
    idea.pop("action")
    idea.update(over)
    return idea


def test_action_payload_survives_the_registry(store):
    # Главный дефект задачи 11, найденный исполнением против прода: генератор
    # отдавал идею с нагрузкой, реестр проецировал её на COLUMNS — и action
    # терялся ещё до базы. Такт записи читает идеи ИЗ БАЗЫ, у прочитанной
    # строки нагрузки не было НИКОГДА, и каждая идея получала отказ
    # «применять нечем». Расчёт и запись — разные прогоны, в памяти нагрузку
    # передать нечем: она обязана лежать в колонке.
    registry.upsert([_idea()])

    assert registry.load(_id(_idea()))["action"] == _action("123")


def test_idea_of_an_applied_tier_without_an_action_is_refused(store):
    # Отказ на записи, а не молчаливый через сутки в такте записи. Генератор
    # ещё жив и обязан узнать о своём дефекте сразу; идея, доехавшая до базы
    # без рычага, вернётся отказом в чужом прогоне и в чужом логе.
    with pytest.raises(ValueError, match="действ"):
        registry.upsert([_idea(action=None)])
    assert store.table == {}


def test_idea_of_an_applied_tier_with_an_empty_action_is_refused(store):
    # Пустой словарь — не «нагрузка есть, она пустая», а её отсутствие:
    # применять по нему нечего ровно так же.
    with pytest.raises(ValueError, match="действ"):
        registry.upsert([_idea(action={})])


def test_arithmetic_and_bet_tiers_need_an_action_too(store):
    # Требование держится на классе, а не на одном значении: применяются все
    # три (tier.APPLIED_TIERS), и любой из них без рычага мёртв одинаково.
    for tier_value in (0, 2):
        with pytest.raises(ValueError, match="действ"):
            registry.upsert([_idea(tier=tier_value, action=None)])


def test_proposal_is_written_without_an_action(store):
    # Обратная сторона: у предложения рычага нет по определению, и требовать
    # с него нагрузку значило бы не пускать в реестр ровно те идеи, ради
    # экрана которых он и заведён.
    registry.upsert([_proposal()])

    assert registry.load(_id(_proposal()))["action"] is None


def test_proposal_carrying_an_action_is_refused(store):
    # Предложение с нагрузкой — предложение, притворяющееся применимым.
    # Класс 3 не едет в кабинет ни при какой ступени, и нагрузка у него
    # означает, что генератор перепутал класс: это дефект, а не мелочь.
    with pytest.raises(ValueError, match="класс 3"):
        registry.upsert([_proposal(action=_action())])


def test_action_is_not_part_of_the_identity(store):
    # Идентичность — пара (источник, объект). Войди нагрузка в отпечаток, и
    # новая ставка заводила бы идею заново: пустая история, снятый отказ
    # человека, тот же объект под новым id.
    a = _idea()
    b = _idea(action={**_action(), "key": "DESKTOP"})

    assert _id(a) == _id(b)
    registry.upsert([a])
    registry.upsert([b])
    assert len(store.table) == 1


def test_regeneration_refreshes_the_action_of_a_live_idea(store):
    # Нагрузка — число генератора, как ожидаемая ценность: заморозь её, и
    # такт записи применял бы ставку, посчитанную в день появления идеи.
    registry.upsert([_idea()])
    registry.upsert([_idea(action={**_action(), "key": "DESKTOP"})])

    assert registry.load(_id(_idea()))["action"]["key"] == "DESKTOP"


def test_action_is_declared_in_the_generator_fields():
    # Тот же довод по тексту модуля: поле, забытое в GENERATOR_FIELDS,
    # пишется первой находкой и больше никогда не обновляется.
    assert "action" in registry.GENERATOR_FIELDS
    assert "action" in registry.COLUMNS


def test_action_column_is_added_to_the_live_table_by_alter():
    # Таблица уже создана в бою: правка тела CREATE TABLE её не догонит, и
    # первый же прогон упадёт на неизвестной колонке. Колонка добавляется
    # отдельным идемпотентным оператором — как остальные поздние колонки
    # схемы агента.
    ddl = "\n".join(AGENT_DDL)
    alters = [s for s in AGENT_DDL
              if "ALTER TABLE edu_agent_ideas" in s and "action" in s]
    assert alters, "нет ALTER TABLE edu_agent_ideas ... ADD COLUMN action"
    assert "ADD COLUMN IF NOT EXISTS action" in " ".join(alters[0].split())
    assert "JSONB" in alters[0].upper()
    # И НЕ в теле CREATE TABLE: там она досталась бы только новым базам.
    body = ddl.split("CREATE TABLE IF NOT EXISTS edu_agent_ideas", 1)[1]
    assert "action " not in body.split(")", 1)[0]


def test_action_goes_to_the_query_as_json_and_empty_one_as_null():
    # JSONB-параметр едет строкой JSON, как subject и success_rule. Пустая
    # нагрузка обязана лечь NULL-ом, а не литералом 'null': колонка читается
    # как «нагрузки нет», и два разных способа сказать это разъедутся на
    # первом же запросе с IS NULL.
    with_action = registry._json_params({"idea_id": "x", "action": {"a": 1}})
    without = registry._json_params({"idea_id": "x", "action": None})

    assert with_action["action"] == '{"a": 1}'
    assert without["action"] is None


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


# ------------------------------------------------ «нет» человека навсегда

def test_human_rejection_silences_the_idea_permanently(store):
    # Генератор детерминирован и найдёт её снова следующим тактом. Без этого
    # экран предложений за месяц превращается в список, который человек
    # перестаёт читать, — а вместе с ним перестаёт читать и новое.
    registry.upsert([_idea()])
    registry.reject(_id(_idea()), by="pavel", reason="направление закрываем")

    registry.upsert([_idea()])
    row = registry.load(_id(_idea()))

    assert row["status"] == registry.STATUS_DROPPED
    assert row["dropped_reason"].startswith("человек")


def test_rejection_is_remembered_by_subject_not_by_row(store):
    # Тот же объект, найденный ДРУГИМ генератором, приезжает под другим
    # идентификатором: idea_id выведен из пары (источник, объект). Помни
    # реестр отказ по строке — обход был бы бесплатным, и «вынести связку X»
    # вернулось бы на экран под новым id уже назавтра.
    rejected = _idea(source="consolidate")
    registry.upsert([rejected])
    registry.reject(_id(rejected), by="pavel", reason="эту связку не трогаем")

    same_subject_other_source = _idea(source="proven")
    registry.upsert([same_subject_other_source])
    row = registry.load(_id(same_subject_other_source))

    assert _id(same_subject_other_source) != _id(rejected)
    assert row["subject_key"] == registry.subject_key(rejected["subject"],
                                                      rejected["account"])
    assert row["status"] == registry.STATUS_DROPPED
    assert row["dropped_reason"].startswith("человек")
    assert row["rejected_by"] == "pavel"


def test_rejection_silences_a_live_idea_about_the_same_subject(store):
    # Человек — высшая инстанция: если объект закрыт, идея о нём снимается,
    # даже если её уже успели взять в работу вторым источником.
    live = _idea(source="proven")
    registry.upsert([live])
    registry.mark(_id(live), registry.STATUS_RUNNING)

    other = _idea(source="consolidate")
    registry.upsert([other])
    registry.reject(_id(other), by="pavel", reason="объект закрыт")

    registry.upsert([live])
    assert registry.load(_id(live))["status"] == registry.STATUS_DROPPED


def test_rejection_of_one_subject_does_not_touch_another(store):
    # Обратная сторона: запрет по объекту не должен глушить соседей —
    # «нет» одному объекту, а не всему источнику.
    registry.upsert([_idea(campaign="123"), _idea(campaign="456")])
    registry.reject(_id(_idea(campaign="123")), by="pavel", reason="не трогаем")

    registry.upsert([_idea(campaign="456")])

    assert registry.load(_id(_idea(campaign="456")))["status"] == (
        registry.STATUS_NEW)


def test_machine_drop_does_not_silence_the_object(store):
    # Машина снимает идею по своим причинам («объект исчез», «данные не
    # приехали»), и это НЕ запрет человека. Считай реестр любое dropped
    # запретом — агент замолчал бы про объект после первой же технической
    # осечки, и вернуть его было бы нечем.
    dropped = _idea(source="consolidate")
    registry.upsert([dropped])
    registry.mark(_id(dropped), registry.STATUS_DROPPED, reason="объект исчез")

    other_source = _idea(source="proven")
    registry.upsert([other_source])

    assert registry.load(_id(other_source))["status"] == registry.STATUS_NEW


def test_reject_records_who_and_when(store):
    # Признак «сказал человек» — колонка rejected_by, а не префикс
    # свободного текста: текст однажды перепишут ради формулировки, и правило
    # молча перестанет срабатывать.
    registry.upsert([_idea()])
    row = registry.reject(_id(_idea()), by="pavel", reason="направление закрываем")

    assert row["rejected_by"] == "pavel"
    assert row["rejected_at"] is not None
    assert "направление закрываем" in row["dropped_reason"]


def test_reject_without_a_person_is_refused(store):
    # Отказ без автора неотличим от машинного снятия — а различие между ними
    # и есть всё содержание этого механизма.
    registry.upsert([_idea()])
    with pytest.raises(ValueError, match="автор"):
        registry.reject(_id(_idea()), by="", reason="почему-то")


def test_taking_into_work_records_who_and_when(store):
    # Предложение (класс 3) применить нечем: рычага записи у него нет, и
    # сделать его может только человек. Имя взявшего — не украшение: без него
    # очередь через неделю не отвечает, взялся за идею кто-нибудь или она
    # просто висит.
    registry.upsert([_idea()])
    row = registry.take_into_work(_id(_idea()), by="pavel")

    assert row["status"] == registry.STATUS_QUEUED
    assert row["queued_by"] == "pavel"
    assert row["queued_at"] is not None
    # Взятие в работу — не отказ: объект остаётся живым для генераторов.
    assert not row.get("rejected_by")


def test_taking_into_work_without_a_person_is_refused(store):
    # queued без имени неотличим от queued, поставленного тактом записи, — а
    # ждать их исполнения надо от разных исполнителей.
    registry.upsert([_idea()])
    with pytest.raises(ValueError, match="автор"):
        registry.take_into_work(_id(_idea()), by="")


def test_a_closed_idea_is_not_taken_into_work(store):
    # Закрытая строка есть запись о случившемся: взять её в работу значило бы
    # переписать исход задним числом.
    done = _idea()
    registry.upsert([done])
    registry.mark(_id(done), registry.STATUS_DONE, reason="раскатано")

    with pytest.raises(ValueError, match="закрыта"):
        registry.take_into_work(_id(done), by="pavel")


def test_reject_of_an_unknown_idea_is_refused(store):
    with pytest.raises(ValueError, match="нет в реестре"):
        registry.reject("такой-идеи-нет", by="pavel", reason="нет")


def test_reject_of_a_finished_idea_keeps_its_outcome_but_silences_the_subject(store):
    # Отказ по уже раскатанной идее не переписывает её исход — исход
    # случился. Но объект после этого молчит: человек сказал «больше не
    # предлагать», и относится это к объекту, а не к строке.
    done = _idea(source="proven")
    registry.upsert([done])
    registry.mark(_id(done), registry.STATUS_DONE, reason="раскатано")
    registry.reject(_id(done), by="pavel", reason="хватит")

    assert registry.load(_id(done))["status"] == registry.STATUS_DONE

    other_source = _idea(source="consolidate")
    registry.upsert([other_source])
    assert registry.load(_id(other_source))["status"] == registry.STATUS_DROPPED


# ------------------------------------------------------- мелочи, но злые

def test_two_ideas_with_the_same_identity_in_one_batch_are_refused(store):
    # Одна и та же находка дважды в одной порции — это двойной счёт: в отчёте
    # такта она посчитается два раза, а в реестр ляжет одна строка, и
    # расхождение будет нечем объяснить. Молча схлопывать нельзя: генератор
    # обязан узнать, что нашёл одно и то же дважды.
    with pytest.raises(ValueError, match="дважды"):
        registry.upsert([_idea(), _idea()])
    assert store.table == {}


def test_broken_tier_type_is_refused_not_crashed(store):
    # Список вместо числа роняет int() TypeError-ом мимо всех проверок:
    # прогон падает трассой вместо внятного отказа, и порция уходит частично.
    with pytest.raises(ValueError, match="класс достоверности"):
        registry.upsert([_idea(tier=["1"])])


def test_prepared_row_has_exactly_the_declared_columns(store):
    # Строка собирается в Python, а SQL пишет то, что дали: поле, забытое при
    # сборке, уехало бы в базу как NULL и всплыло бы уже на отчёте.
    registry.upsert([_idea()])
    assert set(store.table[_id(_idea())]) == set(registry.COLUMNS)


def test_params_of_the_query_are_projected_onto_the_columns():
    # Строка, прочитанная из базы, несёт ещё created_at и updated_at, а
    # слияние может дописать в неё что угодно. Без проекции лишний ключ
    # уехал бы в запрос, где его никто не ждёт.
    params = registry._json_params({"idea_id": "x", "посторонний ключ": 1})
    assert set(params) == set(registry.COLUMNS)


def test_every_placeholder_of_the_query_is_a_declared_column():
    # Опечатка в имени подстановки не видна ничем, кроме живой базы: запрос
    # соберётся, а на первом же прогоне упадёт KeyError-ом. Проверка дешёвая
    # и ловит ровно этот класс.
    import re

    placeholders = set(re.findall(r"%\((\w+)\)s", registry.UPSERT_SQL))
    assert placeholders == set(registry.COLUMNS)


def test_detail_is_not_part_of_the_identity(store):
    # Доказательства плавают от прогона к прогону — состав связок-доноров у
    # выноса меняется каждый день. Войди они в отпечаток, и идея заводилась
    # бы заново каждым прогоном: пустая история и снятый отказ человека на
    # том же самом объекте.
    a = _idea(detail={"queries": ["первая"]})
    b = _idea(detail={"queries": ["первая", "вторая"]})

    assert _id(a) == _id(b)
    registry.upsert([a])
    registry.upsert([b])
    assert len(store.table) == 1


def test_detail_is_refreshed_on_every_find(store):
    # И обратное: доказательства обязаны обновляться. Заморозь их — и экран
    # показывал бы человеку обоснование недельной давности, по которому он
    # принимал бы сегодняшнее решение.
    registry.upsert([_idea(detail={"queries": ["первая"]})])
    rows = registry.upsert([_idea(detail={"queries": ["вторая"]})])

    assert rows[0]["detail"] == {"queries": ["вторая"]}


def test_detail_may_be_absent():
    # Идея, вся суть которой в адресе и числах, доказательств сверх них не
    # обязана нести: пустое поле — законный случай, а не недосмотр.
    assert registry._prepare(_idea())["detail"] is None


def test_detail_must_be_an_object():
    # Список или строка в колонке JSONB прочитались бы потребителем как
    # объект и уронили бы экран, а не генератор, который их туда положил.
    with pytest.raises(registry.InvalidIdea, match="словарь"):
        registry._prepare(_idea(detail=["первая", "вторая"]))


def test_closed_idea_keeps_the_evidence_it_was_closed_on(store):
    # Смета закрытой идеи — основание уже принятого решения. Обнови её
    # задним числом, и разбор остался бы без тех доказательств, по которым
    # идею закрывали.
    registry.upsert([_idea(detail={"queries": ["первая"]})])
    key = _id(_idea())
    registry.reject(key, by="Павел", reason="не сейчас")

    registry.upsert([_idea(detail={"queries": ["вторая"]})])

    assert registry.load(key)["detail"] == {"queries": ["первая"]}


def test_insert_column_list_matches_the_declared_columns():
    # Колонка, объявленная в COLUMNS и забытая в списке INSERT, уехала бы в
    # базу значением по умолчанию — а на UPDATE при этом обновлялась бы:
    # строка вела бы себя по-разному в зависимости от того, первая это запись
    # или повторная.
    head = registry.UPSERT_SQL.split("VALUES", 1)[0]
    names = head.split("(", 1)[1].rsplit(")", 1)[0]
    listed = [name.strip() for name in names.split(",")]
    assert listed == list(registry.COLUMNS)


def test_initial_status_is_honoured_but_never_re_declared(store):
    # Начальный статус генератор задать вправе — предложение (класс 3)
    # заводится сразу как proposed, оно не проходит через очередь. А вот
    # ВТОРАЯ находка той же идеи статуса уже не касается: иначе у реестра
    # оказалось бы два хозяина жизненного цикла — тот, кто применяет, и тот,
    # кто генерирует.
    registry.upsert([_idea(status=registry.STATUS_RUNNING)])
    registry.upsert([_idea(status=registry.STATUS_NEW)])

    assert registry.load(_id(_idea()))["status"] == registry.STATUS_RUNNING


# ------------------------------------------------- поиск идеи по наряду


def test_an_idea_is_found_by_the_order_it_carries(store):
    # Билдер знает про идею одно — order_id кампании, которую по ней завели.
    # Обратный путь (наряд → идея) нужен, чтобы вернуть исход в реестр.
    order = {"order_id": "consolidate-vpo", "campaign_name": "vpo / consolidate"}
    registry.upsert([_idea(action={"kind": "campaign.create",
                                   "object_id": "consolidate-vpo",
                                   "idempotency_key": "k-1",
                                   "payload": {"order": order}})])

    found = registry.find_by_order("consolidate-vpo", account=ACCOUNT)
    assert found is not None
    assert found["idea_id"] == _id(_idea())


def test_an_unknown_order_is_not_found(store):
    assert registry.find_by_order("нет-такого", account=ACCOUNT) is None


def test_the_order_lookup_does_not_filter_by_status():
    # Исход нужен как раз у выносов, доживших до конца, — а такая идея уже
    # закрыта. Фильтр по статусу в этом запросе спрятал бы ровно те кампании,
    # ради которых обратная связь и заводилась.
    sql = " ".join(registry.SELECT_BY_ORDER_SQL.split())
    assert "status" not in sql
    assert "action" in sql and "order_id" in sql
