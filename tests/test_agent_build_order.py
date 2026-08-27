# -*- coding: utf-8 -*-
"""
tests/test_agent_build_order.py — наряд билдеру: контракт со стороны агента.

Наряд — единственный способ агента завести кампанию. Он пересекает границу
двух репозиториев (edu-sync пишет, «EDU кампании» читает), и валидатор здесь
сторожит ровно то, чего билдер проверить уже не сможет: у него на входе будет
готовый файл, и «почему в нём нет доноров» он не узнает никогда.

Проверка формы у ОТПРАВИТЕЛЯ — половина дела; вторая половина живёт на стороне
получателя (EDU кампании/test_order.py читает пример этого же наряда и
собирает по нему уровень). Правило verify-at-consumer: контракт держится
кодом получателя, а не спецификацией.
"""

import json
import os
import subprocess

import pytest

from sync.agent import build_order


_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ACCOUNT = "account1-506453-ln8s"


def _order(**over):
    order = {
        "order_id": "consolidate-vuz-2026-09-01",
        "idea_id": "d96b5cf53b8073c1c6d122e5",
        "kind": build_order.KIND_CONSOLIDATE,
        "account": ACCOUNT,
        "level_slug": "vuz_consolidate_2026_09",
        "campaign_name": "vpo / consolidate / consolidate-vuz-2026-09-01",
        "direction": "vpo",
        "queries": [
            {"phrase": "колледж заочно москва", "donor_campaign_id": "111",
             "cost_rub": 18_400.0, "conversions": 12},
            {"phrase": "заочный колледж после 9", "donor_campaign_id": "222",
             "cost_rub": 9_100.0, "conversions": 6},
        ],
        "donor_negatives": [
            {"campaign_id": "111", "phrases": ["колледж заочно москва"]},
            {"campaign_id": "222", "phrases": ["заочный колледж после 9"]},
        ],
        "campaign": {"weekly_budget": 60_000, "target_cpa": 1_600,
                     "counter_id": 98_627_983, "goal_id": 360_811_375},
        "horizon_days": 30,
        "success_rule": {"metric": "cpa", "comparison": "did_vs_holdout",
                         "threshold": 1.0},
    }
    order.update(over)
    return order


# ------------------------------------------------ кросс-минусовка доноров


def test_order_without_donor_negatives_is_invalid():
    # Шаг 1 задачи 17 дословно. Новая кампания забирает фразы у доноров, и без
    # минусовки обе остаются в одном аукционе одного рекламодателя. Это не
    # теория: на дистанте 73–75% московских ключей дублировались в РФ-версии
    # со ставкой на треть ниже (билдер, exclude_regions, 25.08.2026).
    with pytest.raises(ValueError, match="кросс-минусовк"):
        build_order.validate(_order(donor_negatives=[]))


def test_a_taken_phrase_must_be_muted_at_its_own_donor():
    # Список доноров непуст — и всё равно наряд негоден, если хоть одна
    # вынесенная фраза осталась без минусовки У СВОЕГО донора. Проверка
    # «поле не пустое» пропустила бы ровно тот случай, ради которого поле
    # заведено: два донора, минусовка выписана одному.
    order = _order(donor_negatives=[
        {"campaign_id": "111", "phrases": ["колледж заочно москва"]}])

    with pytest.raises(ValueError, match="заочный колледж после 9"):
        build_order.validate(order)


def test_muting_a_phrase_at_a_stranger_does_not_count():
    # Минусовка не там, где фраза работала, ничего не выключает: донор «222»
    # продолжит по ней торговаться. Совпадать обязаны пара (кампания, фраза),
    # а не множества по отдельности.
    order = _order(donor_negatives=[
        {"campaign_id": "111", "phrases": ["колледж заочно москва",
                                           "заочный колледж после 9"]}])

    with pytest.raises(ValueError, match="заочный колледж после 9"):
        build_order.validate(order)


def test_negatives_are_compared_case_and_space_insensitively():
    # Директ сам приводит фразу к нижнему регистру и схлопывает пробелы.
    # Считай агент иначе — валидатор отбивал бы наряды, которые в кабинете
    # сработали бы верно.
    order = _order(donor_negatives=[
        {"campaign_id": "111", "phrases": ["Колледж  Заочно Москва"]},
        {"campaign_id": "222", "phrases": [" заочный колледж после 9 "]}])

    assert build_order.validate(order)["donor_negatives"]


