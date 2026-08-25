# -*- coding: utf-8 -*-
"""Проба смыслового слоя на раннере: где именно рвётся кириллица."""

import os
import sys
import traceback

import requests

from sync.agent import semantic

key = os.environ.get("DEEPSEEK_API_KEY") or ""
print("ключ: длина", len(key), "только ASCII:", key.isascii())
print("requests", requests.__version__, "python", sys.version.split()[0])

try:
    answer = semantic.deepseek_asker()("высшее образование дистанционно")
    print("ответ получен, длина", len(answer))
    print(semantic.parse_response(answer))
except Exception:
    traceback.print_exc()
    sys.exit(1)
