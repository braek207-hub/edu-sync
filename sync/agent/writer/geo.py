# -*- coding: utf-8 -*-
"""
sync/agent/writer/geo.py — Ф15 (запись): рычаг географии показов.

География — не настройка сбоку: она решает, в какие аукционы кампания входит
вообще. Поэтому ОДИН вид действия несёт ДВА разных утверждения, и цена
уверенности у них тоже разная.

  СУЖЕНИЕ — утверждение о ПРОШЛОМ. Регион откручивал деньги и за зрелое окно
  не дал ни одной конверсии при расходе выше трёх её цен. Убрать его — та же
  арифметика, что минус-фраза и запрет площадки: класс 0, риском не платит
  (writer/tier.py). Основание считает общий вход отсечения
  (negatives.cut_evidence) — тот же, которым судятся оба рычага-близнеца:
  разъехавшись, три одинаковых по смыслу правила судили бы один и тот же
  мёртвый трафик по-разному в зависимости от того, где он показался.

  РАСШИРЕНИЕ — СТАВКА. Истории по новому региону у ЭТОЙ кампании нет по
  определению: она там не показывалась ни дня. Число, которым посчитано
  обещание, снято с другого объекта — ровно это делает действие ставкой, а не
  измерением (класс 2, как у смены цели и смены стратегии).

Класс считает не слово в названии вида, а СОДЕРЖИМОЕ действия: writer/tier
сравнивает список регионов запроса с прочитанным из кабинета и признаёт
сужением только строгое подмножество. Иначе классом управлял бы построитель —
достаточно было бы назвать ход «сужением», чтобы освободить его от риска.

ФОРМА ЗАПРОСА НЕ ВЫДУМЫВАЕТСЯ. Регионы живут в ГРУППАХ, а не в кампании:
RegionIds — поле adgroups (читатель кабинета sync/edu_direct_settings.py,
_fetch_adgroups_by_campaign; там же его читают sync/agent/segments.py и
sync/lime_direct.py). У campaigns такого поля нет ни в чтении, ни в записи —
базовый набор полей кампании (edu_direct_settings, base_fields) региона не
содержит вовсе. Поэтому запрос уходит в adgroups.update, а не в
campaigns.update.

АДРЕСАТ ВСЁ РАВНО КАМПАНИЯ. Действие адресовано кампании (object_level =
campaign), а не отдельной группе: цена риска, кулдаун обучения и потолок
объекта считаются по кампании, и география в кабинете задаётся человеком тоже
на кампанию — Директ лишь разносит её по группам. Отсюда обязательная
проверка: если группы кампании нацелены на РАЗНУЮ географию, единый список
стёр бы ручное разделение, и рычаг отказывается.

СПИСОК ЗАМЕНЯЕТСЯ ЦЕЛИКОМ, как у минус-фраз и площадок: в теле запроса едет
полный новый список, собранный из ПРОЧИТАННОГО состояния, а не дельта.

МИНУС-РЕГИОН. Отрицательный id в списке означает вычитание региона из
покрытия — «Россия минус Москва» (sync/lime_direct._format_regions_display).
Покрытие такого списка по самим спискам не вычисляется: чтобы понять, сужает
ли ход, нужно дерево регионов, которого у рычага нет. Отказ, а не догадка —
иначе расширение поехало бы под классом сужения.

СМЕШАННЫЙ ХОД ЗАПРЕЩЁН. Одно действие либо только убирает регионы, либо
только добавляет. Смешанный ход — два утверждения разных классов в одной
строке: замер не смог бы сказать, какая из сторон сдвинула число, а цена
считалась бы по одной из них.

КАННИБАЛИЗАЦИЯ. Регион, который уже ведёт другая кампания кабинета, не
добавляется: две кампании в одном аукционе торгуются друг с другом, поднимая
цену клика себе же. Ход отвергается ЦЕЛИКОМ, а не подчищается по региону:
желаемый список посчитал кто-то, кто о покрытии не знал, и пересобрать его
здесь значило бы принять решение вместо него — отказ с названным регионом и
номером занявшей его кампании вернёт задачу туда, где список считается.
"""

import hashlib
from typing import Any, Dict, List, Optional, Set, Tuple

from sync.agent.objects import CANDIDATE_WINDOW_DAYS
from sync.agent.writer import exposure, expectation
from sync.agent.writer.negatives import cut_evidence

GEO_KIND = "geo.set"

# Доля прежнего списка, больше которой за одно действие не убирается. Рельса
# ловит не политику, а обвал: сужение больше половины прежней географии — это
# не правка настройки, а другая кампания, и такое решение принимает человек.
# То же число независимо считает рельса guardrails._check_geo.
MAX_REMOVED_SHARE = 0.5

