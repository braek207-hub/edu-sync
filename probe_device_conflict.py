# -*- coding: utf-8 -*-
"""
probe_device_conflict.py — почему TABLET_ADJUSTMENT отвергается после DESKTOP.

Первая боевая запись (32559366898): шесть корректировок применились, седьмая —
TABLET_ADJUSTMENT — отвергнута с Code 6000 «Условия в корректировках
пересекаются». Документация Яндекса утверждает, что DESKTOP (компьютеры +
Smart TV) и TABLET не взаимоисключающие; практика говорит иначе. Верим
кабинету, а не справочнику, — и проверяем экспериментом.

Гипотеза: набор корректировок по устройствам Директ принимает только целиком.
Движок шлёт их по одной (writer/apply.py::to_api_call кладёт ровно один элемент
в BidModifiers), и планшет поверх только что созданного десктопа читается как
пересечение.

Два шага:
  1. повторить ОДИН TABLET — ждём ту же 6000, это проверка воспроизводимости;
  2. отправить TABLET вместе с MOBILE одним запросом — если пройдёт, гипотеза
     подтверждена.

Мобильная корректировка в шаге 2 НЕЙТРАЛЬНАЯ (100 = без изменений): она нужна
только чтобы набор был полным, и ставок не меняет.

Без --apply ничего не отправляет, только печатает тела запросов.
Запуск: python probe_device_conflict.py [--apply]
ENV: DIRECT_TOKEN, DIRECT_CLIENTS_JSON
"""

import json
import os
import sys

import requests

BIDMODIFIERS_URL = "https://api.direct.yandex.com/json/v5/bidmodifiers"

ACCOUNT = "account10-506462-fqs4"
CAMPAIGN = 114057545
# Та самая отвергнутая корректировка: −38 % → 62 в 100-базной шкале Директа.
TABLET_VALUE = 62
NEUTRAL = 100


def _post(login, payload):
    headers = {
        "Authorization": f"Bearer {os.environ['DIRECT_TOKEN']}",
        "Client-Login": login,
        "Accept-Language": "ru",
        "Content-Type": "application/json; charset=utf-8",
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    r = requests.post(BIDMODIFIERS_URL, data=body, headers=headers, timeout=120)
    if r.status_code != 200:
        return {"_http": r.status_code, "_body": r.text[:400]}
    return r.json()


def _verdict(result):
    if "_http" in result:
        return f"HTTP {result['_http']}: {result['_body']}"
    error = result.get("error") or {}
    if error:
        return (f"ошибка запроса: {error.get('error_code')} "
                f"{error.get('error_string')} / {error.get('error_detail')}")
    added = (result.get("result") or {}).get("AddResults") or []
    out = []
    for element in added:
        errors = element.get("Errors") or []
        if errors:
            out.append("ОТКАЗ: " + "; ".join(
                f"{e.get('Code')} {e.get('Details') or e.get('Message')}" for e in errors))
        else:
            out.append(f"ПРИНЯТО, Id={element.get('Id')}")
    return " | ".join(out) or json.dumps(result, ensure_ascii=False)[:300]


def main() -> int:
    apply = "--apply" in sys.argv

    step1 = {"method": "add", "params": {"BidModifiers": [
        {"CampaignId": CAMPAIGN, "TabletAdjustment": {"BidModifier": TABLET_VALUE}},
    ]}}
    step2 = {"method": "add", "params": {"BidModifiers": [
        {"CampaignId": CAMPAIGN, "TabletAdjustment": {"BidModifier": TABLET_VALUE}},
        {"CampaignId": CAMPAIGN, "MobileAdjustment": {"BidModifier": NEUTRAL}},
    ]}}

    for title, payload in (("шаг 1: только TABLET", step1),
                           ("шаг 2: TABLET + нейтральный MOBILE одним запросом", step2)):
        print(f"\n--- {title}")
        print(json.dumps(payload, ensure_ascii=False))
        if not apply:
            print("  (репетиция, запрос не отправлен)")
            continue
        print("  " + _verdict(_post(ACCOUNT, payload)))

    if not apply:
        print("\nЗапуск без --apply: ничего не отправлено.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
