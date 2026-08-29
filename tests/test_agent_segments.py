import pytest
import sync.agent.segments as segments
from sync.agent.segments import _stamp_report_name
from sync.agent.writer import plan


def _payload(fields, goals=None):
    params = {
        "SelectionCriteria": {"DateFrom": "2026-05-16", "DateTo": "2026-08-14"},
        "FieldNames": fields,
        "ReportName": "agent-device",
        "ReportType": "CUSTOM_REPORT",
    }
    if goals:
        params["Goals"] = goals
    return {"params": params}


def test_name_is_deterministic_for_same_params():
    a, b = _payload(["Device", "Clicks"]), _payload(["Device", "Clicks"])
    _stamp_report_name(a)
    _stamp_report_name(b)
    assert a["params"]["ReportName"] == b["params"]["ReportName"]


def test_name_changes_when_params_change():
    # Директ помнит связку имя↔параметры: тот же ReportName с другими параметрами
    # отвергается ошибкой 4000.
    without = _payload(["Device", "Clicks"])
    with_goals = _payload(["Device", "Clicks", "Conversions"], goals=["123"])
    _stamp_report_name(without)
    _stamp_report_name(with_goals)
    assert without["params"]["ReportName"] != with_goals["params"]["ReportName"]


def test_name_keeps_readable_prefix():
    p = _payload(["Device"])
    _stamp_report_name(p)
    assert p["params"]["ReportName"].startswith("agent-device-")


def test_stamping_twice_keeps_stable_prefix():
    # Повторный вызов не должен ломать формат (хеш пересчитается от нового имени).
    p = _payload(["Device"])
    _stamp_report_name(p)
    first = p["params"]["ReportName"]
    _stamp_report_name(p)
    assert p["params"]["ReportName"].startswith(first)


# --------------- цели кабинета: без них Reports API не отдаёт Conversions
# Прогон 32406152097: цели брались ТОЛЬКО из секрета DIRECT_CLIENTS_JSON, там их
# не было, Conversions в запрос не попадала, и все двенадцать сегментных срезов
# отказали с «в срезе нет ни одной конверсии». Агент оказался слеп молча — по
# логу это выглядело как «данных нет», а не как «мы их не спросили».

def test_priority_goals_are_extracted_from_the_strategy():
    campaign = {"TextCampaign": {"BiddingStrategy": {"Search": {
        "AverageCpa": {"PriorityGoals": [{"GoalId": 111}, {"GoalId": 222}]}}}}}
    assert segments.goal_ids_from_campaign(campaign) == [111, 222]


def test_single_goal_strategies_are_extracted_too():
    # MaximumConversionRate / PayForConversion несут одиночный GoalId, а не список.
    campaign = {"TextCampaign": {"BiddingStrategy": {"Network": {
        "PayForConversion": {"GoalId": 333, "Cpa": 1000}}}}}
    assert segments.goal_ids_from_campaign(campaign) == [333]


def test_goals_are_collected_across_campaign_types_and_channels():
    # Кампании трёх типов приходят в одном ответе разными ключами; цель могла
    # быть задана только в одном канале, и потерять его нельзя.
    campaign = {
        "TextCampaign": {"BiddingStrategy": {
            "Search": {"AverageCpa": {"PriorityGoals": [{"GoalId": 1}]}},
            "Network": {"AverageCpa": {"PriorityGoals": [{"GoalId": 2}]}}}},
        "UnifiedCampaign": {"BiddingStrategy": {
            "Search": {"AverageCpa": {"PriorityGoals": [{"GoalId": 3}]}}}},
        "MobileAppCampaign": {"BiddingStrategy": {
            "Network": {"PayForConversion": {"GoalId": 4}}}},
    }
    assert segments.goal_ids_from_campaign(campaign) == [1, 2, 3, 4]