NOT_TEXT_REASON = "не текстовая кампания: структура групп другая"
NO_GROUPS_REASON = (
    "у кампании не прочитано ни одной группы: регионы живут в группах, и "
    "писать их некуда"
)
SPLIT_GROUPS_REASON = (
    "группы кампании нацелены на разную географию ({lists} разных списка): "
    "единый список стёр бы ручное разделение, которое сделал человек"
)
NO_CURRENT_REASON = (
    "текущая география кампании неизвестна: без прочитанного списка не "
    "показать ни направления хода, ни куда возвращать откат"
)
EMPTY_REASON = (
    "новый список регионов пуст: кампания без географии не показывается "
    "вовсе — это остановка трафика, а не правка настройки"
)
NEGATIVE_REGION_REASON = (
    "номер региона {region} не положителен: минус-регион вычитает регион из "
    "покрытия, и сужает ли ход — по спискам не вычислить без дерева регионов"
)
MIXED_REASON = (
    "ход и убирает регионы ({removed}), и добавляет ({added}): это два "
    "утверждения разных классов в одной строке, и замер не сказал бы, какая "
    "сторона сдвинула число"
)
TOO_MUCH_CUT_REASON = (
    "за одно действие убирается {removed} региона(ов) из {total} — больше "
    "доли {share:.0%}: это не правка географии, а другая кампания"
)
CANNIBALISATION_REASON = (
    "регион {region} уже ведёт кампания {campaign}: две кампании в одном "
    "аукционе торгуются друг с другом и поднимают цену клика себе же"
)


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None   # NaN — то же «неизвестно»


def normalize_regions(value: Any) -> Optional[List[int]]:
    """Список регионов в каноническом виде: целые, без дублей, отсортированы.

    None — список нечитаем (не список, не числа). Пустой список читается как
    пустой, а не как «неизвестно»: у отсутствия географии и у нечитаемого
    поля разная судьба, и смешивать их значило бы отвечать неверной причиной.
    """
    if not isinstance(value, (list, tuple, set, frozenset)):
        return None
    out: Set[int] = set()
    for item in value:
        number = _number(item)
        if number is None or number != int(number):
            return None
        out.add(int(number))
    return sorted(out)


def _region_lists(state: Dict[str, Any]) -> List[Tuple[int, ...]]:
    """Различные списки регионов групп кампании. Пусто — читать нечего."""
    seen: List[Tuple[int, ...]] = []
    for group in (state or {}).get("adgroups") or []:
        regions = normalize_regions((group or {}).get("region_ids"))
        if not regions:
            continue
        key = tuple(regions)
        if key not in seen:
            seen.append(key)
    return seen


def adgroup_ids(state: Dict[str, Any]) -> List[int]:
    """Номера групп кампании, в которые уйдёт новый список."""
    out: List[int] = []
    for group in (state or {}).get("adgroups") or []:
        number = _number((group or {}).get("id"))
        if number is not None:
            out.append(int(number))
    return sorted(set(out))


def _occupied_by(region: int, coverage: Dict[Any, Any],
                 campaign_id: str) -> Optional[str]:
    """Чужая кампания, уже ведущая этот регион, или None.

    Ключ ищется и числом, и строкой: карта покрытия приходит из
    JSON-подобных источников, где целые ключи становятся строками.
    """
    owners = coverage.get(region)
    if owners is None:
        owners = coverage.get(str(region))
    for owner in owners or []:
        if str(owner) != str(campaign_id):
            return str(owner)
    return None


def _idempotency_key(campaign_id: str, regions: List[int]) -> str:
    # Порядок регионов в ключ не входит: список «Москва, Питер» и «Питер,
    # Москва» — одно и то же состояние кабинета, и разные ключи означали бы
    # второе применение того же изменения.
    raw = "geo:" + str(campaign_id) + ":" + ",".join(str(r) for r in sorted(regions))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _refusal(campaign_id: str, reason: str) -> Dict[str, Any]:
    return {"campaign_id": campaign_id, "reason": reason}


