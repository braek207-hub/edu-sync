# -*- coding: utf-8 -*-
"""Свёртка строк KZ-Метрики в кортежи lime_stats."""
from sync.lime_kz_metrika import COLUMNS, REGION, build_rows

DIRECT_MAP = {"Поиск. Бренд": ("119566511", True), "ТК муж.": ("706806515", False)}
GOOGLE_MAP = {"23952118304": "PMax Retargeting"}
ADGROUP_MAP = {}
MAPS = (DIRECT_MAP, GOOGLE_MAP, ADGROUP_MAP)

I = {name: i for i, name in enumerate(COLUMNS)}


def _row(**kw):
    base = {
        "date": "2026-07-15", "traffic_source": "ad", "source_engine": "Yandex: Direct",
        "direct_campaign_name": None, "utm_campaign": None, "utm_content": None,
        "visits": 100.0, "users": 80.0, "new_users": 40.0,
        "bounce_rate": 20.0, "page_depth": 5.0,
        "cart_reaches": 30.0, "checkout_reaches": 10.0,
        "orders": 5.0, "revenue": 60000.0,
    }
    base.update(kw)
    return base


def test_revenue_converted_to_rubles_and_region_tagged():
    rows = build_rows([_row()], MAPS, {}, 0.162, "2026-07-15")
    assert len(rows) == 1
    r = rows[0]
    assert r[I["region"]] == REGION == "kz_metrika"
    assert r[I["date"]] == "2026-07-15"
    assert r[I["purchases_revenue"]] == 9720.0     # 60000 тенге × 0.162
    assert r[I["sessions"]] == 100
    assert r[I["new_users"]] == 40


def test_cost_written_only_for_kz_cabinet_campaign():
    cost_map = {("2026-07-15", "119566511"): 4450.0, ("2026-07-15", "706806515"): 99999.0}
    rows = build_rows(
        [_row(direct_campaign_name="Поиск. Бренд"), _row(direct_campaign_name="ТК муж.")],
        MAPS, cost_map, 0.162, "2026-07-15")
    by_campaign = {r[I["campaign_id"]]: r for r in rows}
    assert by_campaign["119566511"][I["cost"]] == 4450.0   # KZ-кабинет → расход переносим
    assert by_campaign["706806515"][I["cost"]] == 0.0      # пролив RU → расход остаётся в RU


def test_unresolved_campaign_rows_collapse_to_channel():
    """Две строки без кампании в одном канале дают одну строку."""
    rows = build_rows([_row(), _row(visits=50.0, orders=1.0, revenue=10000.0)],
                      MAPS, {}, 0.162, "2026-07-15")
    assert len(rows) == 1
    assert rows[0][I["sessions"]] == 150
    assert rows[0][I["purchases_count"]] == 6
    assert rows[0][I["campaign_id"]] == ""


def test_bounce_and_depth_are_visit_weighted_on_collapse():
    """20% на 100 визитов + 40% на 300 → 35% на 400 визитов."""
    rows = build_rows(
        [_row(bounce_rate=20.0, page_depth=4.0, visits=100.0),
         _row(bounce_rate=40.0, page_depth=8.0, visits=300.0)],
        MAPS, {}, 0.162, "2026-07-15")
    assert rows[0][I["sessions"]] == 400
    assert rows[0][I["bounce_rate"]] == 35.0
    assert rows[0][I["page_depth"]] == 7.0


def test_channel_taxonomy_and_traffic_type():
    rows = build_rows([_row(traffic_source="organic", source_engine="Google")],
                      MAPS, {}, 0.162, "2026-07-15")
    assert rows[0][I["channel"]] == "SEO"
    assert rows[0][I["subchannel"]] == "SEO Google"
    assert rows[0][I["traffic_type"]] == "Бесплатный"


def test_paid_row_marked_paid_so_drr_and_cpo_work():
    rows = build_rows([_row(direct_campaign_name="Поиск. Бренд")], MAPS, {}, 0.162, "2026-07-15")
    assert rows[0][I["traffic_type"]] == "Платный"


