# -*- coding: utf-8 -*-
"""Смысловой фильтр над кандидатами рычагов (sync/agent/semantic.py).

Статистика видит только деньги и конверсии: фраза с пятью кликами и нулём
конверсий для неё неотличима от фразы, которая просто не набрала объём. На
живых данных 25.08 в этой зоне лежало 43,5 млн ₽ расхода — 96 % всех денег
на запросах без конверсий. Смысл эту границу видит: «скачать реферат» —
мусор при любом объёме, «высшее образование дистанционно» — ядро при любой
цене.

Модель здесь ПРЕДЛАГАЕТ, а решает по-прежнему арифметика: вердикт модели
может только ЗАПРЕТИТЬ действие, но не назначить его.
"""

import json

from sync.agent import semantic
from sync.agent.semantic import (
    UNCLEAR,
    build_prompt,
    classify,
    keep_minus_candidates,
    parse_response,
)


def _ask(payload):
    """Фейковая модель: отвечает строгим JSON, как настоящая."""
    return json.dumps({"verdicts": [
        {"query": "скачать реферат бесплатно", "verdict": "junk",
         "reason": "нецелевой интент: ищут файл, а не обучение"},
        {"query": "высшее образование дистанционно", "verdict": "core",
         "reason": "прямой интент нашего продукта"},
    ]}, ensure_ascii=False)


def test_classify_returns_verdict_per_query():
    out = classify(["скачать реферат бесплатно", "высшее образование дистанционно"],
                   ask=_ask, context="онлайн-образование")
    assert out["скачать реферат бесплатно"]["verdict"] == "junk"
    assert out["высшее образование дистанционно"]["verdict"] == "core"


def test_unanswered_query_is_unclear_not_junk():
    # Модель ответила не про всё — умолчание безопасное: «неясно», а не
    # «мусор». Иначе молчание модели молча запрещало бы трафик.
    out = classify(["фраза без ответа"], ask=_ask, context="онлайн-образование")
    assert out["фраза без ответа"]["verdict"] == UNCLEAR


def test_broken_response_does_not_break_the_run():
    # Модель вернула не JSON. Прогон обязан продолжиться со всеми «неясно»:
    # смысловой слой — надстройка, его отказ не останавливает рычаг.
    out = classify(["фраза"], ask=lambda payload: "извините, не могу",
                   context="онлайн-образование")
    assert out["фраза"]["verdict"] == UNCLEAR


def test_model_can_only_veto_never_add():
    # Ключевое правило: модель отсеивает кандидатов статистики, но своих не
    # добавляет. «junk» без экономического основания — не действие.
    stat = [{"query": "скачать реферат бесплатно", "cost": 5000.0},
            {"query": "высшее образование дистанционно", "cost": 90_000.0}]
    verdicts = classify([c["query"] for c in stat], ask=_ask,
                        context="онлайн-образование")
    kept = keep_minus_candidates(stat, verdicts)
    assert [c["query"] for c in kept] == ["скачать реферат бесплатно"]
    assert all("semantic" in c for c in kept)


def test_unclear_candidates_still_apply():
    # Модель недоступна или промолчала — поведение прежнее: решает статистика.
    stat = [{"query": "какая-то фраза", "cost": 5000.0}]
    kept = keep_minus_candidates(stat, {})
    assert len(kept) == 1


def test_prompt_carries_context_and_queries():
    prompt = build_prompt(["фраза один", "фраза два"], context="онлайн-образование")
    assert "онлайн-образование" in prompt
    assert "фраза один" in prompt and "фраза два" in prompt
    # Формат ответа задан жёстко: разбор не должен зависеть от фантазии модели.
    assert "json" in prompt.lower()


def test_parse_response_tolerates_code_fences():
    raw = '```json\n{"verdicts": [{"query": "a", "verdict": "junk"}]}\n```'
    assert parse_response(raw)["a"]["verdict"] == "junk"


def test_unclear_reasons_separate_silence_from_ignorance():
    # Для рычага все три случая одинаковы — вето нет. Для человека это
    # «слой работает» против «слой молчит, и молчание похоже на согласие».
    verdicts = {
        "а": {"verdict": semantic.UNCLEAR, "reason": "слой недоступен: ReadTimeout"},
        "б": {"verdict": semantic.UNCLEAR, "reason": "слой недоступен: ReadTimeout"},
        "в": {"verdict": semantic.UNCLEAR, "reason": "модель не ответила"},
        "г": {"verdict": semantic.CORE, "reason": "ровно наш продукт"},
    }

    assert semantic.unclear_reasons(verdicts) == {
        "слой недоступен: ReadTimeout": 2, "модель не ответила": 1}


