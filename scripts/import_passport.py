#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/import_passport.py — паспорт продукта из билдера в смысловой слой агента.

Зачем. Паспорт (что продаём, кто покупатель, кто НЕ наш, слова чужого интента)
строит билдер из лендинга — d:/vscode/EDU кампании, builder/passport.py. Агент
крутится в GitHub Actions на своём чекауте, где этого репозитория нет, поэтому
паспорт едет к нему данными: проекция кладётся в sync/agent/passports/ и
коммитится. Копия — цена за то, что слой работает в бою; чтобы она не старела
молча, в файл пишутся источник и дата импорта.

Едет ПРОЕКЦИЯ, а не паспорт целиком: полный весит 27 КБ, а в промпт помещается
около двух с половиной тысяч символов. Разделы, которые решают интент фразы,
перечислены в FIELDS; остальное (цитаты, оговорки, доказательства) нужно
объявлениям, а не разметке.

    python scripts/import_passport.py --level "d:/vscode/EDU кампании/online_school" \
        --direction school [--apply]

Имя направления — из закрытого перечня sync/classify.py::DIRECTIONS: под ним
паспорт и будет искаться в прогоне.

ВНИМАНИЕ к направлению dist. Классификатор уводит в него ЛЮБУЮ кампанию со
словом «дистанц/заоч/онлайн» в имени — и ВПО-дистант, и СПО-дистант. Продукты у
них разные и паспорта противоречат друг другу («после 9 класса» — анти-маркер
одного и целевой маркер другого), поэтому одним паспортом это направление не
описывается. Скрипт такой импорт делает только с --force и с предупреждением.
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sync.agent import semantic  # noqa: E402
from sync.classify import DIRECTIONS  # noqa: E402

# Что из паспорта решает интент поисковой фразы.
FIELDS = ("what", "who", "not_ours", "anti_markers", "target_markers",
          "competitors")

MIXED_DIRECTIONS = {"dist": "и ВПО-, и СПО-дистант: продукты разные, паспорт один"}


def projection(passport: dict, *, source: str, today: str) -> dict:
    out = {field: passport[field] for field in FIELDS if passport.get(field)}
    # Источник и дата — рядом с данными, а не в истории git: копия стареет
    # молча, и по файлу должно быть видно, из чего и когда она снята.
    out["_source"] = source
    out["_imported_on"] = today
    return out


def read_passport(level_dir: Path) -> dict:
    path = level_dir if level_dir.suffix == ".json" else level_dir / "data" / "passport.json"
    if not path.exists():
        raise SystemExit(f"Паспорта нет: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--level", required=True,
                    help="папка уровня билдера или сам passport.json")
    ap.add_argument("--direction", required=True,
                    help=f"направление кампаний: {', '.join(DIRECTIONS)}")
    ap.add_argument("--apply", action="store_true", help="записать файл")
    ap.add_argument("--force", action="store_true",
                    help="разрешить направление со смешанными продуктами")
    args = ap.parse_args()

    if args.direction not in DIRECTIONS:
        raise SystemExit(f"Направления {args.direction!r} не бывает. "
                         f"Есть: {', '.join(DIRECTIONS)}")
    mixed = MIXED_DIRECTIONS.get(args.direction)
    if mixed and not args.force:
        raise SystemExit(f"Направление {args.direction!r} смешивает {mixed}. "
                         "Одним паспортом оно не описывается; если всё равно "
                         "нужно — --force.")

    level = Path(args.level)
    passport = read_passport(level)
    out = projection(passport, source=str(level), today=date.today().isoformat())
    missing = [f for f in FIELDS if f not in out]

    block = semantic.passport_block(out)
    print(f"Разделов: {len(out) - 2} из {len(FIELDS)}"
          + (f"; нет: {', '.join(missing)}" if missing else ""))
    print(f"Длина блока в промпте: {len(block)} симв. "
          f"(предел {semantic.PASSPORT_BUDGET})")
    if not block:
        raise SystemExit("Проекция пуста: в промпт ехать нечему.")

    path = Path(semantic.PASSPORTS_DIR) / f"{args.direction}.json"
    if not args.apply:
        print(f"\n--- так выглядит в промпте ---\n{block}\n")
        print(f"Ничего не записано. Файл был бы: {path}")
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8", newline="\n")
    print(f"Записано: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
