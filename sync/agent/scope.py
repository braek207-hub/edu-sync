# -*- coding: utf-8 -*-
"""
sync/agent/scope.py — граница зоны ответственности агента.

Что сюда попало, агент не видит НИ В ОДНОЙ стадии: не читает, не считает, не
предлагает и не пишет. Это не фильтр отчёта и не рычаг тюнинга — потому и
константы кода, а не ключи панели edu_agent_config: панель существует, чтобы
человек менял пороги, а «этот кабинет ведёт не владелец» порогом не бывает.

Исключение стоит ОДНИМ множеством на все стадии намеренно. Расчёт, движок
записи, сторож отката и сверка дрейфа входят в кабинет с разных сторон, и
четыре независимых списка разъехались бы первым же дополнением — а цена
расхождения здесь не отчёт с лишней строкой, а изменение в чужом кабинете.
"""

from typing import Any, Dict, Iterable, List, Set

from sync.agent.db import normalize_login

# Кабинет «Бренды» (sheet_name в DIRECT_CLIENTS_JSON) ведёт не владелец
# проекта: решения по нему принимает другая команда, и любое изменение агента
# там — вмешательство в чужую работу. Решение владельца 29.08.2026.
EXCLUDED_ACCOUNTS = frozenset({"account4-506456-gsrr"})

# Кампании «rsv» — чужие РК внутри своих кабинетов: в проде они живут в
# account10-506462-fqs4 (vse) и account1-506453-ln8s (provuz), то есть
# кабинетом их не отсечь. Общего у них только помета в имени. Решение
# владельца 29.08.2026; в журнале edu_agent_actions по ним на тот день были
# только строки status=dry_run — откатывать нечего.
EXCLUDED_CAMPAIGN_PATTERNS = ("rsv",)


def is_excluded_account(login: Any) -> bool:
    """Кабинет вне зоны ответственности агента.

    Логин нормализуется той же функцией, что образует ключ object_id таблиц
    агента (agent_db.normalize_login): сравнение по сырой строке промахнулось
    бы на пробеле в переменной окружения — и промахнулось бы МОЛЧА, то есть
    кабинет остался бы в работе, а отчёт прогона выглядел бы штатно.
    Регистр логина Директу безразличен, поэтому и сравнению тоже.
    """
    value = normalize_login(login).lower()
    return bool(value) and value in {a.lower() for a in EXCLUDED_ACCOUNTS}


def is_excluded_campaign(name: Any) -> bool:
    """Кампания вне зоны ответственности агента — по подстроке в имени.

    Подстрока, а не сегмент имени: в проде помета встречается и суффиксом
    («… ВПО-МСК-rsv», «… ВПО-МСК-rsv-2»), и отдельным куском («… /ВПО/ rsv»).
    Разбор по разделителям отсёк бы первую форму.

    Пустое имя — не признак чужой кампании: в витрине имя бывает не
    заполнено, и считать такие строки исключёнными значило бы прятать от
    агента собственный расход.
    """
    value = str(name or "").lower()
    return any(pattern in value for pattern in EXCLUDED_CAMPAIGN_PATTERNS)


def filter_clients(clients: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Кабинеты прогона без исключённых. Порядок исходного списка сохраняется."""
    return [c for c in clients if not is_excluded_account(c.get("login"))]


def excluded_client_logins(clients: Iterable[Any]) -> List[str]:
    """Логины, которые отбор действительно выбросил из состава прогона.

    Отчёт прогона обязан называть факт, а не константу: EXCLUDED_ACCOUNTS —
    свойство кода, а состав прогона — свойство секрета. Кабинет, которого в
    DIRECT_CLIENTS_JSON нет вовсе, напечатанный как отброшенный, сообщал бы
    о работе, которой не было.
    """
    logins = [c.get("login") if isinstance(c, dict) else c for c in clients]
    return sorted({normalize_login(login) for login in logins
                   if is_excluded_account(login)})


def filter_campaign_rows(rows: Iterable[Dict[str, Any]],
                         name_key: str = "campaign_name") -> List[Dict[str, Any]]:
    """Строки с именем кампании без исключённых.

    name_key — потому что имя приезжает под разными ключами: campaign_name в
    витрине и Name в ответе campaigns.get.
    """
    return [r for r in rows if not is_excluded_campaign(r.get(name_key))]


def excluded_campaign_ids(rows: Iterable[Dict[str, Any]],
                          name_key: str = "campaign_name",
                          id_key: str = "campaign_id") -> Set[str]:
    """Идентификаторы исключённых кампаний из строк, где имя ещё видно.

    Ниже по течению имени нет вовсе: сегментные отчёты Директа отдают
    CampaignId и ничего больше, а сравнивать там уже не с чем. Множество Id
    снимается там, где имя есть, и дальше едет вместо предиката.
    """
    return {str(r.get(id_key)) for r in rows if is_excluded_campaign(r.get(name_key))}


def drop_campaign_ids(rows: Iterable[Dict[str, Any]],
                      excluded_ids: Iterable[Any],
                      id_key: str = "campaign_id") -> List[Dict[str, Any]]:
    """Отбор там, где имени нет вовсе, — по заранее снятому множеству Id.

    Сегментные отчёты Директа и crm_lead_details знают только CampaignId:
    предикат по имени до них не достаёт, и единственная различающая величина —
    множество, снятое там, где имя ещё было (excluded_campaign_ids).

    Пустое множество возвращает те же строки: «исключать нечего» обязано
    означать «ничего не меняем», а не «отбор не сработал».
    """
    excluded = {str(cid) for cid in excluded_ids or ()}
    if not excluded:
        return list(rows)
    return [r for r in rows if str(r.get(id_key)) not in excluded]


def like_patterns() -> List[str]:
    """Те же подстроки в форме LIKE — для условий, которые считает СУБД.

    Выводятся из EXCLUDED_CAMPAIGN_PATTERNS, а не набираются в тексте
    запроса: список исключений один на систему, и вторая его копия в SQL
    разъехалась бы с первым же дополнением.
    """
    for pattern in EXCLUDED_CAMPAIGN_PATTERNS:
        # % и _ в LIKE — метасимволы: подстрока с ними значила бы в SQL не то,
        # что в Python, и граница зоны ответственности разошлась бы между
        # предикатом и запросом. Пока таких подстрок нет, проверка держит это
        # свойство; появится — либо экранировать здесь и добавить ESCAPE во
        # все четыре условия, либо переписать их на strpos.
        assert "%" not in pattern and "_" not in pattern, (
            f"подстрока {pattern!r} содержит метасимвол LIKE: "
            f"условие в SQL перестанет совпадать с предикатом Python")
    return [f"%{pattern}%" for pattern in EXCLUDED_CAMPAIGN_PATTERNS]
