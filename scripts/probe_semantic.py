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

import json as _json

payload = _json.dumps({
    "model": "deepseek-chat",
    "messages": [{"role": "user", "content": "Ответь json: {\"ok\": true}"}],
    "temperature": 0,
    "response_format": {"type": "json_object"},
}, ensure_ascii=False).encode("utf-8")
for label, kwargs in (("data-bytes", {"data": payload}),
                      ("json-dict", {"json": _json.loads(payload.decode("utf-8"))})):
    r = requests.post("https://api.deepseek.com/chat/completions",
                      headers={"Authorization": "Bearer " + key,
                               "Content-Type": "application/json; charset=utf-8"},
                      timeout=60, **kwargs)
    print(label, r.status_code, r.text[:300])

try:
    answer = semantic.deepseek_asker()("высшее образование дистанционно")
    print("ответ получен, длина", len(answer))
    print(semantic.parse_response(answer))
except Exception:
    traceback.print_exc()
    sys.exit(1)
