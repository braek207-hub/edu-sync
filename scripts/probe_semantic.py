# -*- coding: utf-8 -*-
"""Проба смыслового слоя на раннере: где именно рвётся вызов модели."""

import json as _json
import os
import sys
import traceback

import requests

from sync.agent import semantic

key = os.environ.get("DEEPSEEK_API_KEY") or ""
key = "".join(c for c in key if "!" <= c <= "~")
print("ключ: длина", len(key), "только ASCII:", key.isascii())
print("requests", requests.__version__, "python", sys.version.split()[0])

prompt = semantic.build_prompt(["высшее образование дистанционно"], "онлайн-университет")
variants = (
    ("как есть", prompt),
    ("со строчным json", prompt + "\nФормат ответа — json."),
    ("без response_format", prompt),
)
for label, text in variants:
    body = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": text}],
        "temperature": 0,
    }
    if label != "без response_format":
        body["response_format"] = {"type": "json_object"}
    response = requests.post(
        "https://api.deepseek.com/chat/completions",
        data=_json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json; charset=utf-8"},
        timeout=60,
    )
    print(label, response.status_code, response.text[:400])

try:
    answer = semantic.deepseek_asker()("высшее образование дистанционно")
    print("ответ получен, длина", len(answer))
    print(semantic.parse_response(answer))
except Exception:
    traceback.print_exc()
    sys.exit(1)
