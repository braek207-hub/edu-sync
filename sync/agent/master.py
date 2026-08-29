# -*- coding: utf-8 -*-
"""
sync/agent/master.py — Мастер кампаний в контуре агента.

**Что за зона.** Витрину настроек (`edu_campaign_settings`) наполняет
`sync/edu_direct_settings.py` из `campaigns.get`. Кампании «Мастер кампаний»
этот метод не отдаёт вовсе, поэтому в витрину они не попадают, а всё, что
считает агент — недобор трафика, корректировки, кривые насыщения, — считается
по популяции без них. Замер 29.08.2026 на проде (окно решений
2026-07-31…2026-08-28): расход кабинетов 24 829 222 ₽, мимо витрины
2 390 391 ₽ — 9,63 % при 91 кампании и пяти «слепых» строках. Витрина в тот же
день свежая (синк 28.08 16:58, 136 строк), и все её строки —
`TEXT_CAMPAIGN`; ни одной с «МК» в имени там нет.

**Регресса не было — уехало окно.** План пропускной способности называет рост
6,2 % → 10,0 % регрессом после починки синка 25.08. Числа прогонов говорят
другое: 6,18 % снято тактом расчёта на окне 2026-05-20…06-16, а 10,19 % — на
окне 2026-07-28…08-24, и это одно и то же множество из трёх кампаний Мастера.
За два месяца их расход вырос с 1 098 164 ₽ до 2 437 394 ₽ (+122 %) при почти
неизменном расходе кабинетов. Доля выросла потому, что выросли САМИ КАМПАНИИ,
а не потому, что синк стал хуже видеть. Лечится это не починкой синка.

**Слепа только настройка, не статистика.** Reports API про Мастер кампаний
знает всё: расход, клики, конверсии и даже поисковые запросы (замер 29.08.2026:
5 010–5 042 строки в `edu_agent_search_queries` на каждую из трёх кампаний).
Поэтому «в контур» здесь означает не «добыть данные», а «перестать выкидывать
кампанию из картины кабинета за то, что у неё нет карточки настроек».

**Почему не рычаг, а рекомендация.** Записи в Мастер кампаний нет ни у API v5,
ни у агента, и появиться ей неоткуда. Всё, что найдено, уезжает полосой 7
(`lanes.LANE_PROPOSAL`) и классом 3 (`tier.TIER_PROPOSAL`) — человеку на экран,
через реестр идей. Генератор — `sync/agent/ideas/master.py`; здесь только
чтение и сборка входа для него.

**Два непохожих случая под одной вывеской.** Кампания вне витрины — это либо
Мастер кампаний (API молчит, чинит человек), либо обычная кампания, не
доехавшая до витрины (API отдаёт, чинит код). 25.08.2026 всю зону списывали на
Мастер, а замер по явным Id показал 12 обычных TEXT_CAMPAIGN из 15. Поэтому
модуль СПРАШИВАЕТ API про каждый слепой Id тем же клиентом, которым агент
читает кампании (`segments.fetch_campaigns_by_ids`), и разводит случаи по
ответу — вместо того чтобы делить их по «МК» в имени кампании. Имя не признак:
переименование кампании молча меняло бы вывод о поломке синка.

**Адрес кампании.** Кабинет берётся из справочника «кампания → логин», и
справочник у него ДВА источника. Первый, `campaigns.get`, о Мастере молчит —
на нём одном зона и стояла. Второй — отчёт поисковых запросов Reports API:
он запрашивается по кабинетам поимённо, Мастера отдаёт наравне со всеми, и
раз отчёт кабинета вернул кампанию, она этого кабинета. Замер 29.08.2026: на
одном лишь `campaigns.get` все пять кампаний Мастера (1 678 168 ₽ расхода)
отказывались адресоваться и уходили из идей молча.

Не разрешилось справочником — кабинет выводится из проекта кампании, и только
если проект однозначно сводится к одному логину на видимых кампаниях.
Неоднозначный проект — отказ с причиной, а не догадка: идея с чужим кабинетом
в адресе уедет в чужую очередь и посчитается по чужому порогу.
"""

from datetime import date
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from sync.agent.coverage import SAMPLE_LIMIT, known_campaign_ids

# Вид объекта в адресе идеи. Тот же литерал разбирает генератор
# (ideas/master.py) и, через subject, экран человека.
MASTER_KIND = "master_campaign"

REASON_NOT_A_CAMPAIGN = (
    "идентификатор кампании не число: в фактах живут строки шаблонов разметки "
    "({campaignid}, {campaign_id}) — замер 29.08.2026 нашёл две таких на нуле "
    "расхода. Спрашивать API про них нечем, и в счётчике слепой зоны они "
    "раздувают число кампаний, не добавляя ни рубля")
