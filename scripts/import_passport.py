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

**Адрес паспорта — продукт, а не направление.** Паспорт делается ПОД ЛЕНДИНГ,
и уровень билдера — это и есть продукт. Направление кампаний (detect_direction)
с ним не совпадает ни в одну сторону: 'dist' накрывает два разных продукта
(ВПО-дистант и СПО-дистант, у которых «после 9 класса» — анти-маркер одного и
целевой маркер другого), а «Онлайн-школа» живёт в восьми кампаниях одного
направления. Поэтому файл кладётся под именем продукта, а адресация едет
картой sync/agent/passports/index.json:

  * by_campaign — точная: журнал билдера (<уровень>/output/upload-log.jsonl)
    называет id каждой созданной кампании, и точнее ответа не существует;
  * by_direction — приближение для кампаний, которых билдер не собирал (их в
    кабинете большинство). Ставится только там, где направление описывается
    ОДНИМ продуктом; смешанные (MIXED_DIRECTIONS) не ставятся вовсе.

    python scripts/import_passport.py --level "d:/vscode/EDU кампании/kolledzh" \
        [--product kolledzh] [--direction spo] [--also-level <папка>] [--apply]

--product по умолчанию — имя папки уровня. --also-level добавляет кампании
соседнего уровня тому же продукту (варианты одного лендинга: РСЯ, ретаргетинг,
конкуренты). --direction ставит приближение по направлению и требует --force
для смешанных.
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import List, Optional

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


def campaigns_of(level: Path) -> List[str]:
    """Id кампаний, созданных билдером на этом уровне.

    Журнал пишется самим билдером в момент заливки, поэтому связь «кампания →
    лендинг» здесь фактическая, а не выведенная из имени кампании. Журнала
    нет — пустой список: адресация по кампаниям просто не появится, и продукт
    останется доступен через направление.
    """
    path = level / "output" / "upload-log.jsonl"
    if not path.exists():
        return []
    ids: List[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("event") == "campaign_created" and event.get("id"):
            ids.append(str(event["id"]))
    return ids


def merged_index(product: str, campaigns: List[str],
                 direction: Optional[str]) -> dict:
    """Карта адресации с добавленным продуктом.

    Правится, а не переписывается: карту наполняют разные импорты, и полная
    перезапись стирала бы соседние продукты — молча, потому что файл на диске
    выглядел бы валидным.
    """
    path = Path(semantic.PASSPORTS_DIR) / semantic.PASSPORT_INDEX
    try:
        index = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        index = {}
    by_campaign = dict(index.get("by_campaign") or {})
    by_direction = dict(index.get("by_direction") or {})
    for campaign_id in campaigns:
        by_campaign[str(campaign_id)] = product
    if direction:
        by_direction[direction] = product
    return {
        "_note": ("адресация паспортов: кампания → продукт (точная, из журнала "
                  "билдера) и направление → продукт (приближение). Правится "
                  "scripts/import_passport.py, читается sync/agent/semantic.py"),
        "by_campaign": dict(sorted(by_campaign.items())),
        "by_direction": dict(sorted(by_direction.items())),
    }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--level", required=True,
                    help="папка уровня билдера или сам passport.json")
    ap.add_argument("--product", default=None,
                    help="имя продукта (по умолчанию — имя папки уровня)")
    ap.add_argument("--direction", default=None,
                    help=f"приближение по направлению: {', '.join(DIRECTIONS)}")
    ap.add_argument("--also-level", action="append", default=[],
                    help="ещё папка того же продукта — её кампании войдут в карту")
    ap.add_argument("--apply", action="store_true", help="записать файлы")
    ap.add_argument("--force", action="store_true",
                    help="разрешить направление со смешанными продуктами")
    args = ap.parse_args()

    if args.direction is not None:
        if args.direction not in DIRECTIONS:
            raise SystemExit(f"Направления {args.direction!r} не бывает. "
                             f"Есть: {', '.join(DIRECTIONS)}")
        mixed = MIXED_DIRECTIONS.get(args.direction)
        if mixed and not args.force:
            raise SystemExit(f"Направление {args.direction!r} смешивает {mixed}. "
                             "Одним паспортом оно не описывается; если всё равно "
                             "нужно — --force.")

    level = Path(args.level)
    product = (args.product or level.name).strip().lower()
    if not product or "/" in product or "\\" in product:
        raise SystemExit(f"Негодное имя продукта: {product!r}")

    passport = read_passport(level)
    out = projection(passport, source=str(level), today=date.today().isoformat())
    missing = [f for f in FIELDS if f not in out]

    block = semantic.passport_block(out)
    print(f"Продукт: {product}")
    print(f"Разделов: {len(out) - 2} из {len(FIELDS)}"
          + (f"; нет: {', '.join(missing)}" if missing else ""))
    print(f"Длина блока в промпте: {len(block)} симв. "
          f"(предел {semantic.PASSPORT_BUDGET})")
    if not block:
        raise SystemExit("Проекция пуста: в промпт ехать нечему.")

    campaigns = campaigns_of(level)
    for extra in args.also_level:
        campaigns += campaigns_of(Path(extra))
    campaigns = sorted(set(campaigns))
    print(f"Кампаний в журнале билдера: {len(campaigns)}"
          + (f" ({', '.join(campaigns)})" if campaigns else
             " — адресация только по направлению"))
    if not campaigns and not args.direction:
        raise SystemExit("Ни кампаний, ни направления: паспорт был бы недостижим.")

    path = Path(semantic.PASSPORTS_DIR) / f"{product}.json"
    index_path = Path(semantic.PASSPORTS_DIR) / semantic.PASSPORT_INDEX
    index = merged_index(product, campaigns, args.direction)
    if not args.apply:
        print(f"\n--- так выглядит в промпте ---\n{block}\n")
        print(f"Ничего не записано. Файлы были бы: {path}, {index_path}")
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8", newline="\n")
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=1) + "\n",
                          encoding="utf-8", newline="\n")
    print(f"Записано: {path}")
    print(f"Карта: {index_path} "
          f"({len(index['by_campaign'])} кампаний, "
          f"{len(index['by_direction'])} направлений)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
