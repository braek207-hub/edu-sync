# -*- coding: utf-8 -*-
"""
sync/agent/ideas/bundles.py — связки и строки, на которых работают генераторы.

Пять генераторов Ф13 — чистые функции вида candidates(rows, ctx). До этого
модуля их никто не звал: rows не собирал никто, registry.upsert в бою не
вызывался ниоткуда, и весь путь идей выглядел работающим ровно до первого
вопроса «почему предложений нет». Здесь собирается вход, а зовёт генераторы
расчётный такт (agent_e0.collect_ideas).

**Ни одного нового числа.** Модуль ничего не оценивает сам: он берёт то, что
такт уже посчитал, и перекладывает в форму, которую читает генератор. Каждое
поле связки — с названным источником:

  * counts, expected_revenue, ступень воронки — ladder.ladder на пулах такта
    (направление, кабинет). Перевод «событий» в оплаты и выручку делает та же
    лестница, что решает по кампаниям, а не второй расчёт рядом;
  * cost, clicks, conversions сегмента — недельные строки среза Директа
    (slices.build_sliced_facts), из которых такт уже посчитал корректировки;
  * value_per_lead, cost_28d, eff_leads_28d, limit_binds — раскладка портфеля
    (portfolio.portfolio_targets), то есть те же числа, по которым такт
    двигает деньги;
  * current_modifier — витрина настроек кабинета (edu_campaign_settings).

**Окно у связки и у её базы одно.** Окупаемость сегмента считается на окне
СРЕЗА, и окупаемость кампании, с которой генератор её сравнивает, — на том же
окне и по тому же отчёту. Взять базу с окна лестницы (90 зрелых дней) значило
бы делить числа августа на числа мая: отношение romi/base_romi у proven.py
задаёт СИЛУ корректировки, и сезон внутри него выглядел бы как заслуга
сегмента. Коэффициенты пересчёта в оплаты у сегмента и у его кампании при
этом одни и те же, поэтому из отношения они сокращаются, а в сравнении с
порогом кабинета λ участвуют честно.

**Инвариант current_modifier — три значения, три разных факта.** Ключа нет:
витрина настроек по этой кампании не снята, состояние сегмента неизвестно.
None: витрина снята, корректировки в кабинете нет. Число: корректировка есть.
Подмена первого вторым отправит в кабинет bidmodifiers.add поверх
существующей корректировки — Директ отвергнет элемент, действие будет
переотправляться каждый прогон и съест потолок попыток. Поэтому ключ
проставляется ТОЛЬКО там, где витрина по кампании действительно есть.

**Чего здесь нет и почему.** Аудитории (ideas/audiences.py) входа не
получают: срез ретаргетинга расчётный такт не снимает вовсе, а device/gender/
age/region — чужая территория движка, которую тот генератор отбивает сам.
Придумать ему вход из того, что есть, значило бы кормить генератор срезами, о
которых он и так говорит «мимо». Дыра названа в SOURCES_WITHOUT_INPUT и едет
в отчёт прогона: «источника нет» и «источник есть, находок нет» — разные
новости, и по пустому счётчику их не различить.

**Отбраковка.** Молчаливого отсева здесь нет: строка без адреса возвращается
в skipped с причиной. Всё остальное — суждение генератора, и оно остаётся
ему: связка без окупаемости уезжает как есть и получает там свой названный
отказ. Два судьи на одно решение разъехались бы на первой правке одного.
"""

from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sync.agent import ladder as ladder_mod
from sync.agent import portfolio as portfolio_mod
from sync.agent.ideas import audiences as audiences_mod
from sync.agent.writer import plan as plan_mod

# Витрина настроек: где лежат корректировки кампании и какой уровень нас
# касается. Корректировка группы объявлений кампанийной не мешает — это
# разные объекты Директа, и add на уровне кампании она не отвергнет.
MODIFIERS_KEY = "bidModifiers"
MODIFIER_ITEMS_KEY = "items"
MODIFIER_LEVEL_CAMPAIGN = "CAMPAIGN"

# Окно портфеля: те же 28 дней, на которых посчитаны cost_28d и leads_28d.
PORTFOLIO_WINDOW_DAYS = int(portfolio_mod.WEEKS_IN_WINDOW * 7)