def test_goals_are_deduplicated_and_ordered():
    # Одна цель у разных кампаний и каналов — обычное дело. Порядок обязан быть
    # стабилен: цели уходят в параметр запроса, от которого зависит имя отчёта.
    campaign = {"TextCampaign": {"BiddingStrategy": {
        "Search": {"AverageCpa": {"PriorityGoals": [{"GoalId": 9}, {"GoalId": 5}]}},
        "Network": {"AverageCpa": {"PriorityGoals": [{"GoalId": 5}]}}}}}
    assert segments.goal_ids_from_campaign(campaign) == [5, 9]


def test_campaign_without_goals_yields_nothing():
    assert segments.goal_ids_from_campaign({"TextCampaign": {"BiddingStrategy": {
        "Search": {"HighestPosition": {}}}}}) == []
    assert segments.goal_ids_from_campaign({}) == []


def test_account_goals_request_asks_for_strategies_of_all_campaign_types(monkeypatch):
    # Форма запроса взята из рабочего edu_direct_settings: три типа кампаний
    # запрашиваются тремя *FieldNames сразу. Запросишь только Id — стратегия не
    # придёт вовсе, и целей не будет ни одной.
    seen = {}

    def fake_post(url, login, payload, what):
        seen.update({"url": url, "login": login, "payload": payload, "what": what})
        return {"Campaigns": [{"Id": 1, "TextCampaign": {"BiddingStrategy": {
            "Search": {"AverageCpa": {"PriorityGoals": [{"GoalId": 77}]}}}}}]}

    monkeypatch.setattr(segments, "_api_post", fake_post)
    assert segments.fetch_account_goal_ids("cab") == [77]

    params = seen["payload"]["params"]
    assert params["FieldNames"] == ["Id"]
    for key in ("TextCampaignFieldNames", "UnifiedCampaignFieldNames",
                "MobileAppCampaignFieldNames"):
        assert params[key] == ["BiddingStrategy"], key
    assert seen["login"] == "cab"


def test_account_goals_walk_all_pages(monkeypatch):
    # Кабинет EDU — сотни кампаний. Оборвись обход на первой странице, цели
    # поздних кампаний потерялись бы, а отчёт всё равно выглядел бы успешным.
    pages = [
        {"Campaigns": [{"Id": i, "TextCampaign": {"BiddingStrategy": {"Search": {
            "AverageCpa": {"PriorityGoals": [{"GoalId": 10}]}}}}}
            for i in range(segments.PAGE_LIMIT)]},
        {"Campaigns": [{"Id": 9999, "TextCampaign": {"BiddingStrategy": {"Search": {
            "AverageCpa": {"PriorityGoals": [{"GoalId": 20}]}}}}}]},
    ]
    calls = []

    def fake_post(url, login, payload, what):
        calls.append(payload["params"]["Page"]["Offset"])
        return pages[len(calls) - 1]

    monkeypatch.setattr(segments, "_api_post", fake_post)
    assert segments.fetch_account_goal_ids("cab") == [10, 20]
    assert calls == [0, segments.PAGE_LIMIT]


# --------------- колонки конверсий: их имена не «Conversions»
# Боевой отчёт 32556586408 (кабинет account10, 06-20.08):
#   Clicks  Cost  Conversions_330070378_LSCCD  Conversions_330389387_LSCCD ...
#   95515   6041988.26   2753   60   2826   2753
# Парсер читал rec["Conversions"], получал None → 0, и расчёт докладывал «в
# срезе нет ни одной конверсии» при 2826 конверсиях в том же ответе. Так на
# прогоне 32469160289 отказали ВСЕ двенадцать срезов четырёх кабинетов.

def _rec(device, clicks, **conv):
    row = {"Device": device, "Clicks": str(clicks), "Cost": "100.0"}
    row.update({k: str(v) for k, v in conv.items()})
    return row


def test_conversion_columns_are_found_by_prefix():
    records = [_rec("MOBILE", 10, Conversions_111_LSCCD=5, Conversions_222_LSCCD=3)]
    assert segments.conversion_columns(records) == [
        "Conversions_111_LSCCD", "Conversions_222_LSCCD"]


def test_bare_conversions_column_is_not_mistaken_for_a_goal():
    # Колонки с голым именем в ответе не бывает; появись она — это не цель.
    assert segments.conversion_columns([{"Conversions": "7"}]) == []


