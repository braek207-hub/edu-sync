"""Классификация кампаний — порт из GAS detectProjectFromCampaign_ / detectDirectionFromCampaign_."""


# Как DIRECT_SHEETS в GAS config.js — лист → проект (кабинет BJ)
SHEET_NAME_TO_PROJECT = {
    "postupi.vsekolledzhi": "vse",
    "postupi.provuz": "provuz",
    "vuz.edunetwork": "vuz",
    "бренды": "brand",
}

LOGIN_HINT_TO_PROJECT = {
    "vsekolledzhi": "vse",
    "provuz": "provuz",
    "vuz": "vuz",
    "бренд": "brand",
    "brand": "brand",
}


def project_from_client(login: str, item: dict) -> str | None:
    """Проект из JSON клиента (sheet_name / project), как лист Директа в GAS."""
    if item.get("project"):
        return str(item["project"]).strip().lower()
    sheet = str(item.get("sheet_name", "")).strip().lower()
    for fragment, proj in SHEET_NAME_TO_PROJECT.items():
        if fragment in sheet:
            return proj
    low = login.lower()
    for fragment, proj in LOGIN_HINT_TO_PROJECT.items():
        if fragment in low:
            return proj
    return None


def detect_project(campaign_name: str) -> str:
    s = str(campaign_name or "").lower()
    # provuz раньше vuz (GAS detectProjectFromCampaign_)
    if "provuz" in s or "postupi_provuz" in s or "provuz_postupi" in s:
        return "provuz"
    if "vsekolledzhi_postupi" in s:
        return "vse"
    if "vuz" in s:
        return "vuz"
    return "unknown"


def normalize_plan_project(raw: str) -> str:
    """Ключи проекта для plan_monthly и фильтров (как isProjectMatch в UI)."""
    s = str(raw or "").strip().lower()
    if not s:
        return ""
    if s in ("vse", "vsekolledzhi", "всеколледжи", "все колледжи", "postupi_vsekolledzhi"):
        return "vse"
    if s in ("vuz", "вуз"):
        return "vuz"
    if s in ("provuz", "провуз", "postupi_provuz", "provuz_postupi"):
        return "provuz"
    if s in ("brand", "бренды", "бренд"):
        return "brand"
    return s


def normalize_plan_direction(raw: str) -> str:
    """Ключи направления для plan_monthly (как isDirectionMatch в UI)."""
    s = str(raw or "").strip().lower()
    if not s:
        return ""
    if s in ("spo", "спо"):
        return "spo"
    if s in ("vpo", "впо"):
        return "vpo"
    if s in ("dist", "дистанс", "дистанц"):
        return "dist"
    if s in ("mti", "мти"):
        return "mti"
    if s in ("ntb", "нтб"):
        return "ntb"
    if s in ("med", "медицина", "мед"):
        return "med"
    if s in ("transfer", "перевод"):
        return "transfer"
    if s in ("it", "айти"):
        return "it"
    if s in ("other", "остальное"):
        return "other"
    return s


def map_crm_land(land: str) -> str:
    s = str(land or "").strip().lower()
    if not s:
        return "unknown"
    if s == "vuz":
        return "vuz"
    if s in ("vsekolledzhi_postupi", "vse", "postupi_vsekolledzhi"):
        return "vse"
    if s in ("postupi_provuz", "provuz", "provuz_postupi"):
        return "provuz"
    if s in ("бренды", "brand"):
        return "brand"
    return "unknown"


def detect_direction(campaign_name: str) -> str:
    s = str(campaign_name or "").lower()
    if "мти" in s or " mti " in s or "мти " in s or " mti" in s:
        return "mti"
    if "нтб" in s:
        return "ntb"
    if "мед" in s or "медицина" in s or "медицин" in s:
        return "med"
    if "перевод" in s:
        return "transfer"
    if (
        " it" in s
        or "it " in s
        or "айти" in s
        or "программ" in s
        or "разработ" in s
        or "developer" in s
        or "python" in s
        or "java" in s
        or "c#" in s
        or " js" in s
        or "web" in s
        or "data" in s
        or "аналит" in s
        or "информат" in s
    ):
        return "it"
    if (
        "дистанц" in s
        or "дистанс" in s
        or "заоч" in s
        or "онлайн" in s
        or " рф " in s
        or "/ рф" in s
        or "рф /" in s
    ):
        return "dist"
    if "спо" in s:
        return "spo"
    if "впо" in s:
        return "vpo"
    return "other"