def test_negatives_for_a_campaign_outside_the_order_are_refused():
    # Минусовка адресована кампании, у которой наряд ничего не забирал:
    # агент выключил бы чужой рабочий трафик, и в откате этой строки нет —
    # rollback снимает ровно добавленное, а добавлено оно было зря.
    order = _order()
    order["donor_negatives"].append({"campaign_id": "333",
                                     "phrases": ["колледж заочно москва"]})

    with pytest.raises(ValueError, match="333"):
        build_order.validate(order)


# ----------------------------------------------------- критерий и горизонт


def test_order_without_a_success_rule_is_invalid():
    # Шаг 2 задачи 17. Кампания без критерия не закрывается никогда: замер
    # не знает, что считать удачей, и наряд останется в реестре навсегда.
    with pytest.raises(ValueError, match="критери"):
        build_order.validate(_order(success_rule={}))


def test_success_rule_without_a_comparison_base_is_invalid():
    # «CPA ниже 1.0» без базы сравнения — это «ниже чего?». Новая кампания
    # сравнивается с заповедником, а не сама с собой: своей истории у неё нет
    # вовсе, и «до и после» здесь физически невозможно.
    with pytest.raises(ValueError, match="сравнени"):
        build_order.validate(_order(
            success_rule={"metric": "cpa", "threshold": 1.0}))


def test_success_rule_with_an_unknown_comparison_is_invalid():
    with pytest.raises(ValueError, match="сравнени"):
        build_order.validate(_order(
            success_rule={"metric": "cpa", "threshold": 1.0,
                          "comparison": "на глаз"}))


def test_order_without_a_horizon_is_invalid():
    # Горизонт — срок, к которому наряд обязан отдать вердикт. Без него
    # кампания живёт вечно и никогда не попадает в разбор.
    with pytest.raises(ValueError, match="горизонт"):
        build_order.validate(_order(horizon_days=0))


# ---------------------------------------------------------- предмет наряда


def test_order_without_queries_is_invalid():
    # Наряд consolidate несёт фразы из боевого журнала — в этом весь его
    # смысл. Пустой список означал бы кампанию без единого ключа.
    with pytest.raises(ValueError, match="фраз"):
        build_order.validate(_order(queries=[]))


def test_a_query_without_a_donor_is_invalid():
    # Фраза без донора не проверяема: кросс-минусовку по ней не построить, и
    # факт «здесь уже потратили деньги» подтвердить нечем.
    with pytest.raises(ValueError, match="донор"):
        build_order.validate(_order(queries=[
            {"phrase": "колледж заочно москва", "cost_rub": 100.0,
             "conversions": 1}]))


def test_a_query_without_facts_is_invalid():
    # Вердикт этого наряда — деньги, уже потраченные по фразе. Наряд без
    # чисел — гипотеза, а гипотезы едут другим видом (expand), с другим
    # классом риска.
    with pytest.raises(ValueError, match="без фактов"):
        build_order.validate(_order(queries=[
            {"phrase": "колледж заочно москва", "donor_campaign_id": "111"}]))


def test_an_unknown_kind_is_invalid():
    with pytest.raises(ValueError, match="вид наряда"):
        build_order.validate(_order(kind="переделать всё"))


def test_a_level_slug_must_be_a_safe_directory_name():
    # Слаг становится ИМЕНЕМ ПАПКИ на диске билдера. Пробел, слэш или точки
    # уводят запись за пределы каталога уровня.
    with pytest.raises(ValueError, match="слаг"):
        build_order.validate(_order(level_slug="../vuz 2026"))


def test_campaign_without_a_goal_is_invalid():
    # Стратегия AVERAGE_CPA без цели невозможна, а без счётчика цель не
    # существует. Кампания уехала бы в кабинет и встала на ручных ставках.
    with pytest.raises(ValueError, match="цел"):
        build_order.validate(_order(campaign={
            "weekly_budget": 60_000, "target_cpa": 1_600,
            "counter_id": 98_627_983}))


def test_a_valid_order_survives_validation_unchanged_in_meaning():
    # Обратная сторона всех запретов: годный наряд обязан пройти. Иначе
    # валидатор чинится тем, что не пропускает ничего.
    order = build_order.validate(_order())

    assert order["kind"] == build_order.KIND_CONSOLIDATE
    assert [q["phrase"] for q in order["queries"]] == [
        "колледж заочно москва", "заочный колледж после 9"]


# ------------------------------------------------------- имя кампании


