# -*- coding: utf-8 -*-
"""
sync/agent/ideas/registry.py — реестр идей: идея живёт дольше такта.

Что чинит. Идея существовала внутри прогона: посчитали, показали, забыли.
Генератор при этом детерминирован — назавтра он находит ровно ту же связку и
предлагает её снова. Отсюда две болезни сразу: экран предложений за месяц
превращается в список, который человек перестаёт читать (а вместе с ним
перестаёт читать и новое), и нет ни истории «предложили → взяли в работу →
чем кончилось», ни возможности отказать один раз навсегда.

Реестр — это срок жизни, история и приоритет идеи. Он ничего не применяет:
идея едет к кабинету через план записи (задача 11), и класс достоверности
(writer/tier.py) вместе с полосой (writer/lanes.py) у неё те же самые, что у
действия. Второго определения полос и классов здесь нет и быть не должно.

Идентичность. idea_id выведен из тройки (account, source, subject) — одна и та
же связка от одного и того же генератора в одном кабинете всегда попадает В ТУ
ЖЕ СТРОКУ, а не заводит новую каждым тактом. subject_key выведен из пары
(account, subject) и одинаков у идей разных источников про один объект: на нём
держится отклонение человеком. Кабинет входит в оба ключа потому, что адрес
объекта у половины генераторов — направление, а направление «vuz» есть сразу в
нескольких кабинетах.

Отсюда требование к генераторам (задачи 12–15): в subject кладётся АДРЕС
объекта и ничего кроме — кампания, сегмент, фраза, связка. Изменчивые числа
(расход, ДРР, ожидаемая ценность) там смертельны: они пересчитываются каждым
прогоном, отпечаток уезжает вместе с ними, и идея каждое утро заводится
заново — с новым идентификатором, пустой историей и снятым отказом человека.
Числа живут в своих колонках, где им и место.

Кто хозяин статуса. Не генератор. Он вправе завести строку и обновлять свои
числа (ожидаемая ценность, цена проверки, горизонт, критерий), но статус
двигают применение и человек. Поэтому повторный upsert не сбрасывает running
в new, а закрытая идея (done/dropped) повторной генерацией не воскресает: её
поля вообще не переписываются — закрытая строка есть запись о случившемся.

Отклонение человеком — по ОБЪЕКТУ, а не по строке. Помни реестр отказ по
idea_id, обход был бы бесплатным: та же связка, найденная завтра генератором
другого источника, приезжает под другим идентификатором и молча возвращается
на экран. Поэтому отказ ищется по subject_key, а признаком «сказал человек»
служит колонка rejected_by, а не префикс свободного текста dropped_reason.
Отдельной таблицы отказов нет намеренно: второе хранилище того же факта
разъезжается с первым на первой же правке одного из них.

Где живёт правило слияния. Целиком в Python (_merge), а SQL — тупой:
переписывает строку тем, что ему дали. Так решено ради проверяемости: правило
в тексте запроса нельзя проверить иначе как живой базой, а двойник в тесте
повторил бы его второй копией — и тест доказывал бы свойства двойника.
Читать-и-писать без блокировки здесь безопасно: прогон агента один, его
держит аренда edu_agent_run_lock (writer/db.py).
"""

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

import psycopg2.extras

from sync.db import get_connection
from sync.agent.writer import lanes as lanes_mod
from sync.agent.writer import tier as tier_mod

STATUS_NEW = "new"
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_DROPPED = "dropped"
STATUS_PROPOSED = "proposed"

ALL_STATUSES = (STATUS_NEW, STATUS_QUEUED, STATUS_RUNNING, STATUS_DONE,
                STATUS_DROPPED, STATUS_PROPOSED)

# Закрытые статусы: идея своё отжила. Повторная генерация такую строку не
# трогает вовсе — ни статуса, ни чисел. Переписать ожидаемую ценность у
# закрытой идеи значило бы задним числом переписать основание решения, которое
# уже принято.
CLOSED_STATUSES = (STATUS_DONE, STATUS_DROPPED)