def test_primary_goal_is_the_most_massive_one():
    # Самая массовая цель кабинета — основное целевое действие, то есть заявка.
    records = [_rec("MOBILE", 100, Conversions_111_LSCCD=2271, Conversions_222_LSCCD=23),
               _rec("DESKTOP", 50, Conversions_111_LSCCD=452, Conversions_222_LSCCD=31)]
    assert segments.primary_goal_column(records) == "Conversions_111_LSCCD"


def test_goals_are_not_summed_because_they_overlap():
    """Суммировать колонки нельзя: цели пересекаются.

    В боевом отчёте 330070378 и 369313502 дали ОДИНАКОВЫЕ числа во всех трёх
    моделях атрибуции — одно целевое действие под двумя идентификаторами.
    Сумма учла бы его дважды и раздула конверсионность.
    """
    records = [_rec("MOBILE", 100, Conversions_330070378_LSCCD=2753,
                    Conversions_369313502_LSCCD=2753, Conversions_338972879_LSCCD=2826)]
    rows_conv = segments._cell_int(
        records[0][segments.primary_goal_column(records)])

    assert rows_conv == 2826
    assert rows_conv < 2753 + 2753 + 2826


def test_primary_goal_is_picked_once_for_the_whole_slice():
    # Построчный выбор сравнивал бы сегменты по РАЗНЫМ целям, и «мобильные
    # конверсионнее» означало бы всего лишь «у них другая цель».
    records = [_rec("MOBILE", 100, Conversions_111_LSCCD=10, Conversions_222_LSCCD=900),
               _rec("DESKTOP", 100, Conversions_111_LSCCD=800, Conversions_222_LSCCD=5)]
    column = segments.primary_goal_column(records)

    # 111 в сумме 810, 222 в сумме 905 — побеждает 222 для ОБОИХ сегментов.
    assert column == "Conversions_222_LSCCD"


def test_tie_is_broken_deterministically():
    # Имя отчёта и его кеш на стороне API зависят от параметров запроса:
    # плавающий выбор дал бы разные числа на одинаковых данных.
    records = [_rec("MOBILE", 10, Conversions_999_LSCCD=5, Conversions_111_LSCCD=5)]
    assert segments.primary_goal_column(records) == "Conversions_111_LSCCD"


def test_no_data_dashes_are_zero_not_a_crash():
    # «Нет данных» Директ пишет двумя дефисами: SMART_TV в боевом ответе шёл
    # строкой «--». int("--") — исключение, и весь срез потерялся бы.
    assert segments._cell_int("--") == 0
    assert segments._cell_int("") == 0
    assert segments._cell_int(None) == 0
    assert segments._cell_int("2753") == 2753


def test_slice_without_any_conversion_columns_yields_zero():
    # Целей не передали — колонок нет. Это ноль, а не падение.
    assert segments.primary_goal_column([_rec("MOBILE", 10)]) is None


def test_all_zero_conversions_mean_no_primary_goal():
    # Колонки есть, но пустые: выбирать нечего, и нули не должны выглядеть
    # как осмысленный выбор цели.
    assert segments.primary_goal_column(
        [_rec("MOBILE", 10, Conversions_111_LSCCD=0)]) is None


