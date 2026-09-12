# -*- coding: utf-8 -*-
"""
sync/meshnflesh_direct_settings.py — синк настроек кампаний Директа кабинета
meshnflesh (mesh-n-flesh) → mnf_campaign_settings.

Тонкая обёртка над sync/edu_direct_settings.py: весь парсинг настроек (стратегия,
аудитория, таргетинг, корректировки ставок, офферный таргетинг, резолв гео/целей)
общий с EDU и один в один зеркалит формат sync/lime_direct.py (JSONB той же формы,
её парсят те же фронт-селекторы). Копировать полторы тысячи строк парсинга под один
кабинет — плодить дублирование там, где EDU уже решил ту же задачу для нескольких
логинов; переиспользуем через параметры table/extra_counter_ids, добавленные в
_upsert_campaign_settings/_sync_campaign_settings.

Таблицу mnf_campaign_settings создаёт миграция Panda-BI. На проде она может быть
ещё не накачена к первому прогону — тогда INSERT упадёт с UndefinedTable, и это
ДОЛЖНО уронить джоб (не перехватывать и не пропускать молча): тихий пропуск скрыл
бы отсутствие данных под зелёной галочкой.

Запуск:  python -m sync.meshnflesh_direct_settings

ENV:
    DATABASE_URL
    MESHNFLESH_DIRECT_TOKEN    — токен Директа кабинета meshnflesh (тот же секрет,
                                 что sync/meshnflesh_metrika.py)
    MESHNFLESH_YANDEX_TOKEN    — токен Метрики для резолва имён целей (опционально;
                                 без него goalNames в settings останутся пустыми,
                                 остальные поля синкаются как обычно)
"""

import os
import traceback

import sync.edu_direct_settings as eds

DIRECT_CLIENT_LOGIN = "meshnflesh"
TARGET_TABLE = "mnf_campaign_settings"
# Счётчик Метрики meshnflesh (см. sync/meshnflesh_metrika.py) — резолвит имена целей,
# когда campaigns.get не отдаёт CounterIds кампании.
METRIKA_COUNTERS = [82116769]


def _prepare_env() -> None:
    """Настроить общий модуль edu_direct_settings на аккаунт meshnflesh.

    _token()/_client_login() в edu_direct_settings читают env DIRECT_TOKEN и
    глобальный _CURRENT_LOGIN (тот же приём, что sync_edu_campaign_settings
    использует для каждого логина EDU по очереди). Этот синк всегда живёт в
    своём отдельном шаге GitHub Actions — подмена os.environ здесь никого не
    задевает, процесс с настоящим DIRECT_TOKEN (EDU) в этот момент не запущен.
    """
    token = os.environ.get("MESHNFLESH_DIRECT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("MESHNFLESH_DIRECT_TOKEN не задан")
    os.environ["DIRECT_TOKEN"] = token

    ym_token = os.environ.get("MESHNFLESH_YANDEX_TOKEN", "").strip()
    if ym_token:
        os.environ["YM_TOKEN"] = ym_token

    eds._CURRENT_LOGIN = DIRECT_CLIENT_LOGIN


def sync_meshnflesh_campaign_settings() -> int:
    _prepare_env()
    names = eds._list_campaigns_for_login()
    campaign_ids = sorted(names.keys())
    if not campaign_ids:
        print(f"[meshnflesh_direct_settings] {DIRECT_CLIENT_LOGIN}: 0 кампаний")
        return 0

    n = eds._sync_campaign_settings(
        campaign_ids,
        names,
        table=TARGET_TABLE,
        extra_counter_ids=METRIKA_COUNTERS,
    )
    print(f"[meshnflesh_direct_settings] {DIRECT_CLIENT_LOGIN}: настройки {n} кампаний")
    return n


if __name__ == "__main__":
    try:
        sync_meshnflesh_campaign_settings()
    except Exception as e:
        print(f"[meshnflesh_direct_settings] ОШИБКА: настройки не синхронизированы: {e}")
        traceback.print_exc()
        raise