SOURCES_WITHOUT_INPUT = {
    audiences_mod.SOURCE: (
        "срез аудиторий расчётным тактом не снимается: отчёт ретаргетинга в "
        "Э0 не запрашивается, а device/gender/age/region генератор аудиторий "
        "отбивает сам как чужую территорию движка"),
}

REASON_NO_CAMPAIGN = (
    "строка без кампании: адресовать связку нечем — идея была бы про кабинет "
    "вообще")
REASON_UNKNOWN_CAMPAIGN = (
    "кампании нет среди посчитанных тактом: ни ступени воронки, ни кабинета — "
    "связка ссылалась бы на объект, о котором такт ничего не знает")
REASON_NO_SEGMENT_KEY = "у строки среза пустой ключ сегмента"
REASON_NO_PHRASE = "строка отчёта запросов без самого запроса"


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None   # NaN — то же «неизвестно»


def _text(value: Any) -> str:
    return str(value or "").strip()


def _days(window_from: str, window_to: str) -> Optional[int]:
    """Длина окна в днях, обе границы включительно."""
    try:
        start = date.fromisoformat(str(window_from)[:10])
        end = date.fromisoformat(str(window_to)[:10])
    except (TypeError, ValueError):
        return None
    days = (end - start).days + 1
    return days if days > 0 else None


# ------------------------------------------------- состояние корректировки


def modifier_state(settings: Any, direct_type: str,
                   key: str) -> Tuple[bool, Optional[float]]:
    """(известно ли состояние, значение корректировки).

    Первое значение — ровно то, что отличает «витрина не снята» от «в
    кабинете пусто». Витрины по кампании нет или в ней нет блока
    корректировок — известно False, и ключ current_modifier в связку не
    попадёт вовсе.

    Ключ сегмента сверяется только там, где он различает корректировки одного
    типа: у DEMOGRAPHICS_ADJUSTMENT в одной кампании живут и пол, и возраст, у
    REGIONAL_ADJUSTMENT — регионы. У устройств тип и есть ключ
    (MOBILE_ADJUSTMENT), и второй сверки не требует.
    """
    if not isinstance(settings, dict):
        return False, None
    block = settings.get(MODIFIERS_KEY)
    if not isinstance(block, dict):
        return False, None
    items = block.get(MODIFIER_ITEMS_KEY)
    if not isinstance(items, list):
        return False, None

    wanted = _text(key).upper()
    for item in items:
        if not isinstance(item, dict):
            continue
        if _text(item.get("type")).upper() != _text(direct_type).upper():
            continue
        level = _text(item.get("level")).upper()
        if level and level != MODIFIER_LEVEL_CAMPAIGN:
            continue
        detail = _text(item.get("detail")).upper()
        region = _text(item.get("regionId")).upper()
        if detail or region:
            if wanted not in (detail, region):
                continue
        percent = _number(item.get("percent"))
        if percent is not None:
            return True, percent
    return True, None


# ------------------------------------------------------ справочник кампаний