def test_cost_counted_once_per_campaign_per_day():
    """Расход кампании не задваивается, если её визиты пришли двумя строками каналов.

    Обе строки — одна кампания ("Поиск. Бренд"), но разный traffic_source даёт разный
    channel/subchannel (map_metrika_channel: ad+Yandex → SEM/Яндекс.Директ, direct → Direct/Direct),
    поэтому свёртка build_rows кладёт их в ДВЕ разные группы (channel, subchannel, campaign_id)
    при одной и той же кампании. Так тест реально нагружает множество `spent` в build_rows:
    без него расход кампании попал бы в обе группы и задвоился.
    """
    cost_map = {("2026-07-15", "119566511"): 4450.0}
    rows = build_rows(
        [_row(direct_campaign_name="Поиск. Бренд", traffic_source="ad", source_engine="Yandex: Direct"),
         _row(direct_campaign_name="Поиск. Бренд", traffic_source="direct", source_engine=None)],
        MAPS, cost_map, 0.162, "2026-07-15")
    assert len(rows) == 2  # разные группы свёртки — иначе задвоение расхода скрыто ещё до защиты
    assert sum(r[I["cost"]] for r in rows) == 4450.0


def test_warns_when_paid_google_rows_have_zero_cost(capsys):
    """Google-расход завозит отдельный синк (google_ads_fx); если он отвалится — cost_map
    не получит запись для google-кампании, и расход молча станет 0. Должно быть громко."""
    rows = build_rows(
        [_row(traffic_source="ad", source_engine="Google Ads", utm_campaign="23952118304")],
        MAPS, {}, 0.162, "2026-07-15")
    assert rows[0][I["cost"]] == 0.0
    out = capsys.readouterr().out
    assert "lime_kz_metrika: WARN" in out
    assert "2026-07-15" in out


def test_all_24_columns_mapped_correctly_by_name():
    """Позиционный контракт build_rows: каждое поле COLUMNS проверяется по имени со своим,
    легко различимым значением — случайная перестановка позиций (например cart_reaches
    с checkout_reaches) должна ломать этот тест, а не проходить незамеченной."""
    cost_map = {("2026-07-15", "119566511"): 4450.0}
    rows = build_rows(
        [_row(
            direct_campaign_name="Поиск. Бренд", traffic_source="ad", source_engine="Yandex: Direct",
            visits=111.0, users=82.0, new_users=37.0,
            bounce_rate=45.0, page_depth=6.5,
            cart_reaches=23.0, checkout_reaches=9.0,
            orders=7.0, revenue=40000.0,
        )],
        MAPS, cost_map, 0.3, "2026-07-15")
    assert len(rows) == 1
    r = rows[0]

    expected = {
        "date": "2026-07-15",
        "data_source": "web",
        "region": "kz_metrika",
        "channel": "SEM",
        "subchannel": "Яндекс.Директ",
        "traffic_type": "Платный",
        "campaign_id": "119566511",
        "campaign_name": "Поиск. Бренд",
        "cost": 4450.0,
        "clicks": 0.0,
        "impressions": 0.0,
        "sessions": 111,
        "users": 82,
        "clients": 0,
        "purchases_count": 7,
        "purchases_revenue": 12000.0,   # 40000 тенге × 0.3
        "customers": 0,
        "new_users": 37,
        "new_customers": 0,
        "new_customers_revenue": 0.0,
        "bounce_rate": 45.0,
        "page_depth": 6.5,
        "cart_reaches": 23,
        "checkout_reaches": 9,
    }
    assert set(expected) == set(COLUMNS)  # ни одна колонка не забыта и не выдумана
    for name, value in expected.items():
        assert r[I[name]] == value, f"{name}: expected {value!r}, got {r[I[name]]!r}"