def test_unclear_reasons_are_sorted_by_weight():
    verdicts = {
        "а": {"verdict": semantic.UNCLEAR, "reason": "редкая"},
        "б": {"verdict": semantic.UNCLEAR, "reason": "частая"},
        "в": {"verdict": semantic.UNCLEAR, "reason": "частая"},
    }

    assert list(semantic.unclear_reasons(verdicts)) == ["частая", "редкая"]


def test_verdict_without_a_reason_is_still_counted():
    # Причины нет — строка всё равно обязана быть видна: пропуск сделал бы
    # сумму причин меньше числа unclear, и разница читалась бы как ноль.
    verdicts = {"а": {"verdict": semantic.UNCLEAR}}

    assert semantic.unclear_reasons(verdicts) == {"без причины": 1}


def test_request_body_is_utf8_bytes(monkeypatch):
    # Фразы кириллические все до одной. Тело, ушедшее строкой, по дороге
    # кодируется в latin-1 — UnicodeEncodeError, весь батч становится
    # UNCLEAR, и слой молчит неотличимо от «ключа нет». Так он и молчал в
    # первом боевом прогоне: 12 из 12.
    import requests

    sent = {}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": '{"verdicts": []}'}}]}

    def _fake_post(url, **kwargs):
        sent.update(kwargs)
        return _Response()

    monkeypatch.setattr(requests, "post", _fake_post)

    semantic.deepseek_asker(api_key="ключ-проверки")("скачать реферат бесплатно")

    assert "json" not in sent, "тело обязано уходить готовыми байтами"
    assert isinstance(sent["data"], bytes)
    body = json.loads(sent["data"].decode("utf-8"))
    assert "скачать реферат бесплатно" in body["messages"][0]["content"]


def test_non_ascii_body_survives_a_latin1_only_transport(monkeypatch):
    # Проверка у ПОЛУЧАТЕЛЯ: транспорт, умеющий только latin-1, — это и есть
    # http.client. Байты он пропускает, строку с кириллицей — нет.
    import requests

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "{}"}}]}

    def _latin1_only_post(url, **kwargs):
        body = kwargs.get("data")
        if isinstance(body, str):
            body.encode("latin-1")      # то же исключение, что в проде
        return _Response()

    monkeypatch.setattr(requests, "post", _latin1_only_post)

    semantic.deepseek_asker(api_key="k")("высшее образование дистанционно")


def test_invisible_characters_in_the_key_do_not_break_the_call(monkeypatch):
    # BOM и переводы строки цепляются к секрету от конвейера, которым его
    # заливали. Заголовки HTTP кодируются в latin-1, поэтому один такой
    # символ роняет ВЕСЬ батч, а слой при этом молчит ровно так же, как без
    # ключа. Сутки тишины стоили именно этого.
    import requests

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "{}"}}]}

    def _latin1_headers_post(url, **kwargs):
        for value in (kwargs.get("headers") or {}).values():
            value.encode("latin-1")     # то же, что делает http.client
        return _Response()

    monkeypatch.setattr(requests, "post", _latin1_headers_post)

    semantic.deepseek_asker(api_key="﻿sk-ключ\n".replace("ключ", "abc"))("фраза")


def test_a_key_of_only_invisible_characters_counts_as_no_key():
    # Пустой после чистки ключ — это «ключа нет», а не «ключ есть, но
    # сломан»: отчёт обязан сказать про отсутствие, а не молчать вердиктами.
    assert semantic.deepseek_asker(api_key="﻿\n ") is None


# ======================================================= паспорт продукта
# Контекст одной строкой («онлайн-образование…») судит фразы почти в вакууме:
# по нему «аспирантура дистанционно» неотличима от «высшее дистанционно», хотя
# первое — другой продукт кабинета. Паспорт (builder/passport.py) знает и кто
# наш, и кто НЕ наш, и слова-маркеры чужого интента — 43 позиции на замере
# ВПО-дистанта. Задача 20 плана беты: довезти это до промпта.