def campaign_index(
    facts: Sequence[Dict[str, Any]],
    ladder_section: Dict[str, Any],
    portfolio_section: Dict[str, Any],
    *,
    login_by_campaign: Dict[str, str],
    settings_by_campaign: Dict[str, Dict[str, Any]],
    direction_by_campaign: Dict[str, str],
) -> Dict[str, Dict[str, Any]]:
    """Кампания → всё, что о ней знает такт, в одной строке.

    Собирается один раз на прогон и раздаётся всем сборщикам: иначе каждый
    сборщик ходил бы за теми же числами своей дорогой, и первое же расхождение
    дорог означало бы, что два генератора судят об одной кампании по разным
    данным.
    """
    by_object = (ladder_section or {}).get("by_object") or {}
    pool_counts = (ladder_section or {}).get("counts") or {}
    by_direction = pool_counts.get("by_direction") or {}
    account_counts = pool_counts.get("account") or {}
    avg_check = (ladder_section or {}).get("avg_check") or {}

    moves: Dict[str, Dict[str, Any]] = {}
    for login, account in ((portfolio_section or {}).get("accounts") or {}).items():
        for campaign_id, move in (account.get("moves") or {}).items():
            moves[str(campaign_id)] = {**move, "account": login}

    index: Dict[str, Dict[str, Any]] = {}
    campaign_ids = set(by_object) | set(moves) | {
        str(f.get("campaign_id")) for f in (facts or ()) if f.get("campaign_id")}
    for campaign_id in sorted(campaign_ids):
        move = moves.get(campaign_id) or {}
        direction = (_text(move.get("direction"))
                     or _text(direction_by_campaign.get(campaign_id)))
        ladder_row = by_object.get(campaign_id) or {}
        entry: Dict[str, Any] = {
            "campaign_id": campaign_id,
            "account": (_text(move.get("account"))
                        or _text(login_by_campaign.get(campaign_id))),
            "direction": direction,
            "counts": dict(ladder_row.get("events_by_step") or {}),
            "pools": (
                (f"direction:{direction}", by_direction.get(direction) or {}),
                ("account", account_counts),
            ),
            "avg_check": _number(avg_check.get(campaign_id)),
            "settings": settings_by_campaign.get(campaign_id),
            "value_per_lead_rub": _number(move.get("value_per_lead")),
            "cost_28d": _number(move.get("cost_28d")),
            "eff_leads_28d": _number(move.get("leads_28d")),
        }
        # Связывает ли лимит расход — трёхзначно, как и корректировка: кампании
        # нет в раскладке (заповедник, кампания без ценности лида) — ключа нет
        # вовсе, и генератор теста откажет «состояние не снято», а не
        # предложит прибавку вслепую.
        if "limit_binding" in move:
            entry["limit_binds"] = bool(move["limit_binding"])
        index[campaign_id] = entry
    return index


def _revenue(counts: Dict[str, Any], entry: Dict[str, Any]) -> Optional[float]:
    """Ожидаемая выручка набора событий по лестнице кампании.

    Пулы и средний чек берутся у кампании: связка живёт внутри неё, и своих
    коэффициентов перехода у неё быть не может — событий на них не набралось
    бы никогда.
    """
    avg_check = entry.get("avg_check")
    if avg_check is None:
        return None
    result = ladder_mod.ladder(counts, entry.get("pools") or (),
                              avg_check=avg_check)
    return _number(result.get("expected_revenue"))


# ------------------------------------------------ связки сегментов (proven)