def test_no_warning_when_google_cost_present(capsys):
    """Тот же платный Google, но расход есть — предупреждения быть не должно."""
    cost_map = {("2026-07-15", "23952118304"): 5000.0}
    rows = build_rows(
        [_row(traffic_source="ad", source_engine="Google Ads", utm_campaign="23952118304")],
        MAPS, cost_map, 0.162, "2026-07-15")
    assert rows[0][I["cost"]] == 5000.0
    out = capsys.readouterr().out
    assert "WARN" not in out


# ── Тихий ноль по расходу: нераспознанные ПЛАТНЫЕ визиты ─────────────────────
# Гейт про Google требует непустой campaign_id и ловит лишь сценарий «кампания известна,
# расход не доехал». Все реальные отказы соседей дают ПУСТОЙ campaign_id: протухший
# справочник групп, неоднозначное имя кампании Директа, пустая статистика Google за дату.
# Во всех случаях визиты и заказы на месте, а расход падает — метрики выглядят лучше правды.


def test_warns_when_paid_visits_have_no_resolved_campaign(capsys):
    """Протухший справочник групп: utm_content не резолвится → платный визит без кампании."""
    rows = build_rows(
        [_row(traffic_source="ad", source_engine="Google Ads",
              utm_campaign="g", utm_content="782935363650", visits=4200.0)],
        (DIRECT_MAP, GOOGLE_MAP, {}),   # справочник групп пуст — как сейчас в проде
        {}, 0.162, "2026-07-15")
    assert rows[0][I["campaign_id"]] == ""
    assert rows[0][I["traffic_type"]] == "Платный"
    assert rows[0][I["cost"]] == 0.0

    out = capsys.readouterr().out
    assert "lime_kz_metrika: WARN" in out
    assert "2026-07-15" in out          # дата
    assert "1 строк" in out             # число строк
    assert "4200 визитов" in out        # сумма визитов


def test_warns_when_direct_campaign_name_is_ambiguous(capsys):
    """Кампанию продублировали в кабинете с тем же именем → load_direct_map кладёт None.
    Расход этой кампании исчезает целиком, а предупреждения про Директ не было вообще."""
    maps = ({"Дубль имени": None}, GOOGLE_MAP, ADGROUP_MAP)
    rows = build_rows(
        [_row(direct_campaign_name="Дубль имени", visits=900.0)],
        maps, {("2026-07-15", "119566511"): 5000.0}, 0.162, "2026-07-15")
    assert rows[0][I["campaign_id"]] == ""
    assert rows[0][I["cost"]] == 0.0

    out = capsys.readouterr().out
    assert "lime_kz_metrika: WARN" in out
    assert "900 визитов" in out


def test_unresolved_paid_visits_summed_across_rows(capsys):
    """Несколько групп свёртки — в предупреждении их число и суммарные визиты."""
    rows = build_rows(
        [_row(traffic_source="ad", source_engine="Google Ads", visits=1000.0),
         _row(traffic_source="ad", source_engine="TikTok", visits=250.0)],
        (DIRECT_MAP, GOOGLE_MAP, {}), {}, 0.162, "2026-07-15")
    assert len(rows) == 2
    out = capsys.readouterr().out
    assert "2 строк" in out
    assert "1250 визитов" in out


def test_no_unresolved_warning_for_free_traffic(capsys):
    """Органика без кампании — норма, а не поломка склейки: предупреждать нельзя."""
    build_rows([_row(traffic_source="organic", source_engine="Google", visits=5000.0)],
               (DIRECT_MAP, GOOGLE_MAP, ADGROUP_MAP), {}, 0.162, "2026-07-15")
    assert "WARN" not in capsys.readouterr().out


def test_no_unresolved_warning_when_paid_campaign_resolves(capsys):
    """Кампания распознана и расход есть — тишина."""
    build_rows([_row(direct_campaign_name="Поиск. Бренд", visits=800.0)],
               MAPS, {("2026-07-15", "119566511"): 4450.0}, 0.162, "2026-07-15")
    assert "WARN" not in capsys.readouterr().out
