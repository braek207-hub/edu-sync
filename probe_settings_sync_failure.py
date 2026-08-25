# -*- coding: utf-8 -*-
"""
probe_settings_sync_failure.py — где именно падает синк настроек кампаний.

Замер 25.08.2026: в прогоне sync-edu-direct-settings (run 32803336967) три
кабинета из четырёх кончились строкой

    WARN настройки не синхронизированы: invalid literal for int() with base 10: 'Items'

Записалось 2 кампании из 129, workflow при этом зелёный: исключение ловится
общим except и превращается в предупреждение. Витрина edu_campaign_settings
живёт на данных прошлых успешных прогонов, а кампании, созданные позже, в неё
не попадают вовсе — отсюда 12 обычных TEXT_CAMPAIGN в «слепой зоне» агента
(probe_blind_campaigns_api, run 32869110285).

Текст ошибки означает, что где-то итерируется СЛОВАРЬ вида {"Items": [...]}
вместо списка: перебор словаря даёт ключи, и int("Items") падает. Какое именно
поле приходит в такой форме — вопрос к API, а не к рассуждению, поэтому probe
повторяет тот же путь с полным traceback и печатает формы подозрительных
полей.

Скрипт read-only: запись в витрину подменена счётчиком.
"""

import json
import traceback
from typing import Any, Dict, List

from sync import edu_direct_settings as S
from sync.direct import _direct_clients


def _shape(value: Any) -> str:
    if value is None:
        return "None"
    if isinstance(value, list):
        return f"list[{len(value)}]"
    if isinstance(value, dict):
        return "dict{" + ",".join(sorted(value.keys())[:5]) + "}"
    return type(value).__name__


def main() -> int:
    # Запись подменяется до первого вызова: probe не имеет права трогать витрину.
    S._upsert_campaign_settings = lambda rows: len(rows)

    out: List[Dict[str, Any]] = []
    for client in _direct_clients():
        login = client["login"]
        S._CURRENT_LOGIN = login
        entry: Dict[str, Any] = {"login": login}
        try:
            names = S._list_campaigns_for_login()
            campaign_ids = sorted(names.keys())
            entry["campaigns"] = len(campaign_ids)
            if not campaign_ids:
                out.append(entry)
                continue

            # Формы полей, на которых спотыкается перебор. Смотрим на первых
            # десяти кампаниях: если форма плавает, хватит и их.
            base_map = S._fetch_campaigns_for_settings(campaign_ids)
            adgroups_map = S._fetch_adgroups_by_campaign(campaign_ids)
            shapes: Dict[str, Dict[str, int]] = {}

            def _note(field: str, value: Any) -> None:
                shapes.setdefault(field, {}).setdefault(_shape(value), 0)
                shapes[field][_shape(value)] += 1

            for cid in campaign_ids[:10]:
                strat = (base_map.get(cid) or {}).get("strategy") or {}
                for ch_key in ("search", "network"):
                    _note(f"strategy.{ch_key}.goalIds", (strat.get(ch_key) or {}).get("goalIds"))
                _note("strategy.package.goalIds", (strat.get("package") or {}).get("goalIds"))
                _note("strategy.priorityGoals", strat.get("priorityGoals"))
                _note("strategy.priorityGoalsDetails", strat.get("priorityGoalsDetails"))
                for ag in (adgroups_map.get(cid) or [])[:3]:
                    _note("adgroup.regionIds", ag.get("regionIds"))
                    _note("adgroup.restrictedRegionIds", ag.get("restrictedRegionIds"))
            entry["shapes"] = shapes

            entry["rows"] = S._sync_campaign_settings(campaign_ids, names)
            entry["ok"] = True
        except Exception as exc:  # noqa: BLE001
            entry["ok"] = False
            entry["error"] = f"{type(exc).__name__}: {exc}"
            # Полный traceback — то, чего не хватало в боевом логе: строка,
            # на которой перебор наткнулся на словарь.
            entry["traceback"] = traceback.format_exc().splitlines()[-12:]
        out.append(entry)

    print(json.dumps({"accounts": out}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
