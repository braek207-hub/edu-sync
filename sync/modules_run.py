# -*- coding: utf-8 -*-
"""CLI модулей Директа: python -m sync.modules_run --project <slug> [--module <key>] [--dry-run]
[--preview-id <id>] [--inventory]. Код 143 — остановлен SIGTERM (sync/modules/cancel.py).
--inventory — только обновить инвентарь кабинетов и кампаний (юнит <slug>_cabinets)."""

import argparse
import sys
from datetime import datetime, timezone

from sync.modules import cancel
from sync.modules.runner import run_inventory, run_preview, run_project


def _log(msg: str) -> None:
    print(msg, flush=True)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Прогон модулей Директа проекта")
    p.add_argument("--project", required=True)
    p.add_argument("--module")
    p.add_argument("--dry-run", action="store_true")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--preview-id", type=int,
                      help="прогнать только это превью (module_previews.id), без скана")
    mode.add_argument("--inventory", action="store_true",
                      help="только обновить инвентарь кабинетов и кампаний, модули не гонять")
    a = p.parse_args(argv)
    if a.inventory:
        if a.module or a.dry_run:
            p.error("--inventory не сочетается с --module и --dry-run")
        return run_inventory(a.project, log=_log)
    # «Остановить» в Panda-BI → systemctl stop → SIGTERM: чистый выход с кодом 143.
    # Инвентарю перехват не нужен: он короткий и без строк module_runs, SIGTERM просто убивает.
    cancel.install()
    now = datetime.now(timezone.utc)
    if a.preview_id is not None:
        return run_preview(a.project, a.preview_id, now=now, log=_log)
    return run_project(a.project, now=now, only=a.module, dry_run=a.dry_run, log=_log)


if __name__ == "__main__":
    sys.exit(main())
