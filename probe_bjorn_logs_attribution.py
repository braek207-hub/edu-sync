# Проба BJORN, раунд 14. Один вопрос, до которого не дошли раунды 9-13.
#
# План Task 6 берёт из Logs API поля ym:s:last* и кладёт просмотры карточек на строки
# витрины с attribution='lastsign'. Но last (последний переход) и lastsign (последний
# значимый переход) — РАЗНЫЕ модели: last засчитывает прямой заход и внутренний переход,
# lastsign их пропускает и оставляет предыдущий значимый источник. Положить одно на другое
# значит тихо приписать просмотры карточек не тем источникам.
#
# Проверяем на живом API, какие атрибутированные поля визитов Logs API вообще принимает:
# есть ли lastsign-набор, есть ли first-набор. Проверка идёт через /logrequests/evaluate —
# он не создаёт запрос и не тратит квоту.
from __future__ import annotations

import os

import requests

BASE = "https://api-metrika.yandex.net/management/v1/counter/{counter}"
DAY = os.environ.get("PROBE_LOG_DAY", "2026-08-20")

TOKEN = os.environ["METRICA_TOKEN"]
COUNTER = os.environ["METRICA_COUNTER_ID"]
HEADERS = {"Authorization": f"OAuth {TOKEN}"}

CANDIDATES = {
    "last (как в плане Task 6)": [
        "ym:s:visitID",
        "ym:s:lastTrafficSource",
        "ym:s:lastUTMCampaign",
        "ym:s:lastUTMContent",
        "ym:s:lastDirectClickBanner",
    ],
    "lastsign — модель витрины": [
        "ym:s:visitID",
        "ym:s:lastSignTrafficSource",
        "ym:s:lastSignUTMCampaign",
        "ym:s:lastSignUTMContent",
        "ym:s:lastSignDirectClickBanner",
    ],
    "lastsign строчными": [
        "ym:s:visitID",
        "ym:s:lastsignTrafficSource",
        "ym:s:lastsignUTMCampaign",
    ],
    "first — вторая модель витрины": [
        "ym:s:visitID",
        "ym:s:firstTrafficSource",
        "ym:s:firstUTMCampaign",
        "ym:s:firstUTMContent",
        "ym:s:firstDirectClickBanner",
    ],
}


def evaluate(fields: list[str]) -> tuple[bool, str]:
    resp = requests.get(
        BASE.format(counter=COUNTER) + "/logrequests/evaluate",
        params={"date1": DAY, "date2": DAY, "fields": ",".join(fields), "source": "visits"},
        headers=HEADERS,
        timeout=120,
    )
    if resp.status_code == 200:
        body = resp.json().get("log_request_evaluation", {})
        return True, f"возможен={body.get('possible')} максимум дней={body.get('max_possible_day_quantity')}"
    return False, f"HTTP {resp.status_code} {resp.text[:220]}"


def probe_single_fields() -> None:
    """Поимённо: какое ровно поле ломает набор, если набор не принят целиком."""
    print("\nПоимённо (visitID + одно поле):")
    for field in [
        "ym:s:lastTrafficSource",
        "ym:s:lastSignTrafficSource",
        "ym:s:lastsignTrafficSource",
        "ym:s:lastSignificantTrafficSource",
        "ym:s:firstTrafficSource",
        "ym:s:lastSignDirectClickBanner",
        "ym:s:lastSignUTMContent",
    ]:
        ok, note = evaluate(["ym:s:visitID", field])
        print(f"  {'OK  ' if ok else 'НЕТ '} {field}: {note}")


def main() -> None:
    print("=" * 78)
    print(f"Атрибутированные поля визитов Logs API · счётчик {COUNTER} · день {DAY}")
    print("=" * 78)
    for label, fields in CANDIDATES.items():
        ok, note = evaluate(fields)
        print(f"\n{label}\n  {'ПРИНЯТ' if ok else 'ОТКАЗ'}: {note}")
    probe_single_fields()


if __name__ == "__main__":
    main()
