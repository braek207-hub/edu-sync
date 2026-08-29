# -*- coding: utf-8 -*-
"""
tests/test_agent_master.py — Мастер кампаний в контуре (sync/agent/master.py).

Модуль отвечает на один вопрос: почему кампания, тратящая деньги, отсутствует
в витрине настроек. Ответов два, и они лечатся в разных местах — API её отдаёт
(дефект синка, чинится кодом) или API про неё молчит (Мастер кампаний, чинится
человеком). Всё, что здесь проверяется, — про цену их перепутать.

  * СЛИЯНИЕ ДВУХ СЛУЧАЕВ. 25.08.2026 всю слепую зону списали на Мастер
    кампаний, а замер по явным Id (probe_blind_campaigns_api, run 32866947540)
    показал 12 обычных TEXT_CAMPAIGN из 15: две трети «Мастера» были падением
    синка на форме массивов. Кампания, которую API отдаёт, обязана уехать в
    visible_in_api и НЕ попасть в карточки — иначе баг кода превращается в
    вечную рекомендацию человеку и живёт дальше;
  * МОЛЧАНИЕ БЕЗ ВОПРОСА. Прогон без кабинетов (--skip-direct, упавший токен)
    не спрашивает никого, и тогда каждая кампания вне витрины выглядит
    Мастером. Карточки в этом случае наружу не идут;
  * ЧУЖОЙ КАБИНЕТ В АДРЕСЕ. У Мастера логина взять негде: campaigns.get о нём
    молчит, а справочник «кампания → логин» строится тем же методом. Кабинет
    выводится из проекта, и проект, живущий в двух кабинетах, обязан дать
    отказ, а не догадку: идея с чужим кабинетом посчитается по чужому порогу;
  * ЦЕНА ЛИДА, ПОСЧИТАННАЯ С САМОЙ СОБОЙ. При 10 % расхода кабинета кампания
    заметно тянет на себя базу, с которой её же и сравнивают, поэтому база
    считается по видимым кампаниям;
  * СТРОКИ, КОТОРЫЕ НЕ КАМПАНИИ. В фактах живут шаблоны разметки
    («{campaignid}») — замер 29.08.2026 нашёл две таких на нуле расхода. Они
    раздували счётчик слепых кампаний, не добавляя ни рубля.

Сети и БД тесты не требуют: опрос API подменяется функцией-двойником.
"""

import pytest

from sync.agent import master


ACCOUNT = "edu-vse"
OTHER = "edu-vuz"
WINDOW = ("2026-08-01", "2026-08-28")


def _fact(campaign_id, day="2026-08-10", cost=0.0, eff_leads=0.0, **over):
    row = {
        "fact_date": day,
        "campaign_id": campaign_id,
        "campaign_name": f"кампания {campaign_id}",
        "project": "vse",
        "direction": "spo",
        "cost": cost,
        "clicks": 0.0,
        "impressions": 0.0,
        "leads": 0.0,
        "eff_leads": eff_leads,
        "payments_fact": 0.0,
        "revenue": 0.0,
    }
    row.update(over)
    return row


def _facts():
    """Кабинет: две видимые кампании и один Мастер на четверть расхода."""
    return [
        _fact("100", cost=600_000.0, eff_leads=100.0),
        _fact("200", cost=400_000.0, eff_leads=100.0),
        _fact("900", cost=500_000.0, eff_leads=50.0),
    ]


def _settings():
    return {"100": {}, "200": {}}


def _login_by_campaign():
    return {"100": ACCOUNT, "200": ACCOUNT}


def _silent(login, ids):
    """Кабинет, который про спрошенные Id молчит, — так отвечает Мастер."""
    return {}


def _view(**over):
    args = dict(facts=_facts(), settings_rows=_settings(),
                login_by_campaign=_login_by_campaign(),
                logins=[ACCOUNT], window_from=WINDOW[0], window_to=WINDOW[1],
                fetch=_silent)
    args.update(over)
    return master.view(**args)


def test_silent_campaign_becomes_a_card_with_account_price():
    """Мастер попадает в карточки, а цена лида кабинета считается БЕЗ него.

    600 000 + 400 000 рублей на 200 эффективных лидов видимых кампаний — 5 000 ₽
    за лид. Вошла бы сама слепая кампания (500 000 ₽ на 50 лидах), база
    поднялась бы до 6 000 ₽, и кампания сравнивалась бы с числом, в которое
    сама и внесла перекос.
    """
    view = _view()

    assert view["silent_in_api"] == 1
    assert view["cost_silent_in_api_rub"] == 500_000.0
    assert view["visible_in_api"] == 0

    row = view["rows"][0]
    assert row["campaign_id"] == "900"
    assert row["account"] == ACCOUNT
    assert row["base_cpl_rub"] == 5_000.0
    assert row["account_cost_rub"] == 1_500_000.0
    assert row["share_of_account"] == pytest.approx(1 / 3, rel=1e-6)


def test_campaign_the_api_returns_is_not_a_master_campaign():
    """API отдал кампанию — это дефект синка, а не Мастер.

    Разница дорогая: рекомендация человеку «посмотрите на эту кампанию»
    оставила бы баг кода жить, и он молча продолжал бы прятать кампании.
    """
    def answers(login, ids):
        return {"900": {"login": login, "campaign_name": "обычная",
                        "campaign_type": "TEXT_CAMPAIGN", "state": "ON",
                        "status": "ACCEPTED"}}

    view = _view(fetch=answers)

    assert view["visible_in_api"] == 1
    assert view["cost_visible_in_api_rub"] == 500_000.0
    assert view["silent_in_api"] == 0
    # Карточка собирается, но несёт ответ API — генератор по нему и отсеет её.
    assert view["rows"][0]["api"]["campaign_type"] == "TEXT_CAMPAIGN"