# Статусы, с которых идея НАЧИНАЕТ жизнь. Закрытие — событие, а не начальное
# состояние: идея, заведённая сразу закрытой, никогда не была предложена, и в
# реестре появилась бы запись о решении, которого никто не принимал.
OPEN_STATUSES = (STATUS_NEW, STATUS_QUEUED, STATUS_RUNNING, STATUS_PROPOSED)


def _canonical(value: Any) -> str:
    """Каноническая форма объекта идеи для отпечатка.

    sort_keys обязателен: словарь генератора собирается в произвольном порядке
    (разные ветки кода кладут ключи по-разному), и отпечаток, зависящий от
    порядка, дал бы новую строку реестра на тот же объект. Списки, наоборот,
    порядок сохраняют — «фразы-доноры A, B» и «B, A» это один набор, но
    решение о том, что порядок в нём не значим, принимает генератор, а не
    отпечаток.
    """
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=str)


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def idea_id(source: str, subject: Any, account: str) -> str:
    """Идентификатор идеи по тройке (кабинет, источник, объект).

    Кабинет входит в ключ, и это не украшение. Объект идеи адресуется тем,
    чем её адресует генератор, а половина генераторов адресуется НАПРАВЛЕНИЕМ
    (consolidate, market: subject = {kind, direction}). Направление «vuz» есть
    у нескольких кабинетов сразу — без кабинета в ключе находка второго
    кабинета молча затирала бы находку первого в той же порции, а отказ
    человека в одном кабинете глушил бы идею во всех остальных.

    Длина и способ те же, что у идентификаторов действия и ставки
    (writer/db.make_action_id, experiments.experiment_id_for): реестры агента
    сшиваются между собой, и разнобой в форме ключей мешал бы читать историю.
    """
    return _digest(f"idea:{account}:{source}:{_canonical(subject)}")


def subject_key(subject: Any, account: str) -> str:
    """Отпечаток ОБЪЕКТА идеи в его кабинете, без источника.

    Отдельная функция, а не часть idea_id: по ней ищется отклонение человеком,
    и она обязана совпадать у идей разных генераторов про один и тот же
    объект. Совпади она с idea_id — «нет» человека обходилось бы сменой
    источника.

    Кабинет здесь по той же причине, что и в idea_id, но цена ошибки другая:
    «нет» человека на вынос направления в одном кабинете не имеет отношения к
    тому же направлению в соседнем — там другие кампании, другие деньги и
    другой разговор.
    """
    return _digest(f"subject:{account}:{_canonical(subject)}")


# --------------------------------------------------------------- приоритет


def _number(value: Any) -> Optional[float]:
    """Число или None, если значения нет или оно не число.

    NaN — то же «неизвестно», что и пусто: сравнение с NaN всегда ложно, и
    попади он в ключ сортировки, порядок очереди зависел бы от порядка
    входного списка (тот же приём, что в writer/tier._number).
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _value_per_rub(idea: Dict[str, Any]) -> Optional[float]:
    """Ценность на рубль проверки. None — «не посчитано».

    Ноль в знаменателе здесь законен и означает бесплатную проверку (идея
    опирается на уже собранные данные), поэтому такая идея идёт впереди любой
    платной. Отрицательная цена — не «ещё дешевле», а испорченное число, и
    считается непосчитанной наравне с пустым.
    """
    expected = _number(idea.get("expected_rub"))
    cost = _number(idea.get("test_cost_rub"))
    if expected is None or cost is None or cost < 0:
        return None
    if cost == 0:
        return float("inf")
    return expected / cost


def rank(ideas: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Идеи по убыванию ценности на рубль проверки.

    Почему не по абсолютной ценности. Дорогая проверка съедает и риск-бюджет
    полосы, и горизонт, за который можно было закрыть три дешёвых: очередь,
    отсортированная по обещанию, встаёт на первой же крупной ставке.

    Непосчитанная цена (пусто, не число, отрицательное) — это НЕ ноль. Прочти
    её нулём — и незнание оказалось бы сильнейшим аргументом очереди: идея без
    сметы выносится вперёд посчитанных. Такие идут в хвост и там сортируются
    по обещанию, чтобы порядок оставался осмысленным и внутри хвоста.

    Порядок равноценных доопределён идентификатором: генератор детерминирован,
    и очередь обязана быть детерминирована вместе с ним — иначе на одних и тех
    же данных человек видит разный экран.

    Список не сортируется на месте: его читает и отчёт, и план записи, и
    перестановка под ногами второго читателя — дефект, который не увидит ни
    один из них.
    """
    items = list(ideas)
    priced = [i for i in items if _value_per_rub(i) is not None]
    unpriced = [i for i in items if _value_per_rub(i) is None]
    priced.sort(key=lambda i: (-_value_per_rub(i), str(i.get("idea_id") or "")))
    unpriced.sort(key=lambda i: (-(_number(i.get("expected_rub")) or 0.0),
                                 str(i.get("idea_id") or "")))
    return priced + unpriced