PASSPORT = {
    "what": "Дистанционное высшее образование: подбор вуза и сопровождение "
            "поступления, обучение из любого города.",
    "who": "Взрослый 20–40 лет: работает, доучивается заочно.",
    "not_ours": ["Девятиклассник, который выбирает колледж после школы: ему "
                 "нужно СПО, эту ступень ведёт соседняя кампания кабинета",
                 "Человек, которому нужно медицинское образование"],
    "anti_markers": [{"word": "аспирантура", "reason": "другой продукт"},
                     {"word": "автошкола", "reason": "другой продукт"},
                     {"word": "реферат", "reason": "ищут файл, а не обучение"}],
    "target_markers": ["дистанционно", "заочно", "бакалавриат", "вуз"],
    "competitors": ["МФЮА", "Росдистант (ТГУ)"],
}


def _phrases():
    return ["аспирантура дистанционно", "высшее образование заочно"]


def test_passport_reaches_the_prompt():
    # Шаг 1 задачи 20 дословно: паспорт направления доезжает до промпта —
    # и то, что важнее всего, доезжает целиком: кто НЕ наш и слова-маркеры.
    prompt = build_prompt(_phrases(), passport=PASSPORT)
    assert "не подходит" in prompt
    assert PASSPORT["anti_markers"][0]["word"] in prompt
    assert "Девятиклассник" in prompt
    assert "дистанционно" in prompt


def test_a_missing_passport_falls_back_to_the_general_description():
    # Шаг 2: паспорта нет — работаем на общем описании проекта, а не падаем.
    # Паспорт есть не у всех направлений (их десять, а уровней у билдера
    # семь), и отсутствие обязано быть рабочим состоянием, а не аварией.
    prompt = build_prompt(_phrases())
    assert semantic.DEFAULT_CONTEXT in prompt
    assert "аспирантура дистанционно" in prompt


def test_the_passport_does_not_replace_the_verdict_rules():
    # Паспорт добавляет знание о продукте, но правило слоя прежнее: сомнение
    # → unclear. Потеряй промпт эту строку — модель начала бы гадать, а её
    # догадка в позиции «junk» режет живой трафик.
    prompt = build_prompt(_phrases(), passport=PASSPORT)
    assert "unclear" in prompt and "Сомневаешься" in prompt


# ----------------------------------------------------- кэш и порядок частей


def _tail_free(prompt):
    """Всё до списка фраз: та часть, которая обязана быть одинаковой."""
    return prompt.split(semantic.PHRASES_MARKER, 1)[0]


def test_the_variable_part_of_the_prompt_is_the_tail():
    # Шаг 3. Кэш DeepSeek считает совпадающий ПРЕФИКС; при 40 фразах в батче
    # и десятках батчей за прогон это разница между 98 % попаданий и нулём.
    # Значит всё стабильное (роль, паспорт, правила, формат ответа) стоит до
    # списка фраз, а меняется только хвост.
    first = build_prompt(["первая фраза"], passport=PASSPORT)
    second = build_prompt(["вторая фраза", "третья фраза"], passport=PASSPORT)

    assert _tail_free(first) == _tail_free(second)
    assert first.index("первая фраза") > first.index("аспирантура")


def test_the_answer_format_is_stated_before_the_phrases():
    # Формат ответа — самая стабильная часть промпта и раньше стояла ПОСЛЕ
    # списка фраз: любой новый батч сдвигал её и обнулял кэш.
    prompt = build_prompt(_phrases(), passport=PASSPORT)
    assert prompt.index("verdicts") < prompt.index(semantic.PHRASES_MARKER)


def test_a_big_passport_does_not_blow_up_the_prompt():
    # Паспорт целиком — 27 КБ; в промпт едет проекция. Без предела длинный
    # паспорт вытеснил бы фразы за окно модели, и батч вернулся бы пустым.
    big = dict(PASSPORT, anti_markers=[{"word": f"слово{i}", "reason": "мусор"}
                                       for i in range(200)],
               not_ours=[f"не наш номер {i} " * 20 for i in range(50)])
    prompt = build_prompt(_phrases(), passport=big)
    assert len(prompt) < semantic.PASSPORT_BUDGET * 2


# ------------------------------------------------ доставка паспорта в слой


def test_classify_carries_the_passport_into_every_batch():
    seen = []

    def _spy(prompt):
        seen.append(prompt)
        return "{}"

    classify(_phrases(), ask=_spy, context="онлайн-образование",
             passport=PASSPORT, batch_size=1)
    assert len(seen) == 2
    assert all("аспирантура" in p for p in seen)


