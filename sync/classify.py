"""Классификация кампаний — порт из GAS detectProjectFromCampaign_ / detectDirectionFromCampaign_."""


def detect_project(campaign_name: str) -> str:
    s = str(campaign_name or "").lower()
    if "provuz" in s or "postupi_provuz" in s or "provuz_postupi" in s:
        return "provuz"
    if "vsekolledzhi_postupi" in s:
        return "vse"
    if "vuz" in s:
        return "vuz"
    return "unknown"


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