REASON_NO_PROJECT = (
    "у кампании нет проекта в фактах: кабинет вывести не из чего, а идея без "
    "кабинета уедет в чужую очередь")
REASON_AMBIGUOUS_PROJECT = (
    "проект кампании встречается сразу в нескольких кабинетах: адрес "
    "неоднозначен, и выбирать за человека здесь нечем")


def _is_campaign_id(value: Any) -> bool:
    text = str(value or "")
    return bool(text) and text.isdigit()


def _number(value: Any) -> float:
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return number if number == number else 0.0


def _window_days(window_from: str, window_to: str) -> int:
    return (date.fromisoformat(window_to) - date.fromisoformat(window_from)).days + 1


def outside_settings(cost_by_campaign: Mapping[str, float],
                     settings_rows: Any) -> Dict[str, float]:
    """Кампании с расходом, которых нет в витрине настроек.

    Знание «кто в витрине» берётся у счётчика слепой зоны
    (`coverage.known_campaign_ids`), а не переписывается здесь: две копии
    одного правила однажды напечатали бы в отчётах две разные слепые зоны под
    одним именем — ровно тот дефект, из-за которого такт расчёта и такт записи
    в один день показывали 6,18 % и 9,45 %.
    """
    known = known_campaign_ids(settings_rows)
    return {str(cid): _number(cost)
            for cid, cost in (cost_by_campaign or {}).items()
            if str(cid) not in known and _number(cost) > 0}


def probe_accounts(campaign_ids: Sequence[str], logins: Sequence[str],
                   fetch: Optional[Callable[[str, List[str]], Dict[str, Dict[str, Any]]]] = None,
                   ) -> Dict[str, Any]:
    """Что API Директа знает про эти кампании: {"found": {...}, "errors": {...}}.

    Обход по логинам обязателен: Id кампании принадлежит одному кабинету, а
    заголовок `Client-Login` у запроса свой у каждого — чужой кабинет отвечает
    про неё тем же молчанием, что и отсутствующая кампания.

    Отказ одного кабинета не отменяет ответов остальных, но и не притворяется
    ответом: логин уезжает в `errors`, и вызывающий обязан читать «API про эту
    кампанию промолчал» только там, где спрошены ВСЕ кабинеты. Иначе упавший
    токен одного кабинета выглядел бы как открытие «нашли ещё один Мастер
    кампаний».
    """
    if fetch is None:
        from sync.agent import segments
        fetch = segments.fetch_campaigns_by_ids

    ids = [cid for cid in campaign_ids or () if _is_campaign_id(cid)]
    found: Dict[str, Dict[str, Any]] = {}
    errors: Dict[str, str] = {}
    if not ids:
        return {"found": found, "errors": errors, "asked": []}

    asked: List[str] = []
    for login in logins or ():
        try:
            answer = fetch(login, ids)
        except Exception as exc:  # noqa: BLE001
            errors[str(login)] = f"{type(exc).__name__}: {exc}"[:300]
            continue
        asked.append(str(login))
        for campaign_id, info in (answer or {}).items():
            found[str(campaign_id)] = dict(info)
    return {"found": found, "errors": errors, "asked": asked}


def account_by_project(facts: Iterable[Dict[str, Any]],
                       login_by_campaign: Mapping[str, str]) -> Dict[str, str]:
    """Проект → кабинет, выведенное из ВИДИМЫХ кампаний.

    Проект в фактах определяется по имени кампании (`sync/classify.py`), а
    кабинет — по тому, чей `campaigns.get` кампанию отдал. У видимых кампаний
    известны оба, и связь читается прямо из данных. Проект, попавший больше
    чем в один кабинет, из карты выбрасывается: догадка тут стоила бы идеи,
    посчитанной по порогу чужого кабинета.
    """
    logins_by_project: Dict[str, set] = {}
    for fact in facts or ():
        project = str(fact.get("project") or "").strip()
        login = (login_by_campaign or {}).get(str(fact.get("campaign_id")))
        if not project or not login:
            continue
        logins_by_project.setdefault(project, set()).add(str(login))
    return {project: sorted(logins)[0]
            for project, logins in logins_by_project.items() if len(logins) == 1}


