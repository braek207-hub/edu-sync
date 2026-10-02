import os, json, requests
MGMT = "https://api-metrika.yandex.net/management/v1"
token = os.environ["METRICA_TOKEN"].strip()
counter = os.environ["METRICA_COUNTER_ID"].strip()
h = {"Authorization": f"OAuth {token}"}
r = requests.get(f"{MGMT}/counter/{counter}/segment/1008116803", headers=h, timeout=60)
print("HTTP", r.status_code)
s = r.json().get("segment", {})
print("name:", s.get("name"))
print("expression:", s.get("expression"))
print("status:", s.get("status"))
