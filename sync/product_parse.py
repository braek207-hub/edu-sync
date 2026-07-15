"""Разбор строки «Продукты» (лист Оплаты) на измерения для аналитики.

Формат (порядок сегментов «плавает» — форма бывает 4-м или последним):
    Уровень / Ступень / Специальность (код) / [Профиль (id)] / Форма / Факультет / Кафедра(id)
Пример:
    СПО / СПЦ / Сестринское дело (34.02.01) / Заочная с ДОТ полная / Медицинский факультет / …(id)

Возвращаемые поля (все Optional[str], пустое → None):
    level      — Уровень укрупнённо: «Среднее (СПО)» / «Высшее (ВО)» / «ДПО» / «Школа» / «Курс/услуга»
    stage      — Ступень: СПО / Бакалавриат / Специалитет / Магистратура / Аспирантура / Ординатура / ДПО / …
    form       — Форма: «Очная» / «Заочная» / «Очно-заочная/вечерняя» / None
    ugsn       — код УГСН (первые 2 цифры кода специальности), напр. "09"
    direction  — Направление укрупнённо по УГСН: «Медицина» / «IT» / «Юриспруденция» / …
    specialty  — Специальность (текст до «(код)»)
    profile    — Профиль/направленность (сегмент с внутренним id «(NNNN)», если есть)
    faculty    — Факультет (сегмент с «факультет» или известный кампус)
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

FIELDS = ["level", "stage", "form", "ugsn", "direction", "specialty", "profile", "faculty"]

_CODE_RE = re.compile(r"\((\d\d)\.(\d\d)\.(\d\d)\)")  # (NN.LL.NN) — LL = уровень
_PROFILE_ID_RE = re.compile(r"\((\d{4})\)")  # внутренний id профиля (4 цифры)
_EDU_CODE_ANY = re.compile(r"\(\d\d\.\d\d\.\d\d\)")

# УГСН (первые 2 цифры кода) → направление укрупнённо (человекочитаемо).
UGSN_DIRECTION: Dict[str, str] = {
    "07": "Строительство и архитектура",
    "08": "Строительство и архитектура",
    "09": "IT",
    "10": "IT",  # информационная безопасность
    "11": "Электроника и связь",
    "13": "Энергетика",
    "15": "Машиностроение",
    "23": "Транспорт",
    "25": "Беспилотные технологии",
    "27": "Управление в техн. системах",
    "31": "Медицина",
    "32": "Медицина",
    "33": "Медицина",  # фармация
    "34": "Медицина",  # сестринское дело
    "36": "Ветеринария",
    "37": "Психология",
    "38": "Экономика и управление",
    "39": "Социология и соц. работа",
    "40": "Юриспруденция",
    "41": "Политология/междунар.",
    "42": "Реклама и СМИ",
    "43": "Сервис и туризм",
    "44": "Педагогика",
    "45": "Языки и филология",
    "46": "Документоведение/история",
    "49": "Физкультура и спорт",
    "51": "Культура и искусство",
    "53": "Музыкальное искусство",
    "54": "Дизайн и искусство",
}

# Ступень по коду УГСН-уровня (средняя пара цифр NN.LL.NN).
_STAGE_BY_CODE_LL: Dict[str, str] = {
    "01": "СПО",  # профессии рабочих/служащих — СПО-уровень
    "02": "СПО",
    "03": "Бакалавриат",
    "04": "Магистратура",
    "05": "Специалитет",
    "06": "Аспирантура",
    "07": "Ординатура",
    "08": "Ординатура",  # мед. 31.08.xx
}

# Ступень по seg2 (аббревиатура ступени) — приоритетнее кода.
_STAGE_BY_SEG2 = {
    "СПЦ": None,  # зависит от seg1 (СПО/СПЦ=СПО, ВО/СПЦ=Специалитет) → решается ниже
    "БАК": "Бакалавриат",
    "МАГ": "Магистратура",
    "АСП": "Аспирантура",
    "ОРД": "Ординатура",
    "ППП": "ДПО (переподготовка)",
    "ДОП": "Доп. образование",
    "ООО": "Основное общее",
    "СОО": "Среднее общее",
}


def _empty() -> Dict[str, Optional[str]]:
    return {f: None for f in FIELDS}


def _extract_code_ll(s: str) -> Optional[str]:
    m = _CODE_RE.search(s)
    return m.group(2) if m else None


def _extract_ugsn(s: str) -> Optional[str]:
    m = _CODE_RE.search(s)
    return m.group(1) if m else None


def _detect_form(low: str) -> Optional[str]:
    if "очно-заочн" in low or "вечерн" in low or "выходного дня" in low:
        return "Очно-заочная/вечерняя"
    if "заочн" in low:
        return "Заочная"
    if "классическ" in low or "очн" in low:
        return "Очная"
    return None


def _detect_specialty(segs: List[str]) -> Optional[str]:
    # Специальность — сегмент, содержащий образовательный код (NN.LL.NN); текст до «(».
    for seg in segs[2:]:
        if _EDU_CODE_ANY.search(seg):
            name = re.split(r"\s*\(", seg, maxsplit=1)[0].strip()
            return name or None
    # Фолбэк: 3-й сегмент как есть (ДПО/ДО без числового кода).
    seg3 = segs[2] if len(segs) > 2 else ""
    seg3 = re.split(r"\s*\(", seg3, maxsplit=1)[0].strip()
    return seg3 or None


def _detect_profile(segs: List[str]) -> Optional[str]:
    # Профиль — сегмент с 4-значным внутренним id «(NNNN)», не образовательный код, не форма.
    for seg in segs[3:]:
        if _EDU_CODE_ANY.search(seg):
            continue
        low = seg.lower()
        if _detect_form(low):
            continue
        if _PROFILE_ID_RE.search(seg):
            name = re.split(r"\s*\(", seg, maxsplit=1)[0].strip()
            if name:
                return name
    return None


def _detect_faculty(segs: List[str]) -> Optional[str]:
    for seg in segs[3:]:
        low = seg.lower()
        if "факультет" in low:
            return seg.strip() or None
    return None


def parse_product(raw: Optional[str]) -> Dict[str, Optional[str]]:
    s = (raw or "").strip()
    if not s:
        return _empty()

    segs = [seg.strip() for seg in s.split("/")]
    seg1 = segs[0].upper() if len(segs) > 0 else ""
    seg2 = segs[1].upper() if len(segs) > 1 else ""
    low = s.lower()

    out = _empty()

    # ── Уровень + ступень ──
    ll = _extract_code_ll(s)
    if seg1 == "СПО" or (seg1 == "СПЦ" and seg2 == "СПО"):
        out["level"] = "Среднее (СПО)"
        out["stage"] = "СПО"
    elif seg1 == "ВО":
        out["level"] = "Высшее (ВО)"
        if seg2 == "СПЦ":
            out["stage"] = "Специалитет"
        elif seg2 in _STAGE_BY_SEG2 and _STAGE_BY_SEG2[seg2]:
            out["stage"] = _STAGE_BY_SEG2[seg2]
        elif ll and ll in _STAGE_BY_CODE_LL:
            out["stage"] = _STAGE_BY_CODE_LL[ll]
        else:
            out["stage"] = "Высшее (прочее)"
    elif seg1 == "ДПО":
        out["level"] = "ДПО"
        out["stage"] = "ДПО (переподготовка)"
    elif seg1 == "ДО" and seg2 == "ДОП":
        out["level"] = "ДПО"
        out["stage"] = "Доп. образование"
    elif seg1 == "ОО":
        out["level"] = "Школа"
        out["stage"] = _STAGE_BY_SEG2.get(seg2, "Общее")
    else:
        # synergy_education, Годовая подписка, Онлайн-курс, Услуга…
        out["level"] = "Курс/услуга"
        out["stage"] = None

    # ── Форма ──
    out["form"] = _detect_form(low)

    # ── Направление (УГСН) + специальность/профиль/факультет ──
    ugsn = _extract_ugsn(s)
    out["ugsn"] = ugsn
    if ugsn:
        out["direction"] = UGSN_DIRECTION.get(ugsn, f"Другое (код {ugsn})")
    elif out["level"] in ("ДПО", "Курс/услуга", "Школа"):
        out["direction"] = out["level"]

    if out["level"] not in ("Курс/услуга",):
        out["specialty"] = _detect_specialty(segs)
        out["profile"] = _detect_profile(segs)
        out["faculty"] = _detect_faculty(segs)

    return out