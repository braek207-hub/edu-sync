# -*- coding: utf-8 -*-
"""CLI модулей Директа: python -m sync.modules_run --project <slug> [--module <key>] [--dry-run]."""

import argparse
import sys
from datetime import datetime, timezone

from sync.modules.runner import run_project


def _log(msg: str) -> None:
    print(msg, flush=True)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Прогон модулей Директа проекта")
    p.add_argument("--project", required=True)
    p.add_argument("--module")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    return run_project(a.project, now=datetime.now(timezone.utc), only=a.module,
                       dry_run=a.dry_run, log=_log)


if __name__ == "__main__":
    sys.exit(main())
