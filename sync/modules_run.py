# -*- coding: utf-8 -*-
"""CLI модулей Директа: python -m sync.modules_run --project <slug> [--module <key>] [--dry-run]
[--preview-id <id>]."""

import argparse
import sys
from datetime import datetime, timezone

from sync.modules.runner import run_preview, run_project


def _log(msg: str) -> None:
    print(msg, flush=True)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Прогон модулей Директа проекта")
    p.add_argument("--project", required=True)
    p.add_argument("--module")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--preview-id", type=int,
                   help="прогнать только это превью (module_previews.id), без скана")
    a = p.parse_args(argv)
    now = datetime.now(timezone.utc)
    if a.preview_id is not None:
        return run_preview(a.project, a.preview_id, now=now, log=_log)
    return run_project(a.project, now=now, only=a.module, dry_run=a.dry_run, log=_log)


if __name__ == "__main__":
    sys.exit(main())
