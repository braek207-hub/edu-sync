# -*- coding: utf-8 -*-
"""
sync/meshnflesh_direct_settings.py — синк настроек кампаний Директа кабинета
meshnflesh → mnf_campaign_settings + снимок дня в strategy_snapshots.

Сохранён как точка входа workflow sync-meshnflesh-direct-settings.yml; вся логика —
в sync/direct_settings_cabinets.py (общая для meshnflesh / bjorn / polinarepik).

Запуск:  python -m sync.meshnflesh_direct_settings
"""

from sync.direct_settings_cabinets import sync_cabinet


def sync_meshnflesh_campaign_settings() -> int:
    return sync_cabinet("meshnflesh")


if __name__ == "__main__":
    sync_meshnflesh_campaign_settings()