def diff_geo(
    desired: Dict[str, Dict[str, Any]],
    actual_by_campaign: Dict[str, Dict[str, Any]],
    coverage_by_region: Optional[Dict[Any, Any]] = None,
    window_days: int = CANDIDATE_WINDOW_DAYS,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Желаемая география × прочитанное состояние кабинета → (действия, отказы).

    desired — по кампании: region_ids (полный желаемый список) и числа, без
    которых ход не судится:
      * сужение — cut_cost (расход убираемых регионов за окно), cut_conversions
        (их конверсии; None означает «не измеряли» и класса 0 не даёт),
        baseline_cpa;
      * расширение — added_daily_rub (сколько денег возьмёт новый регион,
        оценка снята с другого объекта) и cpa_rub (цена лида кампании).

    actual_by_campaign — прочитанное состояние: тип кампании и её группы с их
    RegionIds. Кампании без записи не порождают ни действия, ни отказа — их не
    оказалось в кабинете, и это видно счётчиком not_found вызывающего.

    coverage_by_region — карта покрытия кабинета: регион → кампании, которые
    его уже ведут. Без неё каннибализация не проверяется вовсе, и это не
    послабление, а отсутствие данных: выдумывать покрытие рычаг не вправе.
    """
    coverage = coverage_by_region or {}
    actions: List[Dict[str, Any]] = []
    refused: List[Dict[str, Any]] = []

    for cid in sorted(desired):
        move = desired[cid]
        state = actual_by_campaign.get(str(cid))
        if state is None:
            continue
        if state.get("campaign_type") not in (None, "TEXT_CAMPAIGN"):
            refused.append(_refusal(cid, NOT_TEXT_REASON))
            continue

        groups = adgroup_ids(state)
        if not groups:
            refused.append(_refusal(cid, NO_GROUPS_REASON))
            continue

        lists = _region_lists(state)
        if not lists:
            refused.append(_refusal(cid, NO_CURRENT_REASON))
            continue
        if len(lists) > 1:
            refused.append(_refusal(cid, SPLIT_GROUPS_REASON.format(
                lists=len(lists))))
            continue
        current = list(lists[0])

        wanted = normalize_regions(move.get("region_ids"))
        if wanted is None:
            refused.append(_refusal(cid, NO_CURRENT_REASON))
            continue
        if not wanted:
            refused.append(_refusal(cid, EMPTY_REASON))
            continue

        negative = next((r for r in wanted + current if r <= 0), None)
        if negative is not None:
            refused.append(_refusal(cid, NEGATIVE_REGION_REASON.format(
                region=negative)))
            continue

        added = [r for r in wanted if r not in current]
        removed = [r for r in current if r not in wanted]
        if not added and not removed:
            continue
        if added and removed:
            refused.append(_refusal(cid, MIXED_REASON.format(
                removed=len(removed), added=len(added))))
            continue

        if removed and len(removed) > MAX_REMOVED_SHARE * len(current):
            refused.append(_refusal(cid, TOO_MUCH_CUT_REASON.format(
                removed=len(removed), total=len(current),
                share=MAX_REMOVED_SHARE)))
            continue

        owner = None
        for region in added:
            owner = _occupied_by(region, coverage, str(cid))
            if owner is not None:
                refused.append(_refusal(cid, CANNIBALISATION_REASON.format(
                    region=region, campaign=owner)))
                break
        if owner is not None:
            continue

        actions.append(_action(str(cid), move, groups, wanted, current,
                               added, removed, window_days))
    return actions, refused


def _action(campaign_id: str, move: Dict[str, Any], groups: List[int],
            wanted: List[int], current: List[int], added: List[int],
            removed: List[int], window_days: int) -> Dict[str, Any]:
    """Действие рычага: тело запроса, прошлое состояние и обещание.

    Экспозиция у двух направлений разная, и это не оговорка:

      * сужение ставит под удар ровно вырезаемый поток — столько же, сколько
        запрет площадки (exposure.traffic_cut_exposure). Тот же вход, что у
        рычагов-близнецов; сменится он на цену по знаку (задача 8 плана
        беты) — сменится сразу у всех трёх;
      * расширение — весь объект. Лимит кампании рычаг не двигает, значит
        новый регион берёт деньги из ТОГО ЖЕ лимита, перекладывая расход на
        географию, которой у кампании нет в истории. Какую долю он возьмёт,
        неизвестно, а неизвестная доля означает «под ударом весь объект»
        (exposure.py), а не ноль.
    """
    cut_window = _number(move.get("cut_cost")) or 0.0
    cut_daily = cut_window / max(1, int(window_days))
    lost_window = move.get("cut_conversions")

    if removed:
        own_exposure = exposure.traffic_cut_exposure(
            cut_daily, f"сужение гео ({len(removed)})")
        evidence = cut_evidence(cut_window, lost_window,
                               move.get("baseline_cpa"), window_days)
        context = {"cut_conversions_per_day":
                   float(lost_window or 0.0) / max(1, int(window_days))}
    else:
        own_exposure = exposure.whole_object_exposure(
            "новый регион берёт деньги из того же лимита кампании, а истории "
            "по нему у неё нет: какую долю расхода он заберёт — неизвестно")
        evidence = None
        context = {"added_daily_rub": move.get("added_daily_rub"),
                   "cpa_rub": move.get("cpa_rub")}

    return expectation.attach({
        "action_kind": GEO_KIND,
        **({"evidence": evidence} if evidence else {}),
        "object_level": "campaign",
        "object_id": campaign_id,
        "exposure": own_exposure,
        "key": "regions",
        "payload": {
            "CampaignId": int(campaign_id),
            # Группы едут в теле: RegionIds — поле группы, и без их номеров
            # запрос не собрать. Список кампании единый — это проверено выше.
            "AdGroupIds": groups,
            "RegionIds": wanted,
            "AddedRegionIds": added,
            "RemovedRegionIds": removed,
        },
        "previous_state": {"RegionIds": current, "AdGroupIds": groups},
        "idempotency_key": _idempotency_key(campaign_id, wanted),
    }, context)


def to_api_call(action: Dict[str, Any]) -> Tuple[str, str, Dict[str, Any]]:
    """Действие → вызов API. Тонкая обёртка над общим сборщиком apply."""
    from sync.agent.writer.apply import to_api_call as apply_to_api_call
    return apply_to_api_call(action)
