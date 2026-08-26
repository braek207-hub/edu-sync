# -*- coding: utf-8 -*-
"""
sync/agent/writer/reprice.py — пересчёт цен УЖЕ ЗАПИСАННЫХ действий журнала.

Дельта-модель риска (writer/exposure.py) появилась 25.08.2026, а журнал
остался в ценах до неё. Пять строк от 22.08 держат 38 876 ₽ из 50 000
недельного лимита и держали бы до 05.09: writer/db.spent_risk считает окно
недели, расширенное назад на горизонт замера, поэтому старая цена мешает
писать ещё неделю после того, как модель уже исправлена. Пока журнал в старых
ценах, агент почти не может действовать — чинить темп до этого бессмысленно.

Откуда берётся доля сегмента. Блока exposure в журнале НЕТ: INSERT_ACTION_SQL
(writer/db.py) пишет payload, direct_type, setting_key, risk_basis,
baseline_daily_rub — и всё. Пересчёт «по правилу неизвестной доли» вернул бы
ровно старую цену (доля неизвестна → под ударом весь объект), то есть не
изменил бы ничего. Поэтому доля восстанавливается из расчёта Э0 тем же
plan.segment_shares, что считал её при первичной оценке, — по строкам
edu_agent_computed_settings.

Расчёт берётся НА ДАТУ ДЕЙСТВИЯ, а не сегодняшний. Сегодняшняя доля сегмента
— уже следствие того самого действия, которое переоценивается: корректировка
−43 % на AGE_55 сама сдвинула долю этого сегмента, и считать по ней значило бы
судить причину по её последствию.

Строка, для которой доля не нашлась или не из чего посчитать сдвиг, НЕ
переоценивается и уходит в отдельный список с причиной. Молчаливое «оставили
как есть» неотличимо от «проверили и всё верно» — та же дыра, из-за которой
risk.object_daily_cost когда-то возвращал ноль вместо «расход неизвестен».
"""

from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from sync.agent.writer import exposure as exposure_mod
from sync.agent.writer import plan as plan_mod
from sync.agent.writer import risk as risk_mod

# Ключ соединения журнала с расчётом: (тип, канонический ключ настройки).
ShareKey = Tuple[str, str]

# Поле payload, в котором лежит сила сдвига корректировки. Оно есть у обеих
# форм действия: bidmodifier.add несёт весь набор, bidmodifier.set — Id и
# BidModifier (writer/diff.py). Его отсутствие и есть признак «это не
# корректировка ставки», проверяемый по данным, а не по списку видов: список
# отстанет от следующего рычага молча, а payload не соврёт.
PERCENT_FIELD = "BidModifier"

REASON_NO_SHARE = "доля сегмента в расчёте на дату действия не нашлась"
REASON_NO_PERCENT = f"в payload нет {PERCENT_FIELD} — силу сдвига взять неоткуда"
REASON_NO_COST = "дневной расход объекта неизвестен и оценить не от чего"
REASON_ALREADY_DELTA = "цена уже посчитана дельта-моделью (заполнен risk_basis)"
REASON_SAME_PRICE = "пересчёт дал ту же цену — она не изменилась"


def _canonical_key(value: Any) -> str:
    """Ключ настройки в той же форме, в какой его пишет план: ВЕРХНИЙ регистр.

    Расхождение регистра здесь означало бы, что план ("age_55") и журнал
    ("AGE_55") не сойдутся по ключу никогда, — тот же дефект, который
    plan.direct_type_for закрывает на записи.
    """
    return str(value or "").strip().upper()


def _shares_from_computed(computed: List[Dict[str, Any]]) -> Dict[ShareKey, float]:
    """Строки расчёта → {(тип корректировки, ключ): доля 0..1}.

    Доля считается plan.segment_shares — тем же кодом, что и при первичной
    оценке. Своя формула здесь дала бы второе определение доли, и первое же
    расхождение между ними выглядело бы как ошибка переоценки.

    Ключ переводится в алфавит журнала (plan.direct_type_for), потому что
    расчёт хранит ВИД настройки (bid_modifier:age), а журнал — ТИП
    корректировки Директа (DEMOGRAPHICS_ADJUSTMENT). Вид, у которого типа нет,
    остаётся под своим именем: пусть по нему ничего не найдётся, но строка
    расчёта не исчезнет из отображения молча.
    """
    out: Dict[ShareKey, float] = {}
    for (kind, key), share in plan_mod.segment_shares(computed).items():
        direct_type, canonical, _ = plan_mod.direct_type_for(kind, key)
        out[(str(direct_type or kind), _canonical_key(canonical))] = float(share)
    return out


