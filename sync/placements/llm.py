# -*- coding: utf-8 -*-
"""Второй судья чистильщика: модель смотрит то, что словарь оставил.

Словарь в classify.py узнаёт мусор по форме имени — bundle id, обменник,
игровой токен. Мимо него проходит мусор, который выглядит как обычный сайт:
дорвеи, склейки новостей, кликбейт-помойки, сайты-однодневки под чужим
брендом. Их отличает не форма имени, а знание о том, что это за ресурс, —
ровно то, чего у словаря нет и быть не может.

Слой судит ТОЛЬКО имена с вердиктом «site»: приложения и обменники уже
отрезаны словарём, и переспрашивать про них незачем. Крупные площадки
защищены списком PROTECTED — по решению Павла (06.09.2026) их не трогаем
вовсе, и мнение модели этого решения не отменяет.

Ключа нет — слой молча выключен, чистильщик работает как раньше. Это
законное состояние, и оно видно в выводе отдельной строкой.
"""

import json
import os
import re
from typing import Callable, Dict, Iterable, List, Optional, Tuple

# Сколько имён уходит в один запрос. Крупнее — дешевле по вызовам, но длинный
# ответ модель чаще обрывает, а оборванный JSON стоит вердиктов всему батчу.
BATCH_SIZE = 40

# Сколько НОВЫХ имён такт спрашивает у модели. Всё, что она уже судила, лежит
# в вечном кэше и денег не стоит; потолок ограничивает только первые прогоны
# и всплески. Остаток не теряется — попадёт в следующий час.
ASK_LIMIT = 60

# Площадки, которые модель резать не вправе. Решение по ним за человеком:
# mail.ru у EDU сливает миллионы, но это не мусор, а вопрос ставок и таргета.
# Сравнение идёт по доменному хвосту, поэтому news.mail.ru тоже защищён.
PROTECTED = (
    "mail.ru", "matchtv.ru", "mirtesen.ru", "dzen.ru", "yandex.ru",
    "ya.ru", "vk.com", "ok.ru", "rambler.ru", "lenta.ru", "ria.ru",
    "rbc.ru", "kp.ru", "aif.ru", "gazeta.ru", "iz.ru", "tass.ru",
    "sport-express.ru", "championat.com", "gismeteo.ru", "pogoda.ru",
    "avito.ru", "auto.ru", "drom.ru", "ozon.ru", "wildberries.ru",
    "kinopoisk.ru", "hh.ru", "sberbank.ru", "gosuslugi.ru", "2gis.ru",
    "pikabu.ru", "drive2.ru", "e1.ru", "ngs.ru", "irr.ru", "youla.ru",
)

PROMPT = """Ты чистишь рекламные площадки Рекламной сети Яндекса.

Реши по каждому домену, стоит ли показывать на нём рекламу.

junk — резать. Это:
- дорвеи, сателлиты, сайты-однодневки, автосгенерированный контент
- пиратские онлайн-кинотеатры, торренты, варез, взлом, читы
- гадания, гороскопы, «народная медицина», заговоры, эзотерика
- знакомства, интим, азартные игры, ставки, займы «до зарплаты»
- агрегаторы кликбейта и «шок-новостей», накрутка, обои/аватарки
- прокси, VPN, «ускорители телефона», сомнительные загрузки софта

keep — оставить. Это:
- реальные СМИ, порталы, форумы, сервисы, магазины, сайты компаний
- нишевые, но настоящие тематические сайты и блоги
- всё, чего ты не знаешь, и всё, в чём сомневаешься

Правило разрешения спора: сомневаешься — keep. Ошибка «оставили мусор»
стоит несколько сотен рублей и правится следующим тактом. Ошибка «зарезали
живую площадку» отнимает конверсии и замечается через недели.

Ответ — JSON: {"items": [{"site": "<домен>", "verdict": "junk|keep",
"why": "<до 6 слов по-русски>"}]}. По одному объекту на каждый домен.

Домены:
%s"""


def is_protected(site: str) -> bool:
    site = (site or "").strip().lower().strip(".")
    return any(site == p or site.endswith("." + p) for p in PROTECTED)