def test_segment_report_carries_real_conversions_end_to_end(monkeypatch):
    """Сквозная половина: конверсии обязаны доехать до строк среза.

    Проверять только primary_goal_column недостаточно — ровно так дефект и
    выживал: разбор считал верно, а тело fetch_segment_report продолжало
    читать несуществующую колонку "Conversions". Здесь подменяется сырой TSV
    боевого вида, а утверждения — о возвращённых строках.
    """
    tsv = (
        "Device\tClicks\tCost\tImpressions"
        "\tConversions_330070378_LSCCD\tConversions_338972879_LSCCD\n"
        "DESKTOP\t17158\t1091405.31\t200000\t452\t442\n"
        "MOBILE\t76719\t4859450.90\t900000\t2271\t2354\n"
        "SMART_TV\t5\t83.57\t50\t--\t--\n"
    )
    monkeypatch.setattr(segments, "_run_report", lambda login, payload: tsv)

    rows, goal = segments.fetch_segment_report(
        "cab", "device", "2026-08-06", "2026-08-20",
        goals=["330070378", "338972879"])

    by_key = {r["segment_key"]: r for r in rows}
    # Победила 338972879 (442 + 2354 = 2796 против 2723) — и она же применена
    # к ОБОИМ сегментам, иначе сравнение шло бы по разным целям.
    assert by_key["MOBILE"]["conversions"] == 2354
    assert by_key["DESKTOP"]["conversions"] == 442
    # «--» это ноль, а не падение и не потеря строки.
    assert by_key["SMART_TV"]["conversions"] == 0
    assert by_key["MOBILE"]["clicks"] == 76719
    # Выбор цели обязан быть ВИДЕН: идентификатор и объём — единственный способ
    # заметить, что корректировки посчитаны не по заявке.
    assert goal == {"goal_column": "Conversions_338972879_LSCCD",
                    "conversions": 2796, "columns_offered": 2}


def test_search_queries_carry_real_conversions_too(monkeypatch):
    # По этим числам отбираются кандидаты в минус-слова: нули у всех запросов
    # означали бы «весь расход бесполезен», то есть предложение отминусовать
    # работающую семантику.
    tsv = (
        "CampaignId\tQuery\tCriteria\tCost\tClicks\tConversions_111_LSCCD\n"
        "555\tкупить диплом\tдиплом\t1000.0\t50\t7\n"
        "555\tчто такое вуз\tвуз\t900.0\t40\t--\n"
    )
    monkeypatch.setattr(segments, "_run_report", lambda login, payload: tsv)

    rows, goal = segments.fetch_search_queries("cab", "2026-08-06", "2026-08-20",
                                               goals=["111"])

    by_query = {r["query"]: r for r in rows}
    assert by_query["купить диплом"]["conversions"] == 7
    assert by_query["что такое вуз"]["conversions"] == 0
    assert goal == {"goal_column": "Conversions_111_LSCCD",
                    "conversions": 7, "columns_offered": 1}


# ------------------- устойчивость чтения отчётов к обрывам транспорта