def _lookup_share(shares: Dict[ShareKey, float],
                 action: Dict[str, Any]) -> Optional[float]:
    """Доля сегмента строки журнала. None — «не нашлась», а не «нулевая».

    Сначала точная пара (тип, ключ). Если её нет — совпадение по одному
    ключу, но ТОЛЬКО когда оно единственное: ключи сегментов внутри расчёта
    не пересекаются (AGE_*, GENDER_*, MOBILE/DESKTOP/TABLET, числовые
    регионы), поэтому единственное совпадение однозначно, а множественное —
    повод отказаться, а не выбрать наугад.

    Запасной путь нужен потому, что тип в журнале и вид в расчёте — разные
    алфавиты, и строки разных эпох движка писали его по-разному. Требовать
    точную пару значило бы переоценить только часть журнала и объявить
    остальное «долей без расчёта».
    """
    key = _canonical_key(action.get("setting_key"))
    if not key:
        return None
    direct_type = action.get("direct_type")
    if direct_type is not None:
        exact = shares.get((str(direct_type), key))
        if exact is not None:
            return exact
    by_key = [share for (_, k), share in shares.items() if k == key]
    if len(by_key) == 1:
        return by_key[0]
    return None


def share_for(action: Dict[str, Any],
              computed: List[Dict[str, Any]]) -> Optional[float]:
    """Доля сегмента строки журнала по расчёту Э0 ТОГО дня."""
    return _lookup_share(_shares_from_computed(computed), action)


def _percent(action: Dict[str, Any]) -> Optional[int]:
    value = (action.get("payload") or {}).get(PERCENT_FIELD)
    if value is None:
        return None
    return int(round(float(value)))


def _exposure(action: Dict[str, Any], share: float) -> Optional[Dict[str, Any]]:
    percent = _percent(action)
    if percent is None:
        return None
    return exposure_mod.bid_modifier_exposure(percent, share)


def recompute(
    action: Dict[str, Any],
    share: Optional[float],
    daily_cost_by_campaign: Dict[str, float],
    days_to_measure: int = risk_mod.DEFAULT_DAYS_TO_MEASURE,
) -> Optional[float]:
    """Новая цена строки. None — переоценивать не из чего, строку не трогать.

    Считается risk.action_risk по собранной из доли экспозиции — той же
    функцией, которой оценивает свои действия сегодняшний прогон. Отдельная
    арифметика здесь означала бы, что переоценённая строка живёт по другим
    правилам, чем строка следующего прогона, и суммы в spent_risk смешивали
    бы две модели.

    Текущее значение risk_rub не читается — поэтому повторная переоценка
    возвращает то же число, а не переоценивает переоценённое.
    """
    if share is None:
        return None
    exposure = _exposure(action, share)
    if exposure is None:
        return None
    new = risk_mod.action_risk({**action, "exposure": exposure},
                               daily_cost_by_campaign, days_to_measure)
    if new == float("inf"):
        return None
    return new


def basis_for(
    action: Dict[str, Any],
    share: float,
    daily_cost_by_campaign: Dict[str, float],
) -> str:
    """Почему цена такая — строкой в колонку risk_basis и в отчёт скрипта.

    Число без основания непроверяемо: по нему не видно, посчитана доля
    сегмента или молча взят весь объект.
    """
    exposure = _exposure(action, share)
    return risk_mod.action_risk_basis({**action, "exposure": exposure},
                                      daily_cost_by_campaign)


def _identity(action: Dict[str, Any]) -> Dict[str, Any]:
    return {"action_id": action.get("action_id"),
            "action_kind": action.get("action_kind"),
            "object_id": action.get("object_id"),
            "risk_rub": float(action.get("risk_rub") or 0.0)}