def segment_bundles(
    sliced_rows: Sequence[Dict[str, Any]],
    index: Dict[str, Dict[str, Any]],
    *,
    window_days: int,
) -> Dict[str, List[Dict[str, Any]]]:
    """Недельные строки срезов → связки «кампания × сегмент».

    Запроса в адресе нет: Директ не отдаёт срез в разрезе поисковых запросов,
    и приписать связке запрос было бы выдумкой. proven.py такую связку
    принимает — ключ query у неё просто отсутствует, а не пустой.
    """
    totals: Dict[Tuple[str, str, str], Dict[str, float]] = {}
    by_campaign: Dict[Tuple[str, str], Dict[str, float]] = {}
    skipped: List[Dict[str, Any]] = []
    for row in sliced_rows or ():
        if not isinstance(row, dict):
            continue
        campaign_id = _text(row.get("campaign_id"))
        kind = _text(row.get("slice_kind"))
        key = _text(row.get("slice_key"))
        if not campaign_id:
            skipped.append({"reason": REASON_NO_CAMPAIGN, "segment": kind})
            continue
        if campaign_id not in index:
            skipped.append({"campaign_id": campaign_id, "segment": kind,
                            "reason": REASON_UNKNOWN_CAMPAIGN})
            continue
        if not key:
            skipped.append({"campaign_id": campaign_id, "segment": kind,
                            "reason": REASON_NO_SEGMENT_KEY})
            continue
        slot = totals.setdefault((campaign_id, kind, key),
                                 {"clicks": 0.0, "leads": 0.0, "cost": 0.0})
        base = by_campaign.setdefault((campaign_id, kind),
                                      {"clicks": 0.0, "leads": 0.0, "cost": 0.0})
        for target in (slot, base):
            target["clicks"] += _number(row.get("clicks")) or 0.0
            target["leads"] += _number(row.get("conversions")) or 0.0
            target["cost"] += _number(row.get("cost")) or 0.0

    bundles: List[Dict[str, Any]] = []
    for (campaign_id, kind, key), counts in sorted(totals.items()):
        entry = index[campaign_id]
        base = by_campaign[(campaign_id, kind)]
        setting_kind = f"bid_modifier:{kind}"
        direct_type, canonical, _reason = plan_mod.direct_type_for(setting_kind, key)

        bundle: Dict[str, Any] = {
            "account": entry["account"],
            "campaign_id": campaign_id,
            "segment": {"kind": setting_kind, "key": key},
            "counts": {"clicks": counts["clicks"], "leads": counts["leads"]},
            "daily_cost_rub": (counts["cost"] / window_days
                               if window_days > 0 else None),
            "value_per_lead_rub": entry.get("value_per_lead_rub"),
        }
        if base["clicks"] > 0:
            bundle["segment_share"] = counts["clicks"] / base["clicks"]
        if counts["leads"] > 0:
            bundle["cpa_rub"] = counts["cost"] / counts["leads"]

        revenue = _revenue(bundle["counts"], entry)
        if revenue is not None and counts["cost"] > 0:
            bundle["romi"] = revenue / counts["cost"]
        base_revenue = _revenue(
            {"clicks": base["clicks"], "leads": base["leads"]}, entry)
        if base_revenue is not None and base["cost"] > 0:
            bundle["base_romi"] = base_revenue / base["cost"]

        # Состояние сегмента в кабинете — только когда витрина снята И вид
        # сегмента вообще переводится в тип корректировки: у непереводимого
        # (сеть, регион названием) спрашивать нечего, и отсутствие ключа
        # честнее выдуманного None. Отказ по такому виду вынесет генератор.
        if direct_type is not None:
            known, value = modifier_state(entry.get("settings"), direct_type,
                                          canonical)
            if known:
                bundle["current_modifier"] = value
        bundles.append(bundle)
    return {"bundles": bundles, "skipped": skipped}


# ------------------------------------------ доноры выноса (consolidate)


def query_donors(
    query_rows: Sequence[Dict[str, Any]],
    index: Dict[str, Dict[str, Any]],
    *,
    phrases: Sequence[str],
    window_days: int,
) -> Dict[str, List[Dict[str, Any]]]:
    """Строки отчёта запросов → доноры выноса в отдельную кампанию.

    Берутся не все запросы, а только названные phrases — те, что расчёт уже
    отобрал в расширение семантики (objects.expansion_candidates): окупаются,
    своей ключевой фразы не имеют, а значит ставкой и объявлением по ним никто
    не управляет. Второго отбора здесь нет: свой критерий «что достойно
    выноса» разъехался бы с тем, по которому расширение печатается в отчёте.

    Ожидаемые оплаты (p_pay_sum) считает лестница кампании-донора, а не
    множитель, придуманный здесь: конверсии запроса — это её ступень leads, и
    перевод их в оплаты уже описан коэффициентами перехода такта.
    """
    wanted = {_text(p).lower() for p in (phrases or ()) if _text(p)}
    rows: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for row in query_rows or ():
        if not isinstance(row, dict):
            continue
        phrase = _text(row.get("query"))
        if not phrase:
            skipped.append({"reason": REASON_NO_PHRASE})
            continue
        if wanted and phrase.lower() not in wanted:
            continue
        campaign_id = _text(row.get("campaign_id"))
        if not campaign_id:
            skipped.append({"phrase": phrase, "reason": REASON_NO_CAMPAIGN})
            continue
        entry = index.get(campaign_id)
        if entry is None:
            skipped.append({"phrase": phrase, "campaign_id": campaign_id,
                            "reason": REASON_UNKNOWN_CAMPAIGN})
            continue

        conversions = _number(row.get("conversions")) or 0.0
        cost = _number(row.get("cost")) or 0.0
        counts = {"clicks": _number(row.get("clicks")) or 0.0,
                  "leads": conversions}
        donor: Dict[str, Any] = {
            "account": entry["account"],
            "campaign_id": campaign_id,
            "direction": entry["direction"],
            "phrase": phrase,
            "cost_rub": cost,
            "conversions": conversions,
            "window_days": window_days,
            # Настройки кампании-донора: из них новая кампания берёт счётчик
            # и цель (launch.campaign_from_donors). Не «на всякий случай» —
            # без них наряд билдеру не собирается, и вынос остаётся
            # предложением человеку.
            "settings": entry.get("settings"),
        }
        result = ladder_mod.ladder(counts, entry.get("pools") or (),
                                   avg_check=entry.get("avg_check"))
        payments = _number(result.get("expected_payments"))
        if payments is not None:
            donor["p_pay_sum"] = payments
        revenue = _number(result.get("expected_revenue"))
        if revenue is not None and cost > 0:
            donor["romi"] = revenue / cost
        rows.append(donor)
    return {"rows": rows, "skipped": skipped}


