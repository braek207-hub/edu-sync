# -*- coding: utf-8 -*-
"""Чёрный ящик прогонов: отчёт и отказы переживают лог GitHub Actions.

До него весь ход мысли агента жил одним JSON-ом в логе прогона: лог хранится
ограниченный срок, не запрашивается и не сравнивается с соседним прогоном.
На бете вопрос «почему он тогда так решил» задают каждый день, и ответом не
может быть «лог протух».
"""

import pytest

from sync.agent import blackbox, rejects


def _action(object_id="111", direct_type="bid_modifier:device", key="MOBILE",
            idem="k1", **extra):
    return {"object_id": object_id, "direct_type": direct_type, "key": key,
            "idempotency_key": idem, **extra}


def test_run_mode_separates_three_kinds_of_run():
    # Репетиция по боевому кабинету и прогон по песочнице считаются на РАЗНЫХ
    # данных. Сравнивать их как одно и то же — ошибка разбора, поэтому режим
    # не сводится к флагу «боевой».
    assert blackbox.run_mode(sandbox=False, dry_run=False) == blackbox.MODE_APPLY
    assert blackbox.run_mode(sandbox=False, dry_run=True) == blackbox.MODE_REHEARSAL
    assert blackbox.run_mode(sandbox=True, dry_run=True) == blackbox.MODE_SANDBOX


def test_run_context_reads_the_code_version(monkeypatch):
    # Без версии кода «агент вчера решал иначе» и «вчера был другой код»
    # выглядят одинаково.
    monkeypatch.setenv("GITHUB_SHA", "abc123")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.delenv("GITHUB_SERVER_URL", raising=False)

    context = blackbox.run_context()

    assert context["code_sha"] == "abc123"
    assert context["run_url"] == "https://github.com/owner/repo/actions/runs/42"


def test_local_run_has_no_invented_link(monkeypatch):
    for name in ("GITHUB_SHA", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"):
        monkeypatch.delenv(name, raising=False)

    context = blackbox.run_context()

    assert context == {"code_sha": None, "run_url": None}


def test_run_ids_are_unique():
    assert blackbox.new_run_id() != blackbox.new_run_id()


def test_write_failure_never_breaks_the_run(monkeypatch):
    # Прогон, применивший изменения в кабинет и упавший на записи СВОЕГО
    # отчёта, — худшее из состояний: изменения живут, журнала нет.
    def _broken():
        raise RuntimeError("база недоступна")

    monkeypatch.setattr(blackbox, "ensure_blackbox_tables", _broken)

    out = blackbox.save_run("r1", stage="e1", mode=blackbox.MODE_APPLY,
                            report={"verdict": "OK"})

    assert out["saved"] is False
    assert "RuntimeError" in out["error"]


def test_rejects_carry_the_money_at_stake():
    rows = rejects.from_groups(
        [(rejects.BUDGET, [_action(baseline_daily_rub=1200.0)])],
        account="acc-1", stage="e1", risks={"k1": 340.5})

    assert rows[0]["cost_rub"] == 1200.0
    assert rows[0]["risk_rub"] == 340.5
    assert rows[0]["account"] == "acc-1"
    assert rows[0]["reason"] == rejects.BUDGET


def test_unpriced_action_is_not_silently_free():
    # У неприменённого действия своего risk_rub нет: цена живёт в словаре
    # прогона. Нет её там — ноль, и это видно как ноль, а не как «дёшево».
    rows = rejects.from_groups([(rejects.RUN_CAP, [_action(idem="нет-в-словаре")])],
                              risks={"k1": 999.0})

    assert rows[0]["risk_rub"] == 0.0


def test_unknown_reason_is_kept_not_dropped():
    # Отказ, потерянный из-за опечатки в коде причины, — ровно та слепота,
    # ради устранения которой журнал заводится.
    rows = rejects.from_groups([("опечатка", [_action()])])

    assert rows[0]["reason"] == rejects.UNKNOWN
    assert rows[0]["detail"]["reason_given"] == "опечатка"


def test_by_reason_sorts_by_weight():
    rows = rejects.from_groups([
        (rejects.BUDGET, [_action(idem="a"), _action(idem="b")]),
        (rejects.COOLDOWN, [_action(idem="c")]),
    ])

    assert list(rejects.by_reason(rows)) == [rejects.BUDGET, rejects.COOLDOWN]


def test_non_dict_entries_do_not_break_collection():
    rows = rejects.from_groups([(rejects.BUDGET, [None, "строка", _action()])])

    assert len(rows) == 1


@pytest.mark.parametrize("reason", sorted(rejects.KNOWN_REASONS))
def test_every_known_reason_survives_a_round_trip(reason):
    rows = rejects.from_groups([(reason, [_action()])])

    assert rows[0]["reason"] == reason