def plan(
    rows: List[Dict[str, Any]],
    computed: List[Dict[str, Any]],
    daily_cost_by_campaign: Dict[str, float],
    days_to_measure: int = risk_mod.DEFAULT_DAYS_TO_MEASURE,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Что переоценить и что осталось как есть: (repriced, untouched).

    computed — строки расчёта Э0 на дату этих действий, из которых share_for
    восстанавливает долю сегмента. Готовый словарь долей сюда не передаётся
    намеренно: он был бы вторым местом, где доля определяется, и разъехался
    бы с share_for на первом же изменении формулы.

    Каждая строка входа попадает РОВНО в один из двух списков — иначе отчёт
    не отличить от «часть строк потерялась по дороге».

    Заполненный risk_basis — признак, что строку писала уже дельта-модель:
    колонка появилась вместе с ней. Такую строку не трогаем: переоценивать
    при неизменной модели значит рисковать сдвигом цены на ровном месте.
    """
    repriced: List[Dict[str, Any]] = []
    untouched: List[Dict[str, Any]] = []
    for row in rows:
        if str(row.get("risk_basis") or "").strip():
            untouched.append({**_identity(row), "reason": REASON_ALREADY_DELTA})
            continue
        share = share_for(row, computed)
        if share is None:
            untouched.append({**_identity(row), "reason": REASON_NO_SHARE})
            continue
        if _percent(row) is None:
            untouched.append({**_identity(row), "reason": REASON_NO_PERCENT})
            continue
        new = recompute(row, share, daily_cost_by_campaign, days_to_measure)
        if new is None:
            untouched.append({**_identity(row), "reason": REASON_NO_COST})
            continue
        old = float(row.get("risk_rub") or 0.0)
        if round(new, 2) == round(old, 2):
            untouched.append({**_identity(row), "reason": REASON_SAME_PRICE})
            continue
        repriced.append({**_identity(row), "old": old, "new": new,
                         "basis": basis_for(row, share, daily_cost_by_campaign)})
    return repriced, untouched


# ------------------------------------------------------------------ история

def action_date(action: Dict[str, Any]) -> Optional[date]:
    """Дата действия для выбора расчёта: applied_at, иначе created_at."""
    for field in ("applied_at", "created_at"):
        value = action.get(field)
        if value is None:
            continue
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return datetime.fromisoformat(str(value)).date()
    return None


COMPUTED_ON_SQL = """
    SELECT calc_date, setting_kind, setting_key, value, support_n,
           raw_value, rel_error
    FROM edu_agent_computed_settings
    WHERE object_level = %s AND object_id = %s
      AND calc_date = (
          SELECT MAX(calc_date) FROM edu_agent_computed_settings
          WHERE object_level = %s AND object_id = %s AND calc_date <= %s
      )
"""


def computed_on(object_level: str, object_id: str,
                on_date: date) -> List[Dict[str, Any]]:
    """Последний расчёт объекта, посчитанный НЕ ПОЗЖЕ даты действия.

    Не MAX(calc_date) вообще, как в load_latest_computed_settings: тот отдаёт
    сегодняшний расчёт, а сегодняшняя доля сегмента — уже след действия,
    которое переоценивается.
    """
    # Драйвер БД подтягивается здесь, а не в шапке модуля: арифметика
    # переоценки обязана проверяться на фикстурах, без psycopg2 и без
    # DATABASE_URL.
    from sync.agent import db as agent_db

    object_id = agent_db.normalize_login(object_id)
    return agent_db._fetch_dicts(
        COMPUTED_ON_SQL,
        (object_level, object_id, object_level, object_id, on_date),
    )


def computed_for_action(account: str, object_id: str,
                        on_date: date) -> List[Dict[str, Any]]:
    """Расчёт, по которому эта кампания оценивалась на дату действия.

    Приоритет тот же, что у прогона (sync/agent_e1.py): личные значения
    кампании, если они есть, иначе кабинетные. Смешивать два уровня нельзя —
    прогон их не смешивал, и переоценка обязана повторить его выбор, а не
    сделать свой.
    """
    campaign = computed_on("campaign", object_id, on_date)
    if campaign:
        return campaign
    return computed_on("account", account, on_date)