def test_an_unknown_product_has_no_passport():
    assert semantic.load_passport("продукта-нет") is None


def test_the_passport_of_a_product_is_read_from_disk(tmp_path, monkeypatch):
    (tmp_path / "kolledzh.json").write_text(
        json.dumps(PASSPORT, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(semantic, "PASSPORTS_DIR", tmp_path)
    assert semantic.load_passport("kolledzh")["what"] == PASSPORT["what"]


# --------------------------------------------- адресация паспорта продуктом


def _index(tmp_path, monkeypatch, by_campaign=None, by_direction=None):
    (tmp_path / semantic.PASSPORT_INDEX).write_text(
        json.dumps({"by_campaign": by_campaign or {},
                    "by_direction": by_direction or {}}, ensure_ascii=False),
        encoding="utf-8")
    monkeypatch.setattr(semantic, "PASSPORTS_DIR", tmp_path)


def test_the_campaign_map_beats_the_direction_map(tmp_path, monkeypatch):
    # Журнал билдера знает, из какого лендинга собрана кампания, и точнее
    # ответа не существует. Направление — приближение, и уступает точному.
    _index(tmp_path, monkeypatch,
           by_campaign={"713822188": "distant_vpo"},
           by_direction={"dist": "distant_spo"})
    assert semantic.passport_key(["713822188"], "dist") == "distant_vpo"


def test_a_campaign_outside_the_map_falls_back_to_its_direction(tmp_path,
                                                                monkeypatch):
    # Кампаний, которых билдер не собирал, в кабинете большинство: пустая
    # карта кампаний не должна отнимать у них приближение по направлению.
    _index(tmp_path, monkeypatch, by_direction={"spo": "kolledzh"})
    assert semantic.passport_key(["999"], "spo") == "kolledzh"


def test_a_phrase_across_two_products_gets_no_passport(tmp_path, monkeypatch):
    # Та же подмена, ради которой правило и написано, но на уровне продукта:
    # паспорт соседнего лендинга судил бы фразу как свою.
    _index(tmp_path, monkeypatch,
           by_campaign={"1": "distant_vpo", "2": "distant_spo"},
           by_direction={"dist": "distant_vpo"})
    assert semantic.passport_key(["1", "2"], "dist") == ""


def test_a_mixed_direction_is_absent_from_the_map(tmp_path, monkeypatch):
    # 'dist' накрывает два разных продукта, и приближения у него быть не
    # может: «после 9 класса» — анти-маркер ВПО-дистанта и целевой маркер
    # СПО-дистанта. Отсутствие в карте — решение, а не пробел.
    _index(tmp_path, monkeypatch, by_direction={"spo": "kolledzh"})
    assert semantic.passport_key(["999"], "dist") == ""


def test_the_shipped_index_addresses_every_shipped_passport():
    """Карта и файлы паспортов обязаны сходиться на чекауте.

    Продукт, названный в карте, но без файла, — это тихое «без паспорта»:
    прогон зелёный, разметка слепая. Обратное (файл без адреса) — паспорт,
    до которого не доедет ни одна фраза.
    """
    index = semantic.load_index()
    named = set(index["by_campaign"].values()) | set(index["by_direction"].values())
    assert named, "карта пуста — ни один паспорт недостижим"
    for product in sorted(named):
        assert semantic.load_passport(product) is not None, (
            f"продукт {product} в карте, а файла нет")


# --------------------------------------------- какой паспорт какой фразе


def _candidate(query, campaigns):
    return {"query": query, "campaigns": set(campaigns)}


def test_phrases_are_grouped_by_the_product_of_their_campaigns(tmp_path,
                                                               monkeypatch):
    _index(tmp_path, monkeypatch,
           by_direction={"vpo": "aspirantura", "spo": "kolledzh"})
    groups = semantic.group_by_direction(
        [_candidate("высшее заочно", ["1"]), _candidate("колледж заочно", ["2"])],
        {"1": "vpo", "2": "spo"})
    assert groups["aspirantura"] == ["высшее заочно"]
    assert groups["kolledzh"] == ["колледж заочно"]


def test_a_phrase_spanning_directions_gets_no_passport():
    # «Дистанционно» стоит и в ВПО, и в СПО. Паспорта этих направлений
    # противоречат друг другу ровно там, где это опаснее всего: «после 9
    # класса» у ВПО — анти-маркер, у СПО — целевой маркер. Взять любой из
    # двух значило бы судить фразу паспортом чужого продукта.
    groups = semantic.group_by_direction(
        [_candidate("учиться дистанционно", ["1", "2"])],
        {"1": "vpo", "2": "spo"})
    assert groups[""] == ["учиться дистанционно"]


def test_two_products_inside_one_direction_are_kept_apart(tmp_path, monkeypatch):
    # Дефект, ради которого карта заведена: обе кампании — 'dist', но
    # продукты разные, и прежняя группировка судила бы обе фразы одним
    # паспортом. Кампанийная адресация разводит их по своим.
    _index(tmp_path, monkeypatch,
           by_campaign={"1": "distant_vpo", "2": "distant_spo"})
    groups = semantic.group_by_direction(
        [_candidate("вуз дистанционно", ["1"]),
         _candidate("колледж дистанционно", ["2"])],
        {"1": "dist", "2": "dist"})
    assert groups["distant_vpo"] == ["вуз дистанционно"]
    assert groups["distant_spo"] == ["колледж дистанционно"]


def test_a_phrase_of_an_unknown_campaign_gets_no_passport():
    groups = semantic.group_by_direction([_candidate("что-то", ["999"])], {})
    assert groups[""] == ["что-то"]


# ------------------------------------ разметка кандидатов паспортами разом


def test_candidates_are_classified_with_the_passport_of_their_direction(
        tmp_path, monkeypatch):
    _index(tmp_path, monkeypatch,
           by_direction={"vpo": "vpo", "spo": "spo"})
    seen = {}

    def _spy(prompt):
        # Ключ — фраза из хвоста промпта: по ней видно, с каким паспортом
        # уехал каждый батч.
        tail = prompt.split(semantic.PHRASES_MARKER, 1)[1]
        seen[tail.strip()] = prompt
        return "{}"

    verdicts, stats = semantic.classify_by_direction(
        [_candidate("высшее заочно", ["1"]), _candidate("колледж заочно", ["2"])],
        ask=_spy, direction_by_campaign={"1": "vpo", "2": "spo"},
        load=lambda d: PASSPORT if d == "vpo" else None)

    assert "аспирантура" in seen["- высшее заочно"]
    assert "аспирантура" not in seen["- колледж заочно"]
    assert stats["with_passport"] == 1 and stats["without_passport"] == 1
    assert set(verdicts) == {"высшее заочно", "колледж заочно"}


def test_the_report_names_which_directions_had_a_passport(tmp_path, monkeypatch):
    # Паспорт есть не у всех продуктов, и это влияет на разметку. Молчание
    # тут неотличимо от «паспорта не понадобились».
    _index(tmp_path, monkeypatch, by_direction={"vpo": "vpo"})
    _, stats = semantic.classify_by_direction(
        [_candidate("высшее заочно", ["1"])], ask=lambda p: "{}",
        direction_by_campaign={"1": "vpo"}, load=lambda d: PASSPORT)
    assert stats["directions"] == {"vpo": 1}
    assert stats["passports"] == ["vpo"]


def test_the_passport_block_keeps_its_line_breaks():
    # Усечение по общему пределу не имеет права схлопывать переносы: слипшись
    # в один абзац, разделы теряют границы, и «кому не подходит» читается как
    # продолжение описания продукта.
    block = semantic.passport_block(PASSPORT)
    assert "\nКому наш продукт не подходит:" in block
    assert block.count("\n  - ") == len(PASSPORT["not_ours"])


def test_the_shipped_passport_is_in_the_checkout():
    """Паспорт «Школы» должен доезжать до прогона, а прогон идёт в Actions.

    Проверка не декоративная: в .gitignore репозитория стоит правило на все
    файлы .json, и паспорт по умолчанию не коммитится вовсе. Локально при этом
    всё зелено — файл лежит на диске. Тест падает именно там, где дефект и
    проявляется: на чекауте, где есть только версионированное.
    """
    passport = semantic.load_passport("online_school")
    assert passport is not None, (
        "sync/agent/passports/online_school.json не в чекауте — .gitignore")
    assert semantic.passport_block(passport)
    assert semantic.load_index()["by_direction"].get("school") == "online_school"