def _totals(facts: Iterable[Dict[str, Any]],
            window_from: str, window_to: str) -> Dict[str, Dict[str, Any]]:
    """Суммы кампаний за окно. Сумма, а не средний темп × дни.

    Разница не косметическая: кампания, отработавшая пять дней из двадцати
    восьми, входит в средний темп своим темпом, растянутым на месяц. Подмена
    уже стоила двух разных слепых долей под одним именем в один день (замер
    25.08.2026: 9,45 % против 6,18 %).
    """
    out: Dict[str, Dict[str, Any]] = {}
    for fact in facts or ():
        day = str(fact.get("fact_date"))[:10]
        if day < window_from or day > window_to:
            continue
        campaign_id = str(fact.get("campaign_id"))
        slot = out.setdefault(campaign_id, {
            "campaign_id": campaign_id, "campaign_name": "", "project": "",
            "direction": "", "cost_rub": 0.0, "clicks": 0.0, "impressions": 0.0,
            "leads": 0.0, "eff_leads": 0.0, "payments": 0.0, "revenue_rub": 0.0,
        })
        for key, field in (("cost_rub", "cost"), ("clicks", "clicks"),
                           ("impressions", "impressions"), ("leads", "leads"),
                           ("eff_leads", "eff_leads"),
                           ("payments", "payments_fact"),
                           ("revenue_rub", "revenue")):
            slot[key] += _number(fact.get(field))
        for key, field in (("campaign_name", "campaign_name"),
                           ("project", "project"), ("direction", "direction")):
            if fact.get(field):
                slot[key] = str(fact[field])
    return out


def rows(*, facts: Iterable[Dict[str, Any]], settings_rows: Any,
         login_by_campaign: Mapping[str, str],
         api_found: Mapping[str, Dict[str, Any]],
         window_from: str, window_to: str) -> Dict[str, Any]:
    """Карточки кампаний вне витрины настроек — вход генератора идей.

    Чистая функция: сеть уже отработала выше (`probe_accounts`), и всё, что
    здесь происходит, воспроизводится тестом на словарях.

    Цена эффективного лида кабинета считается по ВИДИМЫМ кампаниям, без самой
    слепой: при 10 % расхода кабинета кампания заметно тянет на себя число, с
    которым её же и сравнивают, и сравнение выходит с самой собой.
    """
    # Факты обходятся трижды (суммы, карта проектов, ещё раз суммы в view).
    # Генератор на входе отдал бы пустоту со второго обхода — молча, и слепая
    # зона вышла бы нулевой при живых деньгах.
    facts = list(facts or ())
    totals = _totals(facts, window_from, window_to)
    outside = outside_settings({cid: row["cost_rub"] for cid, row in totals.items()},
                               settings_rows)
    project_to_login = account_by_project(facts, login_by_campaign)
    window_days = _window_days(window_from, window_to)

    account_cost: Dict[str, float] = {}
    known_cost: Dict[str, float] = {}
    known_eff: Dict[str, float] = {}
    for campaign_id, row in totals.items():
        login = (login_by_campaign or {}).get(campaign_id)
        if not login:
            continue
        account_cost[login] = account_cost.get(login, 0.0) + row["cost_rub"]
        known_cost[login] = known_cost.get(login, 0.0) + row["cost_rub"]
        known_eff[login] = known_eff.get(login, 0.0) + row["eff_leads"]

    resolved: Dict[str, str] = {}
    skipped: List[Dict[str, Any]] = []
    for campaign_id in sorted(outside):
        row = totals[campaign_id]
        if not _is_campaign_id(campaign_id):
            skipped.append({"campaign_id": campaign_id,
                            "campaign_name": row["campaign_name"],
                            "cost_rub": round(row["cost_rub"], 2),
                            "reason": REASON_NOT_A_CAMPAIGN})
            continue
        login = ((api_found or {}).get(campaign_id) or {}).get("login")
        # Справочник кабинетов — до проекта: он собран из ответов самих
        # кабинетов (campaigns.get плюс отчёт запросов Reports API), и это
        # факт. Проект — вывод по совпадению, и годится только там, где
        # фактов не осталось.
        if not login:
            login = (login_by_campaign or {}).get(campaign_id)
        if not login:
            project = row["project"]
            if not project:
                skipped.append({"campaign_id": campaign_id,
                                "campaign_name": row["campaign_name"],
                                "cost_rub": round(row["cost_rub"], 2),
                                "reason": REASON_NO_PROJECT})
                continue
            login = project_to_login.get(project)
            if not login:
                skipped.append({"campaign_id": campaign_id,
                                "campaign_name": row["campaign_name"],
                                "cost_rub": round(row["cost_rub"], 2),
                                "reason": REASON_AMBIGUOUS_PROJECT})
                continue
        resolved[campaign_id] = str(login)
        account_cost[str(login)] = account_cost.get(str(login), 0.0) + row["cost_rub"]

    out: List[Dict[str, Any]] = []
    for campaign_id, login in sorted(resolved.items()):
        row = totals[campaign_id]
        eff = known_eff.get(login, 0.0)
        base_cpl = (known_cost.get(login, 0.0) / eff) if eff > 0 else None
        total = account_cost.get(login, 0.0)
        out.append({
            "account": login,
            "campaign_id": campaign_id,
            "campaign_name": row["campaign_name"],
            "project": row["project"],
            "direction": row["direction"],
            "cost_rub": round(row["cost_rub"], 2),
            "clicks": row["clicks"],
            "impressions": row["impressions"],
            "leads": row["leads"],
            "eff_leads": row["eff_leads"],
            "payments": row["payments"],
            "revenue_rub": round(row["revenue_rub"], 2),
            "window_days": window_days,
            "account_cost_rub": round(total, 2),
            "share_of_account": (round(row["cost_rub"] / total, 6)
                                 if total > 0 else None),
            "base_cpl_rub": (round(base_cpl, 2) if base_cpl else None),
            # Ответ API — целиком, а не признаком «нашли/не нашли»: тип
            # кампании и её состояние это и есть диагноз, с которым человек
            # идёт в кабинет.
            "api": ((api_found or {}).get(campaign_id) or None),
        })
    return {"rows": out, "skipped": skipped,
            "window": [window_from, window_to]}