def test_no_cabinet_asked_suppresses_cards():
    """Никого не спросили — молчание ничего не доказывает.

    Прогон с --skip-direct или упавшим токеном не отличает Мастера от
    недоехавшей кампании, и выпустить карточки означало бы наштамповать
    человеку рекомендаций про дефект синка.
    """
    view = _view(logins=[])

    assert view["rows"] == []
    assert view["rows_suppressed"]
    assert view["accounts_asked"] == []


def test_failed_cabinet_is_named_not_swallowed():
    """Упавший кабинет — причина в отчёте, а не тихое «Мастер найден»."""
    def boom(login, ids):
        raise RuntimeError("токен протух")

    view = _view(fetch=boom)

    assert "токен протух" in view["api_errors"][ACCOUNT]
    assert view["rows"] == []


def test_ambiguous_project_refuses_to_guess_the_account():
    """Проект в двух кабинетах — отказ с причиной, а не выбор наугад."""
    facts = _facts() + [_fact("300", cost=100_000.0, eff_leads=20.0)]
    login_by_campaign = dict(_login_by_campaign(), **{"300": OTHER})

    view = _view(facts=facts, settings_rows={"100": {}, "200": {}, "300": {}},
                 login_by_campaign=login_by_campaign,
                 logins=[ACCOUNT, OTHER])

    assert view["rows"] == []
    assert [s["reason"] for s in view["skipped"]] == [
        master.REASON_AMBIGUOUS_PROJECT]


def test_utm_template_rows_are_not_campaigns():
    """«{campaignid}» — шаблон разметки, а не кампания.

    Спрашивать API про него нечем, а в счётчике слепой зоны такие строки
    раздували число кампаний, не добавляя ни рубля.
    """
    facts = _facts() + [_fact("{campaignid}", cost=1.0)]

    view = _view(facts=facts)

    assert [s["reason"] for s in view["skipped"]] == [
        master.REASON_NOT_A_CAMPAIGN]
    assert [r["campaign_id"] for r in view["rows"]] == ["900"]


def test_window_bounds_are_inclusive_and_sum_not_pace():
    """Окно считается суммой за дни окна, а не темпом, растянутым на окно.

    Подмена уже стоила двух разных слепых долей под одним именем в один день
    (замер 25.08.2026: 9,45 % такта записи против 6,18 % такта расчёта).
    """
    facts = [
        _fact("100", day="2026-08-01", cost=600_000.0, eff_leads=100.0),
        _fact("200", day="2026-08-28", cost=400_000.0, eff_leads=100.0),
        _fact("900", day="2026-07-31", cost=999_999.0),   # день до окна
        _fact("900", day="2026-08-28", cost=500_000.0, eff_leads=50.0),
    ]

    view = _view(facts=facts)

    assert view["cost_silent_in_api_rub"] == 500_000.0
    assert view["rows"][0]["window_days"] == 28


def test_asking_the_api_carries_no_state_filter(monkeypatch):
    """Вопрос задаётся без фильтра состояний.

    С фильтром ответ «нет» означает «нет в этих состояниях», и Мастер снова
    сливается с кампанией, которую синк просто не искал: ровно так 12 обычных
    кампаний два месяца числились слепой зоной.
    """
    seen = {}

    def spy(url, login, payload, what, attempts=None):
        seen["criteria"] = payload["params"]["SelectionCriteria"]
        return {"Campaigns": []}

    from sync.agent import segments
    monkeypatch.setattr(segments, "_api_post", spy)
    segments.fetch_campaigns_by_ids(ACCOUNT, ["900"])

    assert seen["criteria"]["Ids"] == [900]
    assert set(seen["criteria"]["States"]) == set(segments.ALL_CAMPAIGN_STATES)


def test_facts_given_as_an_iterator_are_not_lost():
    """Факты обходятся трижды, и генератор со второго обхода пуст.

    Отказ был бы молчаливым и самым дорогим из возможных: слепая зона вышла бы
    нулевой при живых деньгах, то есть отчёт сказал бы «всё под контролем».
    """
    view = _view(facts=iter(_facts()))

    assert view["cost_silent_in_api_rub"] == 500_000.0
    assert [r["campaign_id"] for r in view["rows"]] == ["900"]


def test_empty_blind_zone_is_not_reported_as_unasked():
    """Спрашивать было не о чем — это не «никого не спросили».

    Оговорка про неопрошенные кабинеты нужна, чтобы молчание API не сошло за
    доказательство. Печатать её при пустой слепой зоне значит поднимать тревогу
    там, где всё в порядке.
    """
    view = _view(settings_rows={"100": {}, "200": {}, "900": {}}, logins=[])

    assert view["campaigns_outside"] == 0
    assert view["rows_suppressed"] is None


def test_report_section_keeps_numbers_and_drops_cards():
    """В отчёт прогона едут числа, а карточки — в реестр идей.

    Копия карточки в jsonb прогона назавтра разъедется с оригиналом (у идеи
    есть срок жизни, история и отказ человека, у копии нет), а в плохой день
    слепых кампаний 82, и полный список раздувает журнал ровно тогда, когда он
    нужнее всего.
    """
    section = master.report_section(_view())

    assert "rows" not in section
    assert section["proposals"] == 1
    assert section["cost_silent_in_api_rub"] == 500_000.0
    assert section["skipped_sample"] == []