# ------------------------------------------------------- критерий и проверка

# Знаки сравнения, которые умеет проверить машина. Равенства здесь нет
# намеренно: «CPA станет ровно 900» не бывает ни истиной, ни ложью на реальных
# данных, и критерий с ним закрывать пришлось бы человеку.
COMPARISONS = ("<=", ">=", "<", ">")

# Колонки строки реестра. Один список на запись и на слияние: колонка, забытая
# в одном из двух мест, — это поле, которое пишется, но не переживает повторную
# генерацию, либо наоборот.
COLUMNS = ("idea_id", "source", "account", "subject", "subject_key", "tier",
           "lane", "expected_rub", "test_cost_rub", "horizon_days",
           "success_rule", "action", "detail", "status", "action_id",
           "experiment_id",
           "dropped_reason", "rejected_by", "rejected_at")

# Поля, которые генератор вправе обновлять у уже существующей идеи: его
# собственные числа и формулировки. Всего остального (статус, связи с
# действием и ставкой, отказ человека) он не хозяин — см. _merge.
#
# action здесь наравне с ценностью и сметой: нагрузка рычага — такое же
# посчитанное число, и заморозь её первой находкой, такт записи применял бы
# ставку, посчитанную в день появления идеи. В idea_id она при этом НЕ входит
# (см. idea_id): войди — и новая ставка заводила бы идею заново, с пустой
# историей и снятым отказом человека.
GENERATOR_FIELDS = ("source", "account", "subject", "subject_key", "tier",
                    "lane", "expected_rub", "test_cost_rub", "horizon_days",
                    "success_rule", "action", "detail")


