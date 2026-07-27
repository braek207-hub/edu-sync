"""Проба доступа YM_TOKEN к Logs API Метрики (Фаза B, разовый scope-probe, НЕ прод).

Проверяет всю асинхронную цепочку Logs API за 1 день на счётчике vuz:
evaluate → create → poll(status=processed) → download part0 → clean.
Печатает HTTP-коды и итог. Главное — пройдёт ли create (не 403 scope) и вернёт ли
download per-visit строки с ym:s:dateTime (точное время визита → снимает потолок 26%).
Заодно считает, сколько визитов дня принадлежат нашим лидам (client_id из crm).
"""

import io
import os
import time
from datetime import date, timedelta

import requests

from sync.metrika_offline import COUNTER_VUZ

BASE = "https://api-metrika.yandex.net/management/v1/counter/{c}/logrequest"
EVAL = "https://api-metrika.yandex.net/management/v1/counter/{c}/logrequests/evaluate"
CREATE = "https://api-metrika.yandex.net/management/v1/counter/{c}/logrequests"
# Минимум полей для пробы; ym:s:dateTime = точное время визита (ключевое отличие от Reporting).
FIELDS = "ym:s:dateTime,ym:s:clientID,ym:s:visitDuration,ym:s:bounce,ym:s:pageViews,ym:s:isRobot"


def _h():
    return {"Authorization": f"OAuth {os.environ.get('YM_TOKEN','').strip()}"}


def probe():
    c = COUNTER_VUZ
    day = (date.today() - timedelta(days=7)).isoformat()  # финализированный день
    print(f"=== Logs API probe: counter={c}, день={day} ===")

    # 1. evaluate — влезет ли запрос
    r = requests.get(EVAL.format(c=c),
                     params={"date1": day, "date2": day, "source": "visits", "fields": FIELDS},
                     headers=_h(), timeout=60)
    print(f"[1 evaluate] HTTP {r.status_code}: {r.text[:300]}")
    if r.status_code == 403:
        print(">>> SCOPE FAIL: YM_TOKEN не пускает в Logs API. Нужен токен с доступом к счётчику.")
        return

    # 2. create
    r = requests.post(CREATE.format(c=c),
                      params={"date1": day, "date2": day, "source": "visits", "fields": FIELDS},
                      headers=_h(), timeout=60)
    print(f"[2 create] HTTP {r.status_code}: {r.text[:300]}")
    if r.status_code != 200:
        print(">>> CREATE FAIL — см. код выше (403=scope, 400=слишком большой/поля).")
        return
    req_id = r.json()["log_request"]["request_id"]
    print(f"    request_id={req_id}")

    # 3. poll до processed
    status, parts = None, []
    for attempt in range(30):
        rr = requests.get(f"{BASE.format(c=c)}/{req_id}", headers=_h(), timeout=60)
        lr = rr.json().get("log_request", {})
        status = lr.get("status")
        parts = lr.get("parts", [])
        print(f"[3 poll {attempt}] status={status} parts={len(parts)}")
        if status in ("processed", "processing_failed", "awaiting_retry", "cleaned_by_user"):
            break
        time.sleep(10)
    if status != "processed":
        print(f">>> не дошли до processed (status={status})")
        return

    # 4. download part0 + фильтр по нашим client_id
    keep = set()
    try:
        from sync.db import load_lead_client_ids
        keep = load_lead_client_ids()
    except Exception as e:
        print(f"    (не смог загрузить lead client_ids: {e})")
    dl = requests.get(f"{BASE.format(c=c)}/{req_id}/part/0/download", headers=_h(), timeout=180)
    print(f"[4 download] HTTP {dl.status_code}, байт={len(dl.content)}")
    total, ours, sample = 0, 0, None
    reader = io.StringIO(dl.text)
    header = reader.readline().rstrip("\n").split("\t")
    ci = header.index("ym:s:clientID") if "ym:s:clientID" in header else 1
    for line in reader:
        cols = line.rstrip("\n").split("\t")
        total += 1
        if sample is None:
            sample = cols
        if keep and len(cols) > ci and cols[ci] in keep:
            ours += 1
    print(f"    header={header}")
    print(f"    sample_row={sample}")
    print(f"    визитов за день всего={total}, из них наших лидов={ours}")

    # 5. clean
    cl = requests.post(f"{BASE.format(c=c)}/{req_id}/clean", headers=_h(), timeout=60)
    print(f"[5 clean] HTTP {cl.status_code}")
    print(">>> PROBE OK: Logs API доступен, per-visit dateTime получен." if dl.status_code == 200
          else ">>> download проблема — см. выше")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    probe()
