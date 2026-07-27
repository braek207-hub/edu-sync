"""Проба ДОСТУПНЫХ полей Logs API (source=visits) для Фазы B ML (разовая, НЕ прод).

evaluate падает на ПЕРВОМ невалидном поле, поэтому проверяем кандидатов по одному:
base(dateTime)+candidate → 200 значит поле валидно, 400 'Unknown field' — нет.
В конце — evaluate со всеми валидными вместе (итоговый expected_size за день).
Цель: точный набор фич для обогащения ML вместо догадок про имена полей.
"""

import os
import time
from datetime import date, timedelta

import requests

from sync.metrika_offline import COUNTER_VUZ

EVAL = "https://api-metrika.yandex.net/management/v1/counter/{c}/logrequests/evaluate"

# Кандидаты сгруппированы по гипотезе полезности для предсказания оплаты.
CANDIDATES = {
    "поведение (контроль/есть)": [
        "ym:s:dateTime", "ym:s:visitDuration", "ym:s:bounce", "ym:s:pageViews",
    ],
    "история пользователя": [
        "ym:s:isNewUser", "ym:s:visitID", "ym:s:counterUserIDHash",
        "ym:s:firstVisitDateTime", "ym:s:daysSinceFirstVisit",
        "ym:s:userVisitsCount", "ym:s:visitsCount",
    ],
    "UTM из визита (чистая замена utm_source)": [
        "ym:s:UTMSource", "ym:s:UTMMedium", "ym:s:UTMCampaign",
        "ym:s:UTMContent", "ym:s:UTMTerm",
    ],
    "источник/переход": [
        "ym:s:lastTrafficSource", "ym:s:lastsignTrafficSource", "ym:s:firstTrafficSource",
        "ym:s:lastSourceEngine", "ym:s:referer", "ym:s:searchPhrase",
        "ym:s:startURL", "ym:s:endURL",
    ],
    "Директ (качество платного трафика)": [
        "ym:s:lastDirectClickOrder", "ym:s:lastDirectBannerGroup",
        "ym:s:lastDirectClickBanner", "ym:s:lastDirectPhraseOrCond",
        "ym:s:lastDirectPlatformType", "ym:s:lastDirectPlatform",
        "ym:s:lastDirectConditionType", "ym:s:lastDirectClickOrderName",
        "ym:s:hasGCLID", "ym:s:lastGCLID",
    ],
    "гео/тех": [
        "ym:s:regionCountry", "ym:s:regionCity", "ym:s:regionCityID",
        "ym:s:deviceCategory", "ym:s:operatingSystem", "ym:s:browser",
        "ym:s:mobilePhone", "ym:s:mobilePhoneModel",
        "ym:s:screenWidth", "ym:s:screenHeight", "ym:s:physicalScreenWidth",
        "ym:s:windowClientWidth", "ym:s:screenColors",
        "ym:s:javascriptEnabled", "ym:s:cookieEnabled", "ym:s:networkType",
        "ym:s:ipAddress",
    ],
    "⚠️ утечка — только проверка валидности, НЕ использовать": [
        "ym:s:goalsID", "ym:s:params",
    ],
}

BASE = "ym:s:dateTime"


def _h():
    return {"Authorization": f"OAuth {os.environ.get('YM_TOKEN','').strip()}"}


def check(field, day, c):
    fields = BASE if field == BASE else f"{BASE},{field}"
    r = requests.get(EVAL.format(c=c),
                     params={"date1": day, "date2": day, "source": "visits", "fields": fields},
                     headers=_h(), timeout=60)
    if r.status_code == 200:
        ev = r.json().get("log_request_evaluation", {})
        return "valid", ev.get("possible")
    if r.status_code == 400 and "Unknown field" in r.text:
        return "invalid", None
    return f"err{r.status_code}", r.text[:120]


def main():
    c = COUNTER_VUZ
    day = (date.today() - timedelta(days=7)).isoformat()
    print(f"=== Logs API FIELDS probe: counter={c}, день={day} ===\n")
    valid_all = []
    for group, fields in CANDIDATES.items():
        print(f"### {group}")
        for f in fields:
            verdict, extra = check(f, day, c)
            mark = "✅" if verdict == "valid" else ("❌" if verdict == "invalid" else "⁉️")
            print(f"  {mark} {f}" + (f"  [{verdict} {extra}]" if verdict != "valid" else ""))
            if verdict == "valid" and f != BASE:
                valid_all.append(f)
            time.sleep(0.4)
        print()

    print("=== ИТОГ ===")
    print(f"валидных полей (кроме base): {len(valid_all)}")
    print("список:", ",".join(valid_all))
    # финальный evaluate со всеми валидными вместе — размер за день
    r = requests.get(EVAL.format(c=c),
                     params={"date1": day, "date2": day, "source": "visits",
                             "fields": BASE + "," + ",".join(valid_all)},
                     headers=_h(), timeout=60)
    if r.status_code == 200:
        ev = r.json().get("log_request_evaluation", {})
        print(f"evaluate ВСЕ вместе: possible={ev.get('possible')} expected_size={ev.get('expected_size')}")
    else:
        print(f"evaluate всех вместе: HTTP {r.status_code}: {r.text[:200]}")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    main()