class InvalidIdea(ValueError):
    """Идея, которой в реестре не место. ValueError — чтобы отказ нельзя было
    перепутать с пустым результатом: порция не пишется вовсе."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _check_success_rule(rule: Any) -> Dict[str, Any]:
    """Критерий успеха обязан быть проверяемым машиной.

    Три обязательных части: метрика, знак сравнения и порог. Пустой словарь и
    словарь-мнение («станет лучше») отвергаются одинаково — по одной причине:
    закрыть такую идею может только человек, а реестр заведён ровно затем,
    чтобы не гонять человека по одному и тому же списку.

    Значения по умолчанию тут были бы худшим решением из возможных: реестр
    придумал бы за генератор порог, о котором тот промолчал, и идея закрылась
    бы по критерию, которого никто не назначал.
    """
    if not isinstance(rule, dict) or not rule:
        raise InvalidIdea(
            "критерий успеха пуст: идея без машинно проверяемого критерия "
            "неотличима от мнения — её нельзя ни закрыть, ни засчитать")
    metric = _text(rule.get("metric"))
    op = _text(rule.get("op"))
    value = _number(rule.get("value"))
    if not metric or value is None:
        raise InvalidIdea(
            f"критерий успеха неполон ({rule!r}): нужны метрика, знак "
            "сравнения и порог")
    if op not in COMPARISONS:
        raise InvalidIdea(
            f"критерий успеха: знак сравнения {op!r} машина не проверит; "
            f"допустимы {', '.join(COMPARISONS)}")
    return dict(rule)


def _check_action(raw: Any, tier_value: int) -> Optional[Dict[str, Any]]:
    """Нагрузка рычага у идеи: обязательна применимой, запрещена предложению.

    Расчётный такт и такт записи — РАЗНЫЕ прогоны. Передать нагрузку в
    памяти нечем: такт записи читает идеи из базы (open_ideas), и всё, чего
    нет в колонке, для него не существует. Поэтому пустая нагрузка у
    применимого класса — не мелочь, а идея, которая гарантированно получит
    отказ «применять нечем» через сутки, в чужом прогоне и в чужом логе.
    Отказ ставится ЗДЕСЬ, пока генератор ещё жив и может быть исправлен.

    У класса 3 рычага нет по определению (writer/tier.py), и нагрузка у него
    означает не «есть чем применить», а перепутанный класс: предложение,
    притворяющееся применимым. Такое молча обрезать нельзя — обрезанная
    нагрузка выглядела бы как честное предложение, и дефект генератора уехал
    бы в реестр незамеченным.
    """
    if tier_value == tier_mod.TIER_PROPOSAL:
        if raw:
            raise InvalidIdea(
                "класс 3 несёт нагрузку действия: у предложения рычага записи "
                "нет по определению — либо это не предложение, либо нагрузка "
                "здесь лишняя")
        return None
    if not isinstance(raw, dict) or not raw:
        raise InvalidIdea(
            f"нагрузка действия {raw!r} пуста: такт записи читает идеи из "
            "базы, и применять такую идею будет нечем")
    return dict(raw)


def _check_detail(raw: Any) -> Optional[Dict[str, Any]]:
    """Доказательства идеи: словарь или ничего.

    Свободная форма намеренно: чем идея обоснована, знает только её генератор
    — у выноса связок это список доноров и план кросс-минусовки, у разведки
    спроса будет что-то своё. Реестр здесь не судья содержимого, он лишь не
    пускает в колонку то, что нельзя показать как объект.

    Пусто — законно: идея, вся суть которой в её адресе и числах, ничего
    сверх них не обязана нести. Не-словарь — отказ: список или строка в
    колонке JSONB прочитались бы кем-то как объект и уронили бы экран, а не
    генератор, который их туда положил.
    """
    if raw is None or raw == {}:
        return None
    if not isinstance(raw, dict):
        raise InvalidIdea(
            f"доказательства идеи {type(raw).__name__} — нужен словарь: "
            "экран и разбор читают их по именам полей")
    return dict(raw)


def _check_price(idea: Dict[str, Any], field: str,
                 non_negative: bool) -> Optional[float]:
    raw = idea.get(field)
    if raw is None:
        return None
    number = _number(raw)
    if number is None or (non_negative and number < 0):
        raise InvalidIdea(f"{field}: {raw!r} — не число, пригодное для очереди")
    return number


def _prepare(idea: Dict[str, Any]) -> Dict[str, Any]:
    """Идея генератора → строка реестра. Любая неполнота — отказ.

    Отказ, а не запись с дырой: строка без полосы, класса или горизонта
    доезжает до отчёта и до плана записи и там либо падает, либо трактуется
    по умолчанию — то есть решение принимает случайный потребитель, а не
    автор идеи.
    """
    source = _text(idea.get("source"))
    account = _text(idea.get("account"))
    subject = idea.get("subject")
    if not source:
        raise InvalidIdea("источник идеи пуст")
    if not account:
        raise InvalidIdea("кабинет идеи пуст")
    if not isinstance(subject, dict) or not subject:
        raise InvalidIdea("объект идеи пуст: непонятно, на что она")

    # Через _number, а не int(): список или объект уронил бы int() ошибкой
    # типа мимо всех проверок — прогон получил бы трассу вместо внятного
    # отказа, а порция ушла бы наполовину.
    tier = _number(idea.get("tier"))
    if tier is None or int(tier) not in tier_mod.ALL_TIERS:
        raise InvalidIdea(
            f"класс достоверности {idea.get('tier')!r} неизвестен; шкала одна "
            "на идею и на действие — writer/tier.py")
    lane = _text(idea.get("lane"))
    if lane not in lanes_mod.ALL_LANES:
        raise InvalidIdea(f"полоса {lane!r} неизвестна")

    horizon = _number(idea.get("horizon_days"))
    if horizon is None or horizon <= 0:
        raise InvalidIdea(
            f"горизонт {idea.get('horizon_days')!r}: без срока идею нечем "
            "закрыть, критерий проверяется К КОНЦУ горизонта")

    status = _text(idea.get("status")) or STATUS_NEW
    if status not in OPEN_STATUSES:
        raise InvalidIdea(
            f"статус {status!r} не бывает начальным: закрытие идеи — событие "
            "(mark, reject), а не состояние, с которого она заводится")

    computed_id = idea_id(source, subject, account)
    given_id = _text(idea.get("idea_id"))
    if given_id and given_id != computed_id:
        raise InvalidIdea(
            f"идентификатор {given_id!r} не выводится из пары "
            f"(источник, объект) — назавтра генератор посчитает другой и "
            "заведёт вторую строку на ту же находку")

    return {
        "idea_id": computed_id,
        "source": source,
        "account": account,
        "subject": subject,
        "subject_key": subject_key(subject, account),
        "tier": int(tier),
        "lane": lane,
        "expected_rub": _check_price(idea, "expected_rub", non_negative=False),
        "test_cost_rub": _check_price(idea, "test_cost_rub", non_negative=True),
        "horizon_days": int(horizon),
        "success_rule": _check_success_rule(idea.get("success_rule")),
        "detail": _check_detail(idea.get("detail")),
        "action": _check_action(idea.get("action"), int(tier)),
        "status": status,
        # Связи и отказ генератору не принадлежат: их ставят применение
        # (задача 11) и человек (reject).
        "action_id": None,
        "experiment_id": None,
        "dropped_reason": None,
        "rejected_by": None,
        "rejected_at": None,
    }


# ------------------------------------------------------------------- запись


def _merge(existing: Optional[Dict[str, Any]],
           incoming: Dict[str, Any]) -> Dict[str, Any]:
    """Что окажется в строке после повторной находки той же идеи.

    Правило одно и держится на том, что генератор — НЕ хозяин статуса. Он
    вправе обновлять свои числа и формулировки (GENERATOR_FIELDS); статус,
    связи с действием и ставкой, причина снятия и отказ человека остаются
    такими, какими их поставили применение и человек.

    Закрытая строка не переписывается вовсе — даже числами. Смета закрытой
    идеи есть основание уже принятого решения, и правка её задним числом
    оставила бы разбор без тех чисел, по которым идею закрывали.

    Живая, наоборот, числа обязана обновлять: заморозь их — и очередь
    ранжировалась бы по ценности, посчитанной в день появления идеи.
    """
    if existing is None:
        return incoming
    if str(existing.get("status")) in CLOSED_STATUSES:
        return dict(existing)
    merged = dict(existing)
    for field in GENERATOR_FIELDS:
        merged[field] = incoming[field]
    return merged


def upsert(ideas: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Пишет порцию идей генератора и возвращает строки, как они теперь стоят.

    Порция принимается ЦЕЛИКОМ или не принимается вовсе: половина порции —
    состояние, которого не задавал никто (часть идей такта в реестре, часть
    нет), и назавтра генератор досыпал бы остаток как новые идеи. Поэтому
    сначала проверяется всё, и только потом пишется.

    Закрытые строки не переписываются и не пишутся заново: их updated_at
    обязан остаться днём закрытия, иначе «эта идея закрыта два месяца назад»
    перестало бы быть видно в данных.
    """
    prepared = [_prepare(idea) for idea in ideas]
    if not prepared:
        return []
    # Одна и та же находка дважды в одной порции — двойной счёт: в отчёте
    # такта идея посчитается два раза, а в реестр ляжет одна строка, и
    # расхождение будет нечем объяснить. Схлопнуть молча тоже нельзя:
    # генератор обязан узнать, что нашёл одно и то же дважды.
    seen = {row["idea_id"] for row in prepared}
    if len(seen) != len(prepared):
        raise InvalidIdea(
            "порция содержит одну и ту же идею дважды: пара (источник, объект) "
            "повторяется, а идентификатор из неё и выведен")
    existing = _read_rows([row["idea_id"] for row in prepared])
    rejections = _read_rejections({row["subject_key"] for row in prepared})

    result: List[Dict[str, Any]] = []
    to_write: List[Dict[str, Any]] = []
    for row in prepared:
        old = existing.get(row["idea_id"])
        was_closed = old is not None and str(old.get("status")) in CLOSED_STATUSES
        merged = _merge(old, row)
        if was_closed:
            # Уже закрытая строка не переписывается и не пишется заново: её
            # updated_at обязан остаться днём закрытия, иначе «эта идея
            # закрыта два месяца назад» перестанет быть видно в данных.
            result.append(merged)
            continue
        merged = _silence(merged, rejections.get(row["subject_key"]))
        to_write.append(merged)
        result.append(merged)
    _write_rows(to_write)
    return result


