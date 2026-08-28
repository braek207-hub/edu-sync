# -*- coding: utf-8 -*-
"""
sync/agent_manifest.py — выгрузка устройства агента в базу (Э5).

Экран «Карта агента» в Panda-BI читает манифест из edu_agent_manifest. Собрать
его может только этот репозиторий: манифест — это те же константы, по которым
работает прогон (sync/agent/manifest.py), а не их описание.

Запуск:
    python -m sync.agent_manifest            # напечатать сводку, ничего не писать
    python -m sync.agent_manifest --write    # записать в базу

Выгрузка идемпотентна и дёшева (одна строка), поэтому её место — в конце
расчётного прогона: манифест обязан меняться ВМЕСТЕ с кодом, а не по отдельному
решению человека. Ручной запуск остаётся для случая «выкатили правку, а
ежедневный прогон ещё не подошёл».
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sync.agent import db as agent_db
from sync.agent import manifest


def export(write: bool) -> Dict[str, Any]:
    payload = manifest.build(datetime.now(timezone.utc).isoformat())
    if write:
        agent_db.save_manifest(payload)
    return payload


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="выгрузка манифеста агента")
    parser.add_argument("--write", action="store_true",
                        help="записать манифест в edu_agent_manifest")
    args = parser.parse_args(argv)

    payload = export(args.write)
    print(json.dumps({
        "schema_version": payload["schema_version"],
        "lanes": len(payload["lanes"]),
        "action_kinds": len(payload["action_kinds"]),
        "settings": len(payload["settings"]),
        "written": bool(args.write),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
