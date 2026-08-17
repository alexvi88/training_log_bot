"""Конвертирует locales/*.json (плоский ICU-каталог) в Xcode String Catalog.

Формат .xcstrings проверен по документации Apple ("Localizing and Varying
Text with a String Catalog", developer.apple.com) на момент написания:

    {
      "sourceLanguage": "ru",
      "version": "1.0",
      "strings": {
        "<ключ>": {
          "localizations": {
            "en": {"stringUnit": {"state": "translated", "value": "..."}},
            "ru": {"variations": {"plural": {
                "one": {"stringUnit": {"state": "translated", "value": "..."}},
                "other": {"stringUnit": {"state": "translated", "value": "..."}}
            }}}
          }
        }
      }
    }

Важный нюанс, подтверждённый документацией: ``variations`` вложен ВНУТРЬ
конкретной локализации (``localizations.<lang>.variations.plural...``), а не
рядом со ``stringUnit`` на уровне ключа — для каждого языка используется либо
``stringUnit``, либо ``variations``, но не оба сразу.

Если значение в locales/*.json целиком состоит из одной ICU-плюрализации
(``{var, plural, one{...} other{...}}`` без текста снаружи) — раскладываем по
категориям в variations.plural. Если plural-конструкция — только часть более
длинной строки, ICU там валиден как есть для будущего рантайма, поэтому
оставляем плоский stringUnit со значением без изменений (и считаем такие
случаи в отчёте).

Запуск:

    python scripts/i18n_export_xcstrings.py
    python scripts/i18n_export_xcstrings.py --out Localizable.xcstrings
"""

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCALES_DIR = REPO_ROOT / "locales"

# Целиком-строка вида "{var, plural, <branches>}" — var и тело плюрализации.
PLURAL_WHOLE_RE = re.compile(r"^\{\s*(\w+)\s*,\s*plural\s*,\s*(.*)\}\s*$", re.DOTALL)


def parse_plural_branches(body: str) -> dict[str, str] | None:
    """Разбирает `one{..} few{..} other{..}` с учётом вложенных `{}` внутри веток.

    Возвращает None, если после разбора всех веток в теле осталось что-то,
    кроме пробелов, — значит, это не чистая ICU-плюрализация целиком.
    """
    branches: dict[str, str] = {}
    i = 0
    n = len(body)
    while i < n:
        m = re.match(r"\s*(\w+)\s*\{", body[i:])
        if not m:
            break
        i += m.end()
        depth = 1
        start = i
        while i < n and depth > 0:
            if body[i] == "{":
                depth += 1
            elif body[i] == "}":
                depth -= 1
            i += 1
        if depth != 0:
            return None  # незакрытая скобка — не наш случай
        branches[m.group(1)] = body[start : i - 1]
    if body[i:].strip():
        return None  # хвост после веток — конструкция не покрывает всю строку
    return branches or None


def parse_whole_plural(value: str) -> tuple[str, dict[str, str]] | None:
    match = PLURAL_WHOLE_RE.match(value.strip())
    if not match:
        return None
    var_name, body = match.groups()
    branches = parse_plural_branches(body)
    if branches is None:
        return None
    return var_name, branches


def load_locales() -> dict[str, dict[str, str]]:
    locales: dict[str, dict[str, str]] = {}
    for path in sorted(LOCALES_DIR.glob("*.json")):
        lang = path.stem
        locales[lang] = json.loads(path.read_text(encoding="utf-8"))
    return locales


def build_localization_entry(value: str, embedded_plural_counter: list) -> dict:
    parsed = parse_whole_plural(value)
    if parsed is not None:
        _var_name, branches = parsed
        plural_variations = {
            category: {"stringUnit": {"state": "translated", "value": content}}
            for category, content in branches.items()
        }
        return {"variations": {"plural": plural_variations}}
    if "plural," in value:
        # Похоже на ICU-плюрал, но не покрывает всю строку целиком —
        # оставляем как плоский текст, ICU внутри останется валиден для
        # рантайма, но Apple тут не сможет показать отдельные формы.
        embedded_plural_counter[0] += 1
    return {"stringUnit": {"state": "translated", "value": value}}


def build_catalog(locales: dict[str, dict[str, str]], source_language: str) -> tuple[dict, int, int]:
    keys = sorted({key for catalog in locales.values() for key in catalog})
    strings: dict[str, dict] = {}
    plural_keys = 0
    embedded_plural_counter = [0]
    for key in keys:
        localizations = {}
        had_plural_variation = False
        for lang, catalog in locales.items():
            if key not in catalog:
                continue
            entry = build_localization_entry(catalog[key], embedded_plural_counter)
            if "variations" in entry:
                had_plural_variation = True
            localizations[lang] = entry
        if had_plural_variation:
            plural_keys += 1
        strings[key] = {"localizations": localizations}
    catalog_doc = {
        "sourceLanguage": source_language,
        "version": "1.0",
        "strings": strings,
    }
    return catalog_doc, plural_keys, embedded_plural_counter[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=Path("Localizable.xcstrings"), help="Путь для выходного файла")
    parser.add_argument("--source-language", default="ru", help="sourceLanguage каталога (по умолчанию ru)")
    args = parser.parse_args()

    if not LOCALES_DIR.is_dir():
        print(f"Каталог локализации не найден: {LOCALES_DIR}", file=sys.stderr)
        return 1
    locale_files = sorted(LOCALES_DIR.glob("*.json"))
    if not locale_files:
        print(f"В {LOCALES_DIR} нет ни одного .json файла", file=sys.stderr)
        return 1

    locales = load_locales()
    catalog_doc, plural_keys, embedded_plural_count = build_catalog(locales, args.source_language)

    args.out.write_text(json.dumps(catalog_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    total_keys = len(catalog_doc["strings"])
    print(f"Прочитано локалей: {', '.join(sorted(locales))}")
    print(f"Записан {args.out} ({total_keys} ключей)")
    print(f"  из них с variations.plural: {plural_keys}")
    print(f"  plural внутри более длинной строки (оставлены плоским ICU-текстом): {embedded_plural_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