def test_api_post_retries_transport_errors(monkeypatch):
    # Обрыв соединения при чтении отчёта уронил боевой прогон Э0 целиком
    # (run 32730235917: ChunkedEncodingError «Response ended prematurely»).
    # Чтение идемпотентно — его можно и нужно повторить.
    import requests as _requests
    calls = {"n": 0}

    class _Resp:
        encoding = "utf-8"
        @staticmethod
        def json():
            return {"result": {"ok": True}}

    def _post(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _requests.exceptions.ChunkedEncodingError("Response ended prematurely")
        return _Resp()

    monkeypatch.setattr(segments.requests, "post", _post)
    monkeypatch.setattr(segments.time, "sleep", lambda *_: None)
    monkeypatch.setattr(segments, "_api_headers", lambda login: {})

    out = segments._api_post("http://x", "acc", {"method": "get"}, "test")
    assert out == {"ok": True}
    assert calls["n"] == 3


def test_api_post_gives_up_after_the_last_attempt(monkeypatch):
    import requests as _requests

    def _post(*a, **k):
        raise _requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(segments.requests, "post", _post)
    monkeypatch.setattr(segments.time, "sleep", lambda *_: None)
    monkeypatch.setattr(segments, "_api_headers", lambda login: {})

    with pytest.raises(_requests.exceptions.ConnectionError):
        segments._api_post("http://x", "acc", {"method": "get"}, "test")


# ------------------- площадки РСЯ (Э3.7)


def test_fetch_placements_asks_for_the_right_report(monkeypatch):
    # Площадки живут в отдельном отчёте (PLACEMENT_PERFORMANCE_REPORT): в
    # сегментных срезах их нет, и рычаг запрета площадок без него не построить.
    seen = {}

    def _run_report(login, payload):
        seen["payload"] = payload
        return ("CampaignId\tPlacement\tAdNetworkType\tCost\tClicks\tImpressions\t"
                "Conversions_360811375_LSCCD\n"
                "111\tsome.site.ru\tAD_NETWORK\t9000\t120\t5000\t0\n"
                "111\tanother.site\tAD_NETWORK\t500\t10\t900\t2\n")

    monkeypatch.setattr(segments, "_run_report", _run_report)
    rows, goal = segments.fetch_placements("acc", "2026-07-01", "2026-08-11",
                                           goals=["360811375"])
    params = seen["payload"]["params"]
    # Тип отчёта — CUSTOM_REPORT: PLACEMENT_PERFORMANCE_REPORT в API v5 не
    # существует, и боевой прогон получал 8000 «неверное значение перечисления».
    assert params["ReportType"] == "CUSTOM_REPORT"
    assert "Placement" in params["FieldNames"]
    assert rows[0]["placement"] == "some.site.ru"
    assert rows[0]["cost"] == 9000.0
    assert rows[0]["conversions"] == 0
    assert rows[1]["conversions"] == 2
    assert goal["conversions"] == 2


def test_fetch_placements_keeps_only_network_traffic(monkeypatch):
    # Поисковые строки того же отчёта площадкой не являются: запрещать
    # «поиск Яндекса» нечем и незачем.
    monkeypatch.setattr(segments, "_run_report", lambda login, payload: (
        "CampaignId\tPlacement\tAdNetworkType\tCost\tClicks\tImpressions\n"
        "111\tyandex.ru\tSEARCH\t9000\t120\t5000\n"
        "111\tsome.site.ru\tAD_NETWORK\t9000\t120\t5000\n"))
    rows, _ = segments.fetch_placements("acc", "2026-07-01", "2026-08-11")
    assert [r["placement"] for r in rows] == ["some.site.ru"]


def test_region_slice_is_keyed_by_numeric_id_with_name_alongside(monkeypatch):
    """Ключ региона — RegionId, имя едет рядом.

    RegionalAdjustment в API записи требует число, а срез отдавал «Москва» —
    региональные корректировки не применялись вовсе (167 отказов на прогоне
    29.08.2026). TargetingLocationId проверен probe-ом run 33248004571.
    """
    captured = {}

    def _fake(login, payload):
        captured["fields"] = payload["params"]["FieldNames"]
        return (
            "TargetingLocationId\tTargetingLocationName\tClicks\tCost\tImpressions\n"
            "213\tМосква\t1000\t50000.00\t20000\n"
            "1\tМосковская область\t400\t18000.00\t9000\n"
        )

    monkeypatch.setattr(segments, "_run_report", _fake)
    rows, _goal = segments.fetch_segment_report(
        "cab", "region", "2026-08-06", "2026-08-20")

    # Оба поля в одном запросе: отдельный проход за именами удвоил бы отчёты.
    assert captured["fields"][:2] == ["TargetingLocationId",
                                      "TargetingLocationName"]
    by_key = {r["slice_key"]: r for r in rows}
    assert by_key["213"]["slice_label"] == "Москва"
    assert by_key["213"]["clicks"] == 1000
    # Ключ проходит писателя: до правки plan отбивал его как нечисловой.
    assert plan.direct_type_for("bid_modifier:region", "213")[0] == \
        "REGIONAL_ADJUSTMENT"


def test_non_region_slices_have_no_label(monkeypatch):
    """У остальных срезов ключ сам себе имя — второе поле не запрашивается."""
    captured = {}

    def _fake(login, payload):
        captured["fields"] = payload["params"]["FieldNames"]
        return "Device\tClicks\tCost\tImpressions\nMOBILE\t10\t100.0\t50\n"

    monkeypatch.setattr(segments, "_run_report", _fake)
    rows, _goal = segments.fetch_segment_report(
        "cab", "device", "2026-08-06", "2026-08-20")
    assert "TargetingLocationName" not in captured["fields"]
    assert rows[0]["slice_label"] == ""
