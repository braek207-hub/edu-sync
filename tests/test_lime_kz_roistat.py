# -*- coding: utf-8 -*-
"""Свёртка строк Роистата в кортежи lime_stats (region='kz_roistat')."""
from sync.lime import FOREIGN_REGIONS
from sync.lime_kz_roistat import COLUMNS, REGION, build_rows

FX = 0.15158  # ₽ за ₸, курс ЦБ июня
I = {name: i for i, name in enumerate(COLUMNS)}


def api_row(channel, **kw):
    row = {"channel": channel, "level2_id": "", "level2": "", "level3_id": "", "level3": "",
           "visits": 0.0, "leads": 0.0, "paid_leads": 0.0,
           "paid_revenue": 0.0, "progress_revenue": 0.0, "canceled_revenue": 0.0,
           "cost": 0.0, "paid_clients": 0.0, "canceled_leads": 0.0}
    row.update(kw)
    return row


def col(row, name):
    return row[I[name]]


def test_region_registered_so_lime_sync_does_not_wipe_it():
    """sync/lime.py удаляет всё, кроме перечисленного. Пропуск = тихая потеря среза."""
    assert REGION == "kz_roistat"
    assert REGION in FOREIGN_REGIONS


def test_revenue_converted_from_tenge():
    rows = build_rows([api_row("SEO", visits=100, leads=10, paid_leads=8,
                               paid_revenue=1_000_000)], FX, {}, "2026-06-18")
    assert round(col(rows[0], "net_revenue")) == round(1_000_000 * FX)


def test_gross_revenue_includes_canceled_and_in_progress():
    """purchases_* — созданные заказы, значит и выручка по ним должна быть полной."""
    rows = build_rows([api_row("SEO", leads=10, paid_leads=6, paid_revenue=600.0,
                               progress_revenue=300.0, canceled_revenue=100.0)],
                      FX, {}, "2026-06-18")
    # Деньги пишем с точностью до копеек — сравниваем в той же точности.
    assert col(rows[0], "purchases_revenue") == round(1000.0 * FX, 2)
    assert col(rows[0], "net_revenue") == round(600.0 * FX, 2)
    assert col(rows[0], "purchases_count") == 10
    assert col(rows[0], "net_purchases_count") == 6


def test_direct_cost_comes_from_cabinet_by_campaign():
    """Валютная ловушка: расход Директа Роистат отдаёт в рублях под видом тенге.

    Июнь: Роистат 1 398 441, кабинет LIME-KZ1 1 397 767 ₽ — одно и то же число.
    Умножив его на курс, мы занизили бы расход Директа в 6.6 раза. Берём из кабинета,
    причём по campaign_id — иначе расход не разложится по кампаниям.
    """
    rows = build_rows(
        [api_row("Яндекс.Директ 1", level2_id="context", level2="РСЯ",
                 level3_id="117776765", level3="Смарт баннеры CPO",
                 visits=7418, cost=75_768.485)],
        FX, {"117776765": 487_143.0}, "2026-06-18")
    assert col(rows[0], "cost") == 487_143.0


def test_direct_campaign_without_cabinet_cost_gets_zero_not_roistat_number():
    """Нет кампании в кабинете — лучше ноль, чем число, заниженное в 6.6 раза."""
    rows = build_rows(
        [api_row("Яндекс.Директ 1", level3_id="999", level3="Неизвестная",
                 visits=10, cost=50_000.0)],
        FX, {}, "2026-06-18")
    assert col(rows[0], "cost") == 0.0


def test_meta_cost_taken_from_roistat_and_converted():
    """Meta — единственный источник расхода: кабинета у нас нет."""
    rows = build_rows([api_row("Facebook", level2_id="120254142253170405",
                               level2="CPO: ЛЕТНИЙ SALE_ЖЕНЩИНЫ", visits=13040,
                               cost=815_916.0)], FX, {}, "2026-06-18")
    assert round(col(rows[0], "cost")) == round(815_916.0 * FX)


def test_google_cost_taken_from_roistat():
    """Google берём из Роистата: он видит КМС и PMax, которых нет в нашем кабинете."""
    rows = build_rows([api_row("Google Ads 1", level2_id="d", level2="КМС",
                               level3_id="23952158615",
                               level3="Медийная кампания КМС 18.06.26-25.06.26",
                               visits=7583, cost=507_212.0)], FX, {}, "2026-06-18")
    assert round(col(rows[0], "cost")) == round(507_212.0 * FX)
    assert col(rows[0], "campaign_id") == "23952158615"


def test_offline_deals_have_no_visits_and_no_cost():
    rows = build_rows([api_row("Сделки, созданные самостоятельно", leads=463,
                               paid_leads=387, paid_revenue=15_008_760)],
                      FX, {}, "2026-06-18")
    assert col(rows[0], "sessions") == 0
    assert col(rows[0], "cost") == 0.0
    assert col(rows[0], "channel") == "Offline"
    assert col(rows[0], "purchases_count") == 463


def test_clients_are_filled_for_cac():
    rows = build_rows([api_row("SEO", visits=100, paid_clients=7)], FX, {}, "2026-06-18")
    assert col(rows[0], "customers") == 7


def test_campaign_id_and_subchannel_from_levels():
    """Кампания с level_3, подканал SEO — с level_2 (замер: SEO › Google 10 284)."""
    rows = build_rows([api_row("SEO", level2_id="google", level2="Google", visits=10284)],
                      FX, {}, "2026-06-18")
    assert col(rows[0], "subchannel") == "SEO Google"
    assert col(rows[0], "campaign_id") == ""


def test_referral_subchannel_is_domain():
    rows = build_rows([api_row("Визиты с сайтов", level2="l.instagram.com", visits=2500)],
                      FX, {}, "2026-06-18")
    assert col(rows[0], "channel") == "Referrals"
    assert col(rows[0], "subchannel") == "l.instagram.com"


def test_rows_carry_region_and_date():
    rows = build_rows([api_row("SEO", visits=1)], FX, {}, "2026-06-18")
    assert col(rows[0], "region") == "kz_roistat"
    assert col(rows[0], "date") == "2026-06-18"
    assert col(rows[0], "data_source") == "web"
