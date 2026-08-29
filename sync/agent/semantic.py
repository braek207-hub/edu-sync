# -*- coding: utf-8 -*-
"""
sync/agent/semantic.py — смысловой слой над кандидатами рычагов.

Арифметика видит только деньги и конверсии. Фраза с пятью кликами и нулём
конверсий для неё неотличима от фразы, которая просто не набрала объём: при
базовой конверсии 3,6 % приговор требует 83 кликов, а хвост живёт на трёх-
двадцати. Замер 25.08: расход на запросах без единой конверсии — 45,5 млн ₽
за шесть недель, из них статистика способна судить 208 фраз на 2,08 млн ₽.
Остальные 43,5 млн — зона, где решает СМЫСЛ, а не объём.

Смысл эту границу видит сразу: «скачать реферат» — мусор при любом объёме,
«высшее образование дистанционно» — ядро при любой цене. Ровно этого не
хватило 25.08, когда рычаг предложил заминусовать «высшее», «институты» и
«факультеты»: защиту пришлось строить правилами (ноль конверсий + слова
собственных ключевых фраз), а правила описывают частные случаи.

ГЛАВНОЕ ПРАВИЛО СЛОЯ: модель может только ЗАПРЕТИТЬ действие, но не назначить
его. Кандидатов по-прежнему отбирает экономика; вердикт "core" снимает
кандидата с минусации, вердикт "junk" снимает его с расширения. «Модель
сказала мусор» само по себе не основание тратить или резать деньги —
галлюцинация в этой позиции стоила бы кампании.

Второе правило: отказ слоя не останавливает рычаг. Нет ключа, сеть легла,
ответ не разобрался — все вердикты становятся UNCLEAR, и поведение ровно
такое же, как до появления модуля: решает статистика.
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

# Вердикты. UNCLEAR — безопасное умолчание: молчание модели не должно ни
# запрещать трафик, ни разрешать трату.
JUNK = "junk"
CORE = "core"
UNCLEAR = "unclear"

KNOWN_VERDICTS = (JUNK, CORE, UNCLEAR)

# Сколько фраз уходит в один запрос. Батч крупнее экономит вызовы, но длинный
# ответ модель чаще обрывает на середине, а оборванный JSON — это UNCLEAR для
# всего батча разом.
BATCH_SIZE = 40

# Алиас "deepseek-chat" провайдер снял с поддержки 24.07.2026: он ещё отвечает,
# но только по доброй воле провайдера, и фактически указывает на облегчённую
# модель "deepseek-v4-flash" — поэтому здесь явный идентификатор, а не алиас.
# Пара "deepseek-v4-pro" втрое дороже и для этого слоя не нужна.
DEEPSEEK_MODEL = "deepseek-v4-flash"

# Чем занимается проект, когда паспорта направления нет. Без описания модель
# судит фразы в вакууме: «школа» для образовательного проекта ядро, для
# магазина одежды мусор. Живёт здесь, а не у вызывающего: это умолчание САМОГО
# слоя, и вторая его копия у каждого прогона разъехалась бы с первой.
DEFAULT_CONTEXT = (
    "онлайн-образование: высшее и среднее профессиональное образование, "
    "колледж, дистанционное обучение, приём абитуриентов"
)

# Где лежат проекции паспортов: <продукт>.json, где продукт — уровень билдера
# (лендинг), а не направление кампаний. Проекция, а не паспорт целиком — тот
# весит 27 КБ и в промпт не помещается (scripts/import_passport.py).
#
# Ключ продукта, а не направления, потому что паспорт делается ПОД ЛЕНДИНГ, и
# один и тот же detect_direction накрывает разные продукты: в 'dist' попадают
# и ВПО-дистант, и СПО-дистант, у которых «после 9 класса» — анти-маркер
# одного и целевой маркер другого. Обратное тоже верно: продукт живёт в
# нескольких кампаниях (у «Онлайн-школы» их восемь — РСЯ, ретаргетинг,
# конкуренты, вечерние), и направление у них одно.
PASSPORTS_DIR = Path(__file__).resolve().parent / "passports"

# Карта адресации: кампания → продукт (точная, из журнала билдера) и
# направление → продукт (приближение для кампаний, которых билдер не собирал).
# Направление попадает в карту только там, где оно описывается ОДНИМ
# продуктом; смешанные (dist) не попадают вовсе — приблизиться к ним нечем.
PASSPORT_INDEX = "index.json"

# Сколько символов паспорта едет в промпт. Предел не косметический: паспорт
# без границы вытеснил бы список фраз за окно модели, и батч вернулся бы
# пустым — то есть UNCLEAR по всем сорока фразам разом.
PASSPORT_BUDGET = 2400
_FIELD_LIMIT = 600      # на один раздел проекции
_ITEM_LIMIT = 160       # на один пункт списка

# Метка начала изменчивой части промпта. Всё до неё обязано совпадать от
# батча к батчу: кэш модели считает общий ПРЕФИКС, и сдвиг стабильного текста
# вниз обнуляет попадания (98 % → 0) на всём прогоне.
PHRASES_MARKER = "Фразы:"

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _fit(text: str, limit: int) -> str:
    """Усечение по длине БЕЗ схлопывания переносов: границы разделов
    важнее экономии символов — слипшись в один абзац, «кому не подходит»
    читается как продолжение описания продукта."""
    text = str(text or "")
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _bullets(items, limit: int) -> str:
    return "\n".join(f"  - {_clip(i, _ITEM_LIMIT)}" for i in list(items)[:limit])


def passport_block(passport: Optional[Dict[str, Any]]) -> str:
    """Паспорт продукта → компактный блок промпта. Нет паспорта — пусто.

    Едут ровно те разделы, которыми решается интент фразы: что продаём, кто
    покупатель, кто НЕ наш, слова чужого интента и слова своего. Остальное
    (цитаты, доказательства, оговорки) нужно объявлениям, а не разметке, и
    заняло бы место, за которым фразы не поместятся.

    Анти-маркеры отдаются С ПРИЧИНОЙ: «аспирантура» без пояснения читается
    моделью как «слово запрещено», а нужно «это другой продукт кабинета» —
    иначе она разметит junk-ом и «аспирантура дистанционно», и «магистратура
    дистанционно» заодно.
    """
    if not passport:
        return ""
    anti = []
    for item in list(passport.get("anti_markers") or ())[:30]:
        if isinstance(item, dict):
            word, reason = item.get("word"), item.get("reason")
            anti.append(f"{word} ({reason})" if reason else str(word))
        else:
            anti.append(str(item))

    parts = []
    what = _clip(passport.get("what"), _FIELD_LIMIT)
    if what:
        parts.append(f"Что продаём. {what}")
    who = _clip(passport.get("who"), _FIELD_LIMIT)
    if who:
        parts.append(f"Кто покупатель. {who}")
    not_ours = _bullets(passport.get("not_ours") or (), 8)
    if not_ours:
        parts.append("Кому наш продукт не подходит:\n" + not_ours)
    if anti:
        parts.append("Слова чужого интента: " + _clip("; ".join(anti), _FIELD_LIMIT))
    target = passport.get("target_markers") or ()
    if target:
        parts.append("Слова нашего интента: "
                     + _clip(", ".join(str(t) for t in target), _FIELD_LIMIT))
    rivals = passport.get("competitors") or ()
    if rivals:
        parts.append("Чужие бренды ниши: "
                     + _clip(", ".join(str(r) for r in rivals), _FIELD_LIMIT))
    return _fit("\n".join(parts), PASSPORT_BUDGET) if parts else ""


def build_prompt(queries: Iterable[str], context: str = DEFAULT_CONTEXT,
                 passport: Optional[Dict[str, Any]] = None) -> str:
    """Запрос к модели: продукт + правила + формат ответа, и ФРАЗЫ В КОНЦЕ.

    Порядок частей — не вкусовщина. Кэш модели считает совпадающий префикс, а
    список фраз меняется каждым батчем: стоит он в середине — стабильный текст
    ниже него кэш не увидит ни разу. Поэтому всё постоянное (роль, паспорт,
    правила, формат ответа) идёт до PHRASES_MARKER, а после него только фразы.

    Формат задан явно и с примером, потому что разбор не должен зависеть от
    фантазии модели: свободный текст здесь означает UNCLEAR для всего батча.
    """
    listed = "\n".join(f"- {q}" for q in queries)
    block = passport_block(passport)
    product = f"{block}\n\n" if block else ""
    return (
        "Ты — маркетолог контекстной рекламы. Проект: "
        f"{context}.\n\n"
        f"{product}"
        "Для каждой поисковой фразы из списка в конце реши, что это за интент:\n"
        "  core — человек ищет ровно наш продукт или близкое к нему; такую\n"
        "         фразу нельзя запрещать ни при какой цене;\n"
        "  junk — интент нецелевой (ищут файл, работу, чужой город, чужой\n"
        "         продукт, справку вместо обучения); деньги на неё уходят зря;\n"
        "  unclear — по фразе нельзя судить уверенно.\n\n"
        "Сомневаешься — отвечай unclear: ошибочный junk отрезает живой трафик.\n\n"
        "Ответь ТОЛЬКО валидным JSON, без пояснений вокруг, строго в виде:\n"
        '{"verdicts": [{"query": "<фраза>", "verdict": "core|junk|unclear", '
        '"reason": "<коротко, почему>"}]}\n\n'
        f"{PHRASES_MARKER}\n{listed}"
    )


def parse_response(raw: str) -> Dict[str, Dict[str, Any]]:
    """Ответ модели → {фраза: {verdict, reason}}. Мусор на входе → пустой словарь.

    Терпит обёртку в ```-блок: модели её добавляют, несмотря на просьбу.
    Неизвестный вердикт трактуется как UNCLEAR — набор значений закрытый,
    и «почти core» здесь не бывает.
    """
    if not raw:
        return {}
    text = _FENCE.sub("", str(raw)).strip()
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return {}
    rows = data.get("verdicts") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        query = str(row.get("query") or "").strip().lower()
        if not query:
            continue
        verdict = str(row.get("verdict") or "").strip().lower()
        out[query] = {
            "verdict": verdict if verdict in KNOWN_VERDICTS else UNCLEAR,
            "reason": str(row.get("reason") or "")[:200],
        }
    return out


def classify(
    queries: Iterable[str], ask: Callable[[str], str],
    context: str = DEFAULT_CONTEXT, batch_size: int = BATCH_SIZE,
    passport: Optional[Dict[str, Any]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Вердикты по фразам. Всё, о чём модель не ответила, — UNCLEAR.

    ask — функция «промпт → текст ответа». Инъекция, а не прямой вызов сети:
    так слой тестируется без ключа и без интернета, а замена модели не
    трогает правила применения.

    Исключение любого вызова гасится: смысловой слой — надстройка, и его
    падение не имеет права ронять расчёт, который считает деньги.
    """
    listed = [str(q).strip().lower() for q in queries if str(q).strip()]
    out: Dict[str, Dict[str, Any]] = {
        q: {"verdict": UNCLEAR, "reason": "модель не ответила"} for q in listed}
    for start in range(0, len(listed), batch_size):
        batch = listed[start:start + batch_size]
        try:
            answered = parse_response(
                ask(build_prompt(batch, context, passport=passport)))
        except Exception as exc:  # noqa: BLE001 — см. докстринг
            answered = {}
            for q in batch:
                out[q] = {"verdict": UNCLEAR,
                          "reason": f"слой недоступен: {type(exc).__name__}"}
        for query, verdict in answered.items():
            if query in out:
                out[query] = verdict
    return out


def _read_json(name: str) -> Optional[Dict[str, Any]]:
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    try:
        return json.loads((Path(PASSPORTS_DIR) / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_passport(product: str) -> Optional[Dict[str, Any]]:
    """Проекция паспорта продукта с диска. None — паспорта нет.

    Отсутствие — рабочее состояние, а не авария: паспорт есть там, где
    кампанию собирал билдер. Нет паспорта — слой работает на общем описании,
    ровно как до задачи 20.
    """
    key = str(product or "").strip().lower()
    return _read_json(f"{key}.json") if key else None


def load_index() -> Dict[str, Dict[str, str]]:
    """Карта адресации паспортов. Нет файла — пустые карты, слой без паспортов.

    Читается с диска каждый раз, а не кэшируется в модуле: прогон один, вызов
    один, а кэш пережил бы правку карты в тестах и дал бы зелёное там, где
    боевой прогон читает старое.
    """
    raw = _read_json(PASSPORT_INDEX) or {}
    return {
        "by_campaign": {str(k): str(v) for k, v in
                        (raw.get("by_campaign") or {}).items()},
        "by_direction": {str(k): str(v) for k, v in
                         (raw.get("by_direction") or {}).items()},
    }


def passport_key(campaigns: Iterable[Any], direction: str,
                 index: Optional[Dict[str, Dict[str, str]]] = None) -> str:
    """Каким продуктом судить фразу. Пусто — судить нечем, поедет без паспорта.

    Два уровня разрешения, и порядок между ними не произволен.

    Сначала КАМПАНИЯ: журнал билдера знает, из какого лендинга собрана каждая
    кампания, и точнее этого ответа не существует. Кампании фразы обязаны
    сойтись на ОДНОМ продукте — иначе это та же подмена, ради которой правило
    и написано: паспорт соседнего продукта судит фразу как свою.

    Потом НАПРАВЛЕНИЕ — приближение для кампаний, которых билдер не собирал
    (их в кабинете большинство). В карте направлений стоят только те, что
    описываются одним продуктом; смешанные там отсутствуют, и фраза из них
    честно едет без паспорта.

    Кампания, которой в карте нет, ответ не портит: направление отвечает за
    неё. А вот РАЗНЫЕ продукты у разных кампаний — портит, и это правильно.
    """
    index = index if index is not None else load_index()
    by_campaign = index.get("by_campaign") or {}
    products = {by_campaign[str(c)] for c in (campaigns or ())
                if str(c) in by_campaign}
    if len(products) == 1:
        return products.pop()
    if products:
        return ""
    return (index.get("by_direction") or {}).get(
        str(direction or "").strip().lower(), "")


def group_by_direction(
    candidates: Iterable[Dict[str, Any]], direction_by_campaign: Dict[str, str],
) -> Dict[str, List[str]]:
    """Фразы кандидатов → {продукт: [фразы]}. Спорные — под ключом "".

    Фраза, откручивавшаяся в кампаниях РАЗНЫХ продуктов, паспорта не
    получает. Паспорта соседних продуктов противоречат друг другу ровно там,
    где это опаснее всего: «после 9 класса» у высшего — анти-маркер, у СПО —
    целевой маркер. Взять любой из двух значило бы судить фразу паспортом
    чужого продукта, и вето «core» пришло бы не по делу.

    Продукт разрешается passport_key: сначала по кампании (журнал билдера),
    потом по направлению. Имя функции осталось прежним — его знает Э0 — но
    ключ группы теперь продукт, и это ровно та поправка, ради которой карта
    заведена: в 'dist' живут два разных продукта.
    """
    index = load_index()
    groups: Dict[str, List[str]] = {}
    for candidate in candidates:
        query = str(candidate.get("query") or "").strip().lower()
        if not query:
            continue
        campaigns = list(candidate.get("campaigns") or ())
        seen = {str(direction_by_campaign.get(str(c)) or "") for c in campaigns}
        seen.discard("")
        direction = seen.pop() if len(seen) == 1 else ""
        key = passport_key(campaigns, direction, index)
        listed = groups.setdefault(key, [])
        if query not in listed:
            listed.append(query)
    return groups


def classify_by_direction(
    candidates, ask, direction_by_campaign, context: str = DEFAULT_CONTEXT,
    load=None, batch_size: int = BATCH_SIZE,
):
    """Вердикты по всем кандидатам разом, каждой группе — свой паспорт.

    Возвращает (вердикты, счётчики). Счётчики нужны отчёту: разметка с
    паспортом и без — разного качества, и не видя долей, человек не отличит
    «паспорта не понадобились» от «паспорта не завезли».

    Группировка стоит денег: направлений в кабинете несколько, и каждое —
    свой батч, то есть свой вызов. Это осознанная цена за то, чтобы фразу
    судил паспорт её продукта, а не соседнего.
    """
    load = load or load_passport
    groups = group_by_direction(candidates, direction_by_campaign)
    verdicts: Dict[str, Dict[str, Any]] = {}
    stats = {"with_passport": 0, "without_passport": 0,
             "directions": {}, "passports": []}
    for direction in sorted(groups):
        phrases = groups[direction]
        passport = load(direction) if direction else None
        if passport:
            stats["passports"].append(direction)
            stats["with_passport"] += len(phrases)
        else:
            stats["without_passport"] += len(phrases)
        if direction:
            stats["directions"][direction] = len(phrases)
        verdicts.update(classify(phrases, ask=ask, context=context,
                                 batch_size=batch_size, passport=passport))
    return verdicts, stats


def unclear_reasons(verdicts: Dict[str, Dict[str, Any]]) -> Dict[str, int]:
    """Почему вердикты вышли неопределёнными — счётчик по причинам.

    UNCLEAR приходит тремя разными путями: модель честно ответила «не знаю»,
    ответ не разобрался, вызов упал. Для рычага это одно и то же (вето нет),
    для человека — совершенно разные новости: первое значит, что слой
    работает, а два других — что он молчит, и молчание выглядит как согласие.
    Одна строка счётчиков в отчёте отличает их без чтения логов.
    """
    out: Dict[str, int] = {}
    for verdict in verdicts.values():
        if verdict.get("verdict") != UNCLEAR:
            continue
        reason = str(verdict.get("reason") or "").strip() or "без причины"
        out[reason[:60]] = out.get(reason[:60], 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def keep_minus_candidates(
    candidates: List[Dict[str, Any]], verdicts: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Кандидаты в минус, из которых убраны признанные СВОЕЙ семантикой.

    Вето, а не отбор: UNCLEAR и отсутствие вердикта оставляют кандидата в
    силе — решает экономика, как и до появления слоя.
    """
    kept: List[Dict[str, Any]] = []
    for candidate in candidates:
        query = str(candidate.get("query") or "").strip().lower()
        verdict = verdicts.get(query) or {}
        if verdict.get("verdict") == CORE:
            continue
        kept.append({**candidate, "semantic": verdict.get("verdict", UNCLEAR)})
    return kept


def keep_expansion_candidates(
    candidates: List[Dict[str, Any]], verdicts: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Кандидаты на расширение без тех, что признаны мусорным интентом.

    Зеркало keep_minus_candidates: там вето накладывает "core", здесь —
    "junk". Случай из живых данных: «диплом о высшем образовании купить»
    даёт дешёвые заявки, и арифметика хочет их докупить; смысл видит, что
    это не наш покупатель.
    """
    kept: List[Dict[str, Any]] = []
    for candidate in candidates:
        query = str(candidate.get("query") or "").strip().lower()
        verdict = verdicts.get(query) or {}
        if verdict.get("verdict") == JUNK:
            continue
        kept.append({**candidate, "semantic": verdict.get("verdict", UNCLEAR)})
    return kept


def deepseek_asker(
    api_key: Optional[str] = None, model: str = DEEPSEEK_MODEL,
    timeout: int = 120,
) -> Optional[Callable[[str], str]]:
    """Функция обращения к модели, или None, если ключа нет.

    None — законное состояние: без ключа прогон работает ровно как раньше,
    и это видно в отчёте отдельной строкой, а не молчанием.
    """
    # Ключ чистится от всего, что не печатный ASCII. Заголовки HTTP
    # кодируются в latin-1, и один невидимый символ в значении роняет вызов
    # целиком: слой возвращает UNCLEAR по всему батчу и молчит неотличимо от
    # «ключа нет». Так и случилось: BOM в начале секрета (его добавил
    # конвейер, которым секрет заливали) стоил суток тишины, и по отчёту это
    # выглядело как «модель ни в чём не уверена».
    key = "".join(c for c in str(api_key or os.environ.get("DEEPSEEK_API_KEY") or "")
                  if "!" <= c <= "~")
    if not key:
        return None

    def _ask(prompt: str) -> str:
        import requests

        # Тело кодируется в UTF-8 ЗДЕСЬ, а не отдаётся строкой параметру
        # json=: строку по дороге кодируют в latin-1, и первая же кириллица
        # даёт UnicodeEncodeError. Слой ошибку глотает и возвращает UNCLEAR
        # по всему батчу — то есть молчит ровно так же, как без ключа, и это
        # молчание неотличимо от согласия. Ровно так он и молчал в первом
        # боевом прогоне: 12 вердиктов из 12, все «слой недоступен».
        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            # Ноль температуры: разметка обязана быть воспроизводимой,
            # иначе один и тот же кандидат в разных прогонах получает
            # разные вердикты и рычаг дрожит.
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }, ensure_ascii=False).encode("utf-8")
        response = requests.post(
            "https://api.deepseek.com/chat/completions",
            data=payload,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json; charset=utf-8"},
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]

    return _ask
