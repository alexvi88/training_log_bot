"""Помогает вынимать русские строковые литералы из модуля в каталог локализации.

Разбирает файл через ``ast``, находит строковые константы с кириллицей и
предлагает для каждой ключ вида ``<prefix>.<slug>`` и ICU-значение (с учётом
подстановок из f-строк). НИЧЕГО не переписывает в исходном .py — только
печатает таблицу-предложение и, по флагу ``--json``, пишет фрагмент каталога
в отдельный файл. Это осознанное решение: автозамена литералов по всему
репозитории (~89k строк) без ревью человека опаснее, чем ручной перенос —
скрипт экономит время на подбор ключей и текста, а не заменяет ревьюера.

Докстринги модуля/функций/классов исключаются из выдачи — это комментарии
для разработчика, а не пользовательские тексты, и их в разы больше, чем
реальных строк экрана.

Запуск:

    python scripts/i18n_extract.py bot/screens/workout.py --prefix screen.workout
    python scripts/i18n_extract.py bot/screens/workout.py --prefix screen.workout --json out.json
"""

import argparse
import ast
import json
import re
import sys
from pathlib import Path

CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
WORD_RE = re.compile(r"[А-Яа-яЁё]+")

# Практическая транслитерация RU -> LAT для ключей (не ГОСТ, просто читаемо).
TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

MAX_SLUG_WORDS = 4


def has_cyrillic(text: str) -> bool:
    return bool(CYRILLIC_RE.search(text))


def translit_word(word: str) -> str:
    return "".join(TRANSLIT.get(ch.lower(), ch.lower()) for ch in word)


def make_slug(text: str) -> str:
    """Транслитерация первых 3-4 значимых слов текста в snake_case."""
    words = WORD_RE.findall(text)[:MAX_SLUG_WORDS]
    if not words:
        return "text"
    return "_".join(translit_word(w) for w in words)


def expr_to_placeholder_name(node: ast.expr, counter: list) -> tuple[str, bool]:
    """Возвращает (имя_плейсхолдера, нужна_ли_ручная_правка) для FormattedValue.

    Простое имя (``name``) или атрибут (``obj.attr``) выводится как есть.
    Всё остальное (вызовы функций, арифметика и т.п.) помечается как
    ``argN`` — такие места требуют ручной правки промпта/строки.
    """
    if isinstance(node, ast.Name):
        return node.id, False
    if isinstance(node, ast.Attribute):
        parts = []
        cur: ast.expr = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
            return ".".join(reversed(parts)), False
    counter[0] += 1
    return f"arg{counter[0]}", True


def collect_docstring_ids(tree: ast.AST) -> set[int]:
    """id() строковых Constant-узлов, которые являются докстрингами."""
    docstring_ids: set[int] = set()
    doc_owner_types = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    for node in ast.walk(tree):
        if isinstance(node, doc_owner_types):
            body = getattr(node, "body", None)
            if not body:
                continue
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(
                first.value.value, str
            ):
                docstring_ids.add(id(first.value))
    return docstring_ids


class Extraction:
    __slots__ = ("lineno", "text", "value", "placeholders", "needs_review")

    def __init__(self, lineno: int, text: str, value: str, placeholders: list[str], needs_review: bool):
        self.lineno = lineno
        self.text = text
        self.value = value
        self.placeholders = placeholders
        self.needs_review = needs_review


class Extractor(ast.NodeVisitor):
    """Собирает кириллические литералы, пропуская докстринги.

    f-строки (``JoinedStr``) обрабатываются целиком в ``visit_JoinedStr`` и не
    спускаются внутрь через ``generic_visit`` — иначе их литеральные куски
    попали бы в выдачу ещё и как обычные ``Constant``.
    """

    def __init__(self, docstring_ids: set[int]):
        self.docstring_ids = docstring_ids
        self.results: list[Extraction] = []

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and id(node) not in self.docstring_ids and has_cyrillic(node.value):
            self.results.append(Extraction(node.lineno, node.value, node.value, [], False))

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        literal_has_cyrillic = False
        text_parts: list[str] = []
        display_parts: list[str] = []
        placeholders: list[str] = []
        needs_review = False
        counter = [0]
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                text_parts.append(part.value)
                display_parts.append(part.value)
                if has_cyrillic(part.value):
                    literal_has_cyrillic = True
            elif isinstance(part, ast.FormattedValue):
                name, review = expr_to_placeholder_name(part.value, counter)
                placeholders.append(name)
                needs_review = needs_review or review
                text_parts.append("{" + name + "}")
                display_parts.append("{" + name + "}")
        if literal_has_cyrillic:
            original = "".join(text_parts)
            value = "".join(display_parts)
            self.results.append(Extraction(node.lineno, original, value, placeholders, needs_review))
        # Не рекурсируем внутрь: Constant-куски уже учтены выше.


def extract_from_file(path: Path) -> list[Extraction]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    docstring_ids = collect_docstring_ids(tree)
    extractor = Extractor(docstring_ids)
    extractor.visit(tree)
    extractor.results.sort(key=lambda e: e.lineno)
    return extractor.results


def assign_keys(results: list[Extraction], prefix: str) -> list[tuple[str, Extraction]]:
    used: dict[str, int] = {}
    keyed: list[tuple[str, Extraction]] = []
    for extraction in results:
        slug = make_slug(extraction.text)
        key = f"{prefix}.{slug}" if prefix else slug
        if key in used:
            used[key] += 1
            key = f"{key}_{used[key]}"
        else:
            used[key] = 1
        keyed.append((key, extraction))
    return keyed


def print_table(keyed: list[tuple[str, Extraction]], file_path: Path) -> None:
    print(f"Найдено кириллических литералов: {len(keyed)} в {file_path}\n")
    header = f"{'line':>5}  {'ключ':<40}  {'значение (ICU)'}"
    print(header)
    print("-" * len(header))
    needs_review_lines = []
    for key, extraction in keyed:
        print(f"{extraction.lineno:>5}  {key:<40}  {extraction.value!r}")
        if extraction.needs_review:
            needs_review_lines.append((extraction.lineno, key))
    if needs_review_lines:
        print("\nТребуют ручной правки (сложное выражение внутри f-строки, не имя/атрибут):")
        for lineno, key in needs_review_lines:
            print(f"  строка {lineno}: {key}")


def write_json(keyed: list[tuple[str, Extraction]], out_path: Path) -> None:
    catalog = {key: extraction.value for key, extraction in keyed}
    out_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nФрагмент каталога записан в {out_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file", type=Path, help="Путь к .py файлу, из которого извлекать строки")
    parser.add_argument("--prefix", default="", help="Префикс ключа, например screen.workout")
    parser.add_argument("--json", dest="json_out", type=Path, default=None, help="Куда записать фрагмент каталога")
    args = parser.parse_args()

    if not args.file.is_file():
        print(f"Файл не найден: {args.file}", file=sys.stderr)
        return 1

    results = extract_from_file(args.file)
    keyed = assign_keys(results, args.prefix)
    print_table(keyed, args.file)
    if args.json_out is not None:
        write_json(keyed, args.json_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