def _parse(text: str) -> Dict[str, Tuple[str, str]]:
    """Ответ модели → вердикты. Мусор в ответе не должен ронять такт."""
    if not text:
        return {}
    # Модель иногда оборачивает JSON в ```json … ```
    body = re.sub(r"^```(?:json)?|```$", "", text.strip(),
                  flags=re.MULTILINE).strip()
    try:
        data = json.loads(body)
    except ValueError:
        return {}
    out: Dict[str, Tuple[str, str]] = {}
    for item in (data.get("items") or []):
        if not isinstance(item, dict):
            continue
        site = str(item.get("site") or "").strip().lower()
        verdict = str(item.get("verdict") or "").strip().lower()
        if not site or verdict not in ("junk", "keep"):
            continue
        why = str(item.get("why") or "").strip()[:80]
        out[site] = (verdict, why)
    return out


def judge(sites: Iterable[str],
          ask: Optional[Callable[[str], str]] = None,
          batch_size: int = BATCH_SIZE,
          errors: Optional[List[str]] = None) -> Dict[str, Tuple[str, str]]:
    """Имена → вердикт модели. Пустой ответ значит «слой промолчал».

    Молчание — не согласие: имя без вердикта чистильщик не режет, оно просто
    останется на следующий такт. Но молчание обязано быть слышно: список
    errors собирает причины, иначе мёртвый провайдер выглядит как «мусора
    нет». Ровно так и вышло 07.09.2026 — DeepSeek отвечал 402 Payment
    Required, а такт рапортовал «к запрету 0».
    """
    names = []
    seen = set()
    for site in sites:
        site = (site or "").strip().lower()
        if site and site not in seen and not is_protected(site):
            seen.add(site)
            names.append(site)
    if not names or ask is None:
        return {}

    out: Dict[str, Tuple[str, str]] = {}
    for start in range(0, len(names), batch_size):
        batch = names[start:start + batch_size]
        try:
            answer = ask(PROMPT % "\n".join(batch))
        except Exception as err:
            # Недоступная модель не повод ронять чистку словарём, но и
            # прятать её отказ нельзя.
            if errors is not None:
                errors.append(str(err)[:200])
            continue
        parsed = _parse(answer)
        if not parsed and errors is not None:
            errors.append("ответ без вердиктов: %s" % (answer or "")[:120])
        for site, verdict in parsed.items():
            if site in seen:
                out[site] = verdict
    return out


def overrides_from(verdicts: Dict[str, Tuple[str, str]]) -> Dict[str, Tuple[str, str]]:
    """Вердикты модели → поправки к словарю (только «резать»).

    «keep» в поправки не идёт: словарь уже сказал «site», то есть не режем,
    и подтверждать это отдельной записью нечего.
    """
    return {site: ("llm", "модель: %s" % (why or "мусорный сайт"))
            for site, (verdict, why) in verdicts.items()
            if verdict == "junk" and not is_protected(site)}


def anthropic_asker(model: str = "claude-haiku-4-5-20251001",
                    timeout: int = 120):
    """Запасной провайдер. None, если ключа нет.

    Нужен не «на будущее»: основной уже отвечал 402 Payment Required, и слой
    при этом молчит целиком. Ключ чистится от непечатных символов —
    заголовки HTTP кодируются в latin-1, и один BOM роняет вызов.
    """
    key = "".join(c for c in str(os.environ.get("ANTHROPIC_API_KEY") or "")
                  if "!" <= c <= "~")
    if not key:
        return None

    def _ask(prompt: str) -> str:
        import requests

        payload = json.dumps({
            "model": model,
            "max_tokens": 2000,
            "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
        }, ensure_ascii=False).encode("utf-8")
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={"x-api-key": key,
                     "anthropic-version": "2023-06-01",
                     "Content-Type": "application/json; charset=utf-8"},
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        return "".join(block.get("text", "")
                       for block in data.get("content") or [])

    return _ask


def asker() -> Optional[Callable[[str], str]]:
    """Клиент модели или None, если ключей нет.

    DeepSeek первым — он дешевле; клиент берётся готовый из семантического
    слоя агента, где уже пережил BOM в секрете и кириллицу в теле запроса.
    """
    from sync.agent.semantic import deepseek_asker
    return deepseek_asker() or anthropic_asker()


def model_name(ask=None) -> str:
    from sync.agent.semantic import DEEPSEEK_MODEL
    if ask is None:
        return "нет"
    if os.environ.get("DEEPSEEK_API_KEY"):
        return DEEPSEEK_MODEL
    return "claude-haiku-4-5"
