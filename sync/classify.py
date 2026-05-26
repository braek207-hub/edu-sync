"""Классификация кампаний — порт из GAS detectProjectFromCampaign_ / detectDirectionFromCampaign_."""


# Как DIRECT_SHEETS в GAS config.js — лист → проект (кабинет BJ)
# Только для подсказки кабинета в DIRECT_CLIENTS_JSON (не project строки — как GAS).
SHEET_NAME_TO_PROJECT = {
    "postupi.vsekolledzhi": "vse",
    "postupi.provuz": "provuz",
    "vuz.edunetwork": "vuz",
}

LOGIN_HINT_TO_PROJECT = {
    "vsekolledzhi": "vse",
    "provuz": "provuz",
    "vuz": "vuz",
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


# Листы Директа в книге EDU (как GAS DIRECT_SHEETS)
DIRECT_SHEETS: dict[str, str] = {
    "vse": "Postupi.VseKolledzhi",
    "provuz": "Postupi.ProVuz",
    "vuz": "Vuz.Edunetwork",
    "brand": "Бренды",
}


def normalize_b24_dim(raw) -> str:
    s = str(raw if raw is not None else "").strip()
    return s or "unknown"


def normalize_city_ip_segment(raw) -> str:
    """Порт GAS normalizeCityIpSegment_."""
    s = str(raw if raw is not None else "").strip().lower()
    if not s:
        return "rf"
    compact = (
        s.replace("ё", "е")
        .replace(".", " ")
        .replace("-", " ")
        .replace("_", " ")
        .replace(",", " ")
        .replace("(", " ")
        .replace(")", " ")
    )
    while "  " in compact:
        compact = compact.replace("  ", " ")
    compact = compact.strip()
    if "московская область" in compact or "подмосков" in compact:
        return "msk_mo"
    # Как normalizeCityIpSegment_ в GAS utils.js (полный список)
    msk_mo_cities = (
        "москва",
        "зеленоград",
        "троицк",
        "щербинка",
        "балашиха",
        "подольск",
        "химки",
        "мытищи",
        "королев",
        "люберцы",
        "красногорск",
        "одинцово",
        "домодедово",
        "электросталь",
        "коломна",
        "серпухов",
        "орехово зуево",
        "долгопрудный",
        "пушкино",
        "раменское",
        "реутов",
        "жуковский",
        "ногинск",
        "лобня",
        "видное",
        "дмитров",
        "солнечногорск",
        "истра",
        "фрязино",
        "дубна",
        "клин",
        "чехов",
        "ступино",
        "наро фоминск",
        "воскресенск",
        "егорьевск",
        "сергиев посад",
        "ивантеевка",
        "щелково",
        "дзержинский",
        "котельники",
        "лыткарино",
        "краснознаменск",
        "московский",
        "апрелевка",
        "дедовск",
        "звенигород",
        "протвино",
        "павловский посад",
        "руза",
        "можайск",
        "волоколамск",
        "лосино петровский",
        "электрогорск",
        "электроугли",
        "старая купавна",
        "бронницы",
        "яхрома",
        "красноармейск",
        "куровское",
        "ликино дулево",
        "пересвет",
        "верея",
        "высоковск",
        "талдом",
        "озеры",
        "кашира",
        "зарайск",
        "луховицы",
        "шатура",
        "розы люксембург",
    )
    for city in msk_mo_cities:
        if compact == city or city in compact:
            return "msk_mo"
    return "rf"


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


def resolve_row_project(
    sheet_project: str | None,
    campaign_name: str,
    land: str = "",
) -> str:
    """Проект строки: ленд CRM → имя кампании (как GAS metaByCampaignId, без ключа листа)."""
    _ = sheet_project  # лист «Бренды» не задаёт project
    if land:
        mapped = map_crm_land(land)
        if mapped != "unknown":
            return mapped
    return detect_project(campaign_name)


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