# ------------------------------------------------- кампании тестов (abtest)


def campaign_tests(
    index: Dict[str, Dict[str, Any]],
    *,
    holdout_ids: Sequence[str],
    learning_reset: Dict[str, Any],
    today: date,
) -> List[Dict[str, Any]]:
    """Справочник кампаний → строки для генератора A/B-тестов.

    Дней с последнего сброса обучения считается по журналу применённых
    действий (writer/db.last_learning_reset), а не по календарю кампании: сброс
    устраивает изменение, а не время. Кампании, которую агент не трогал, ключ
    не проставляется вовсе — иначе «никогда не трогали» читалось бы как
    «сбросили сегодня» или наоборот, и обе подмены двигают срок теста.
    """
    guarded = {str(h) for h in (holdout_ids or ())}
    rows: List[Dict[str, Any]] = []
    for campaign_id, entry in sorted(index.items()):
        row: Dict[str, Any] = {
            "account": entry["account"],
            "campaign_id": campaign_id,
            "direction": entry["direction"],
            "eff_leads": entry.get("eff_leads_28d"),
            "cost_rub": entry.get("cost_28d"),
            "window_days": PORTFOLIO_WINDOW_DAYS,
            "in_holdout": campaign_id in guarded,
        }
        if "limit_binds" in entry:
            row["limit_binds"] = entry["limit_binds"]
        last = (learning_reset or {}).get(campaign_id)
        if last is not None:
            reset_day = last if isinstance(last, date) else None
            if reset_day is None:
                try:
                    reset_day = date.fromisoformat(str(last)[:10])
                except (TypeError, ValueError):
                    reset_day = None
            if reset_day is not None:
                row["days_since_learning_reset"] = (today - reset_day).days
        rows.append(row)
    return rows


# ---------------------------------------------------- поводы рынка (market)


def demand_rows(
    regimes: Dict[str, Dict[str, Any]],
    *,
    account: str,
    uncovered_by_direction: Dict[str, Sequence[str]],
    cpl_by_direction: Dict[str, float],
    live_directions: Sequence[str],
) -> List[Dict[str, Any]]:
    """Режимы спроса → поводы завести семантику.

    «Покрываем» выводится из двух фактов такта, а не объявляется: направление
    живо (кампании этого направления тратили в окне) И расширение семантики не
    нашло по нему ни одной окупающейся фразы без своей ключевой. Тогда растущий
    спрос кабинет уже забирает, и повод — перелить бюджет, а не строить
    кампанию. Пустое расширение у направления, которого в кабинете нет вовсе,
    покрытием не считается: там нечему покрывать.
    """
    live = {_text(d) for d in (live_directions or ()) if _text(d)}
    rows: List[Dict[str, Any]] = []
    for direction in sorted(regimes or {}):
        regime = regimes[direction] or {}
        phrases = [_text(p) for p in (uncovered_by_direction.get(direction) or ())
                   if _text(p)]
        rows.append({
            "kind": "demand",
            "account": account,
            "direction": direction,
            "regime": regime.get("regime"),
            "sigma": regime.get("sigma"),
            "frequency": regime.get("frequency"),
            "baseline_median": regime.get("baseline_median"),
            "last_week": regime.get("last_week"),
            "covered": bool(direction in live and not phrases),
            "uncovered_phrases": phrases,
            "direction_cpl_rub": _number((cpl_by_direction or {}).get(direction)),
        })
    return rows