def test_an_order_whose_name_lost_the_order_id_is_invalid():
    # Имя — поле наряда, и проверять его обязан отправитель: билдер увидит
    # только готовую строку и «почему в ней нет order_id» не узнает никогда.
    with pytest.raises(ValueError, match="order_id"):
        build_order.validate(_order(campaign_name="ВПО / вынос / сентябрь"))


def test_an_order_without_a_campaign_name_is_invalid():
    with pytest.raises(ValueError, match="имени кампании"):
        build_order.validate(_order(campaign_name=""))


def test_campaign_name_carries_the_order_id():
    # Шаг 4 задачи 17. Связь «наряд → кампания в кабинете» иначе держится
    # только на памяти человека: drift.py сверяет кабинет с журналом по
    # объектам, а у новой кампании Id появляется лишь после создания.
    name = build_order.campaign_name(_order())

    assert "consolidate-vuz-2026-09-01" in name


def test_the_order_id_is_the_last_part_of_the_name():
    # Директ режет имя кампании по длине. Уедет order_id в хвост длинного
    # человеческого названия — обрежется именно он, и связь потеряется.
    name = build_order.campaign_name(_order(
        direction="очень длинное направление " * 4))

    assert len(name) <= build_order.NAME_LIMIT
    assert name.endswith("consolidate-vuz-2026-09-01")


def test_the_same_order_always_gets_the_same_name():
    # Идемпотентность заливки держится на имени (direct/upload.py ищет
    # кампанию по нему). Плавающее имя завело бы вторую кампанию на тот же
    # наряд.
    assert build_order.campaign_name(_order()) == build_order.campaign_name(
        _order())


# --------------------------------------------------- наряд из идеи реестра


def test_an_order_built_from_an_idea_keeps_its_link():
    # Наряд без idea_id — сирота: замер не сможет вернуть исход в реестр, и
    # идея останется running навсегда.
    order = build_order.from_idea(
        {"idea_id": "i-1", "account": ACCOUNT,
         "subject": {"kind": "consolidate", "direction": "vpo"},
         "horizon_days": 30,
         "success_rule": {"metric": "cpa", "op": "<=", "value": 1_600.0,
                          "comparison": "did_vs_holdout"},
         "detail": {"queries": _order()["queries"]}},
        campaign={"weekly_budget": 60_000, "target_cpa": 1_600,
                  "counter_id": 98_627_983, "goal_id": 360_811_375},
        today="2026-09-01")

    assert order["idea_id"] == "i-1"
    assert order["order_id"].endswith("2026-09-01")
    assert build_order.validate(order)


def test_an_order_from_an_idea_mutes_every_taken_phrase():
    # Кросс-минусовка выводится из самих фраз, а не приписывается человеком:
    # донор каждой фразы уже назван в идее, и забыть его здесь невозможно.
    order = build_order.from_idea(
        {"idea_id": "i-1", "account": ACCOUNT,
         "subject": {"kind": "consolidate", "direction": "vpo"},
         "horizon_days": 30,
         "success_rule": {"metric": "cpa", "op": "<=", "value": 1_600.0,
                          "comparison": "did_vs_holdout"},
         "detail": {"queries": _order()["queries"]}},
        campaign=_order()["campaign"], today="2026-09-01")

    muted = {(n["campaign_id"], p) for n in order["donor_negatives"]
             for p in n["phrases"]}
    assert muted == {("111", "колледж заочно москва"),
                     ("222", "заочный колледж после 9")}


# ------------------------------------------------------ пример для билдера


def test_the_shipped_example_is_a_valid_order():
    # Пример в репозитории — это то, что читает тест на стороне билдера.
    # Разъедься он с валидатором — обе стороны остались бы зелёными, а бой
    # упал бы на первом наряде.
    with open(build_order.EXAMPLE_PATH, encoding="utf-8") as f:
        example = json.load(f)

    assert build_order.validate(example)


def test_the_example_is_tracked_by_git():
    # Пример — общий артефакт двух репозиториев: тест билдера читает ИМЕННО
    # его. Останься он только на диске, контракт держался бы на файле,
    # которого у получателя нет, — а здесь всё было бы зелено.
    #
    # Ловушка не гипотетическая: .gitignore этого репозитория прячет *.json
    # целиком (защита от сервисных ключей), и пример молча не коммитился.
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch",
         os.path.relpath(build_order.EXAMPLE_PATH, _REPO)],
        cwd=_REPO, capture_output=True, text=True)

    assert tracked.returncode == 0, (
        "Пример наряда не отслеживается git — проверь .gitignore:\n"
        + tracked.stderr)