def view(*, facts: Iterable[Dict[str, Any]], settings_rows: Any,
         login_by_campaign: Mapping[str, str], logins: Sequence[str],
         window_from: str, window_to: str,
         fetch: Optional[Callable[[str, List[str]], Dict[str, Dict[str, Any]]]] = None,
         ) -> Dict[str, Any]:
    """Полный такт: спросить API про слепые Id и собрать карточки.

    Секция отчёта прогона, а не просто вход генератора. Разделение
    «API отдаёт / API молчит» печатается числами: первое — счёт дефектов
    синка, который обязан идти к нулю, второе — размер настоящей зоны Мастера
    кампаний, который к нулю не пойдёт никогда и лечится человеком.
    """
    facts = list(facts or ())
    totals = _totals(facts, window_from, window_to)
    outside = outside_settings({cid: row["cost_rub"] for cid, row in totals.items()},
                               settings_rows)
    probe = probe_accounts(sorted(outside), logins, fetch=fetch)
    built = rows(facts=facts, settings_rows=settings_rows,
                 login_by_campaign=login_by_campaign,
                 api_found=probe["found"],
                 window_from=window_from, window_to=window_to)

    visible = {cid: cost for cid, cost in outside.items() if cid in probe["found"]}
    silent = {cid: cost for cid, cost in outside.items() if cid not in probe["found"]}

    # Молчание — доказательство только тогда, когда СПРОСИЛИ. Ни одного
    # опрошенного кабинета (пустой список логинов, упавший токен, прогон с
    # --skip-direct) — и каждая кампания вне витрины выглядит Мастером, включая
    # те, что API отдаёт: генератор наштамповал бы человеку рекомендаций про
    # дефект синка, который чинится кодом. Поэтому карточки в этом случае
    # наружу не идут, а причина печатается.
    unasked = None if (probe["asked"] or not outside) else (
        "ни один кабинет не опрошен: молчание API про кампанию неотличимо от "
        "нежелания спрашивать, и Мастера от недоехавшей кампании здесь не "
        "отличить")
    return {
        "window": [window_from, window_to],
        "campaigns_outside": len(outside),
        "cost_outside_rub": round(sum(outside.values()), 2),
        # Кампании, которые API ОТДАЁТ: это не Мастер кампаний, а недоезд до
        # витрины. Число здесь больше нуля означает открытый дефект синка.
        "visible_in_api": len(visible),
        "cost_visible_in_api_rub": round(sum(visible.values()), 2),
        # Кампании, про которые API молчит при опрошенных кабинетах, — та
        # самая зона Мастера кампаний.
        "silent_in_api": len(silent),
        "cost_silent_in_api_rub": round(sum(silent.values()), 2),
        "accounts_asked": probe["asked"],
        "api_errors": probe["errors"],
        "rows": ([] if unasked else built["rows"]),
        "rows_suppressed": unasked,
        "skipped": built["skipped"],
    }


def report_section(view_result: Dict[str, Any]) -> Dict[str, Any]:
    """Секция отчёта прогона из результата view(): числа и образец, без карточек.

    Карточки в отчёт не едут намеренно. Их место — реестр идей: там у находки
    есть срок жизни, история и отказ человека, а в отчёте она стала бы копией,
    которая назавтра разъедется с оригиналом. Плюс размер: слепая зона — это
    3 кампании в хороший день и 82 в плохой (замер 25.08.2026 на окне
    лестницы), и полный список в jsonb каждого прогона раздувает журнал ровно
    тогда, когда он нужнее всего.

    Образец отбраковок обрезан тем же пределом, что образец слепых кампаний у
    счётчика зоны (`coverage.SAMPLE_LIMIT`): два разных предела на две половины
    одного отчёта разъехались бы на первой правке одного из них.
    """
    out = {key: value for key, value in (view_result or {}).items()
           if key not in ("rows", "skipped")}
    out["proposals"] = len((view_result or {}).get("rows") or [])
    out["skipped_sample"] = ((view_result or {}).get("skipped") or [])[:SAMPLE_LIMIT]
    return out