def _human_reason(who: str, reason: str) -> str:
    """Причина снятия словами человека — для отчёта и разбора.

    Начинается со слова «человек» намеренно: в отчёте видно, кто закрыл идею,
    без похода в колонки. Но ПРАВИЛОМ этот текст не служит — машинный признак
    отказа живёт в rejected_by (см. докстринг модуля).
    """
    words = _text(reason)
    tail = f": {words}" if words else ""
    return f"человек ({who}) отклонил эту идею{tail}"


def _silence(row: Dict[str, Any],
             rejection: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Идея про объект, по которому человек уже сказал «нет».

    Строка не удаляется и не прячется: она ложится в реестр СРАЗУ снятой, с
    именем человека и его формулировкой. Так у отказа остаётся след — видно,
    что генератор нашёл это снова и был остановлен, а не что он вдруг
    перестал находить.
    """
    if not rejection or not _text(rejection.get("rejected_by")):
        return row
    who = _text(rejection.get("rejected_by"))
    reason = _text(rejection.get("dropped_reason")) or _human_reason(who, "")
    out = dict(row)
    out["status"] = STATUS_DROPPED
    out["rejected_by"] = who
    out["rejected_at"] = rejection.get("rejected_at")
    out["dropped_reason"] = reason
    return out


def reject(idea_id_value: str, by: str, reason: str = "") -> Dict[str, Any]:
    """Человек отклонил идею. Отказ помнится по ОБЪЕКТУ и переживает строку.

    Автор обязателен: отказ без имени неотличим от машинного снятия (mark со
    статусом dropped), а различие между ними и есть всё содержание механизма
    — машина снимает идею по технической причине, и объект после этого обязан
    оставаться живым.

    Уже закрытая идея сохраняет свой исход: раскатанная остаётся done, потому
    что раскатана. Отказ при этом всё равно записывается — он относится к
    объекту, а не к строке, и глушит будущие находки того же объекта любым
    генератором.
    """
    who = _text(by)
    if not who:
        raise InvalidIdea(
            "отказ без автора: без имени он неотличим от машинного снятия, а "
            "машинное снятие объект не глушит")
    row = load(idea_id_value)
    if row is None:
        raise InvalidIdea(f"идеи {idea_id_value!r} нет в реестре: отклонять нечего")

    row = dict(row)
    row["rejected_by"] = who
    row["rejected_at"] = datetime.now(timezone.utc)
    row["dropped_reason"] = _human_reason(who, reason)
    if str(row.get("status")) not in CLOSED_STATUSES:
        row["status"] = STATUS_DROPPED
    _write_rows([row])
    return row


def mark(idea_id_value: str, status: str, action_id: Optional[str] = None,
         experiment_id: Optional[str] = None,
         reason: Optional[str] = None) -> Dict[str, Any]:
    """Двигает статус идеи и подшивает к ней связи. Возвращает строку.

    Единственная точка, которой позволено менять жизненный цикл: применение
    ставит queued/running и связывает идею с действием и ставкой, замер
    закрывает её как done, машина снимает как dropped. Человек ходит через
    reject — его отказ хранится иначе (см. докстринг модуля).

    Из закрытого статуса пути нет. Отказ здесь громкий, а не тихий: молчаливое
    «ничего не делаю» оставило бы прогон с отчётом об успехе и идею в прежнем
    статусе — расхождение всплыло бы на разборе, когда причину восстановить уже
    нечем (тот же довод, что у experiments.check_transition).
    """
    if status not in ALL_STATUSES:
        raise InvalidIdea(f"статус {status!r} неизвестен")
    row = load(idea_id_value)
    if row is None:
        raise InvalidIdea(f"идеи {idea_id_value!r} нет в реестре: двигать нечего")

    current = str(row.get("status") or "")
    if current in CLOSED_STATUSES:
        if status == current:
            return row
        raise InvalidIdea(
            f"идея {idea_id_value!r} закрыта как {current!r}: закрытая идея "
            "не открывается заново")

    row = dict(row)
    row["status"] = status
    if action_id is not None:
        row["action_id"] = str(action_id)
    if experiment_id is not None:
        row["experiment_id"] = str(experiment_id)
    if reason is not None:
        row["dropped_reason"] = str(reason)
    _write_rows([row])
    return row


def load(idea_id_value: str) -> Optional[Dict[str, Any]]:
    """Строка реестра по идентификатору. None — такой идеи нет."""
    return _read_rows([idea_id_value]).get(idea_id_value)


def idea_tier(idea: Dict[str, Any]) -> int:
    """Класс достоверности идеи. Пустой или незнакомый класс — предложение.

    Умолчание — самый строгий конец шкалы намеренно. Ноль означает
    «утверждение о прошлом: применяется всегда и риском не платит»; прочти
    пустое поле нулём — и забывчивость генератора открывала бы кабинет самым
    дешёвым путём, какой в движке есть.

    Правило живёт ЗДЕСЬ, а не у каждого читателя реестра. Читателей уже два —
    отчёт такта расчёта и гейт такта записи, — и разойдись их умолчания хоть
    на шаг, человек видел бы идею предложением на экране, пока агент считал
    бы её измеренной и применял.
    """
    try:
        value = int(idea.get("tier"))
    except (TypeError, ValueError):
        return tier_mod.TIER_PROPOSAL
    return value if value in tier_mod.ALL_TIERS else tier_mod.TIER_PROPOSAL


def open_ideas(account: Optional[str] = None) -> List[Dict[str, Any]]:
    """Открытые идеи реестра в порядке очереди. account=None — все кабинеты.

    Порядок задаёт rank, а не база. SQL-сортировка детерминирована, но
    сортирует по идентификатору, то есть по хэшу: отчёт показывал бы человеку
    один порядок, а такт записи брал бы идеи в другом — и объяснить, почему
    первой поехала не верхняя строка экрана, было бы нечем.

    Закрытые идеи не читаются вовсе: реестр помнит их ради истории, а не ради
    повторного предложения. Отклонённая человеком лежит именно закрытой
    (_silence), и попади она в этот список — его «нет» обходилось бы каждым
    тактом.
    """
    return rank(_read_open(account))


# ------------------------------------------------------------ доступ к БД
# Четыре примитива ниже — единственное место модуля, которое ходит в базу, и
# ничего кроме хранения не делают: правило слияния живёт в Python выше, а SQL
# переписывает строку тем, что ему дали. Так решено ради проверяемости —
# правило в тексте запроса нельзя проверить иначе как живой базой.

UPSERT_SQL = """
    INSERT INTO edu_agent_ideas (
        idea_id, source, account, subject, subject_key, tier, lane,
        expected_rub, test_cost_rub, horizon_days, success_rule, action,
        detail, status, action_id, experiment_id, dropped_reason, rejected_by,
        rejected_at
    ) VALUES (
        %(idea_id)s, %(source)s, %(account)s, %(subject)s, %(subject_key)s,
        %(tier)s, %(lane)s, %(expected_rub)s, %(test_cost_rub)s,
        %(horizon_days)s, %(success_rule)s, %(action)s, %(detail)s,
        %(status)s, %(action_id)s, %(experiment_id)s, %(dropped_reason)s,
        %(rejected_by)s, %(rejected_at)s
    )
    ON CONFLICT (idea_id) DO UPDATE SET
        source         = EXCLUDED.source,
        account        = EXCLUDED.account,
        subject        = EXCLUDED.subject,
        subject_key    = EXCLUDED.subject_key,
        tier           = EXCLUDED.tier,
        lane           = EXCLUDED.lane,
        expected_rub   = EXCLUDED.expected_rub,
        test_cost_rub  = EXCLUDED.test_cost_rub,
        horizon_days   = EXCLUDED.horizon_days,
        success_rule   = EXCLUDED.success_rule,
        action         = EXCLUDED.action,
        detail         = EXCLUDED.detail,
        status         = EXCLUDED.status,
        action_id      = EXCLUDED.action_id,
        experiment_id  = EXCLUDED.experiment_id,
        dropped_reason = EXCLUDED.dropped_reason,
        rejected_by    = EXCLUDED.rejected_by,
        rejected_at    = EXCLUDED.rejected_at,
        updated_at     = now()
"""

# Отклонения человеком: по КЛЮЧУ ОБЪЕКТА, не по идентификатору идеи.
# DISTINCT ON оставляет последнее слово человека — отказов по одному объекту
# может накопиться несколько (разные источники нашли его в разные дни).
SELECT_REJECTIONS_SQL = """
    SELECT DISTINCT ON (subject_key)
           subject_key, rejected_by, rejected_at, dropped_reason
      FROM edu_agent_ideas
     WHERE rejected_by IS NOT NULL
       AND subject_key = ANY(%s)
     ORDER BY subject_key, rejected_at DESC NULLS LAST
"""


# Открытые идеи кабинета. Фильтр по кабинету — внутри запроса, а не после
# чтения: реестр общий на все кабинеты, а такт записи идёт по одному, и
# вычитывать чужие строки в память ради того, чтобы тут же их выбросить, —
# лишний трафик, растущий с числом кабинетов.
SELECT_OPEN_SQL = """
    SELECT *
      FROM edu_agent_ideas
     WHERE status = ANY(%(statuses)s)
       AND (%(account)s IS NULL OR account = %(account)s)
"""


def _json_params(row: Dict[str, Any]) -> Dict[str, Any]:
    """Строка → параметры запроса, ровно по объявленным колонкам.

    Проекция на COLUMNS обязательна: строка, прочитанная из базы, несёт ещё и
    created_at с updated_at, а слияние может дописать в неё что угодно. Лишний
    ключ в базу молча не поедет, недостающий уехал бы как NULL — оба конца
    держит один список.
    """
    params = {column: row.get(column) for column in COLUMNS}
    params["subject"] = json.dumps(row.get("subject") or {}, ensure_ascii=False)
    params["success_rule"] = json.dumps(row.get("success_rule") or {},
                                        ensure_ascii=False)
    # Пустая нагрузка едет NULL-ом, а не литералом 'null': колонка читается
    # как «рычага нет» (у предложения его и не бывает), и два разных способа
    # сказать это разъехались бы на первом же условии с IS NULL.
    action = row.get("action")
    params["action"] = (json.dumps(action, ensure_ascii=False)
                        if action else None)
    detail = row.get("detail")
    params["detail"] = (json.dumps(detail, ensure_ascii=False)
                        if detail else None)
    return params


def _read_rows(idea_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    ids = [str(i) for i in idea_ids]
    if not ids:
        return {}
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM edu_agent_ideas WHERE idea_id = ANY(%s)", (ids,))
            return {str(r["idea_id"]): dict(r) for r in cur.fetchall()}


def _read_open(account: Optional[str] = None) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(SELECT_OPEN_SQL,
                        {"statuses": list(OPEN_STATUSES),
                         "account": None if account is None else str(account)})
            return [dict(r) for r in cur.fetchall()]


def _read_rejections(subject_keys: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    keys = [str(k) for k in subject_keys]
    if not keys:
        return {}
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(SELECT_REJECTIONS_SQL, (keys,))
            return {str(r["subject_key"]): dict(r) for r in cur.fetchall()}


def _write_rows(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            for row in rows:
                cur.execute(UPSERT_SQL, _json_params(row))
        conn.commit()
    return len(rows)
