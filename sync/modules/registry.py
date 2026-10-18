# -*- coding: utf-8 -*-
"""Реестр модулей Директа: ключ -> объект с KEY, run, apply, rollback.

Каждый модуль добавляет свою запись сам; ключ в project_modules без записи здесь
раннер пропускает с причиной, а не падает.
"""

from typing import Any, Dict

MODULES: Dict[str, Any] = {}

# Импорт внизу: модуль не знает о реестре, цикла нет.
from sync.modules import placements as _placements  # noqa: E402

MODULES["placements"] = _placements
