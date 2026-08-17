"""i18n core: язык пользователя, каталоги строк и ICU-подобный форматтер.

Модуль намеренно не знает ни про aiogram, ни про db — только stdlib. Он должен
импортироваться из любого места (хендлеры, фоновые джобы, тесты) без риска
циклического импорта.

Формат каталога — плоский JSON `str -> str` в `locales/<lang>.json`. Значения
могут содержать подмножество ICU MessageFormat: простую подстановку `{name}`,
плюрализацию `{n, plural, one{...} few{...} many{...} other{...}}`, `#` внутри
ветки plural (сама цифра) и выбор по полу `{g, select, male{...} other{...}}`.
Апостроф — служебный символ ICU-кавычек: `''` даёт литеральный апостроф,
`'{'`/`'}'` — литеральные фигурные скобки (иначе их не вставить в текст).

Важно: кавычку открывает НЕ любой апостроф, а только стоящий перед `{`, `}`,
`#` или `|` — так это описано в ICU и так это критично для английского, где
сокращения обязательны по тон-оф-войсу. Апостроф в `don't` — литерал; если
считать его открывающей кавычкой, строка молча съест кусок текста до
следующего апострофа или до конца.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_LANG = "ru"
SUPPORTED: tuple[str, ...] = ("ru", "en")

# Языки СНГ/постсоветского пространства — у их пользователей телеграмный
# language_code часто не "ru", но продукт для них по-прежнему на русском.
_CYRILLIC_SPHERE = {"ru", "uk", "be", "kk", "ky", "hy", "az", "uz"}

_LOCALES_DIR = Path(__file__).resolve().parent / "locales"

# Каталоги и разобранные ICU-деревья кэшируются лениво — на диск ходим один
# раз за процесс, а не на каждый вызов t().
_catalogs: dict[str, dict[str, str]] = {}
_ast_cache: dict[str, list[tuple]] = {}
# Ключи, по которым уже пожаловались в лог — чтобы не заспамить прод одним и
# тем же WARNING на каждый рендер экрана.
_warned_missing_keys: set[str] = set()


def normalize(code: str | None) -> str:
    """Телеграмный language_code -> наш язык. Регистр и суффиксы (en-US) не важны."""
    if not code:
        return DEFAULT_LANG
    base = code.strip().lower().replace("_", "-").split("-")[0]
    if not base:
        return DEFAULT_LANG
    return "ru" if base in _CYRILLIC_SPHERE else "en"


current_lang: contextvars.ContextVar[str] = contextvars.ContextVar("current_lang", default=DEFAULT_LANG)


def get_lang() -> str:
    return current_lang.get()


def set_lang(lang: str) -> None:
    current_lang.set(lang if lang in SUPPORTED else DEFAULT_LANG)


@contextlib.contextmanager
def use_lang(lang: str):
    """Временно переключить язык в текущем контексте (для фоновых джобов,
    которые в одной задаче рендерят тексты по очереди для разных пользователей).
    """
    previous = get_lang()
    token = current_lang.set(lang if lang in SUPPORTED else DEFAULT_LANG)
    try:
        yield previous
    finally:
        current_lang.reset(token)


def reload() -> None:
    """Сбросить кэши каталогов/AST и список уже прозвучавших WARNING — для тестов."""
    _catalogs.clear()
    _ast_cache.clear()
    _warned_missing_keys.clear()


def _load_catalog(lang: str) -> dict[str, str]:
    if lang not in _catalogs:
        path = _LOCALES_DIR / f"{lang}.json"
        try:
            with path.open(encoding="utf-8") as f:
                _catalogs[lang] = json.load(f)
        except FileNotFoundError:
            _catalogs[lang] = {}
    return _catalogs[lang]


def _resolve(lang: str, key: str) -> tuple[str, str | None]:
    """Вернуть (шаблон, язык-на-котором-нашли) либо (key, None), если ключа нет нигде."""
    catalog = _load_catalog(lang)
    if key in catalog:
        return catalog[key], lang
    if lang != DEFAULT_LANG:
        ru_catalog = _load_catalog(DEFAULT_LANG)
        if key in ru_catalog:
            return ru_catalog[key], DEFAULT_LANG
    if key not in _warned_missing_keys:
        _warned_missing_keys.add(key)
        logger.warning("i18n: ключ %r не найден ни в %r, ни в fallback %r", key, lang, DEFAULT_LANG)
    return key, None


# --- ICU-подобный парсер -----------------------------------------------------
#
# Символы, перед которыми апостроф работает кавычкой. Всё остальное («don't»,
# «that's») — обычный литеральный апостроф английского текста.
_QUOTABLE = "{}#|"


def _opens_quote(s: str, i: int) -> bool:
    return i + 1 < len(s) and s[i + 1] in _QUOTABLE


#
# Пишем рекурсивным спуском вручную: ветки plural/select сами содержат
# {name}-подстановки и могут быть вложены произвольно, а простая регулярка на
# вложенных фигурных скобках либо не матчит, либо матчит неправильно.

def _extract_balanced(s: str, pos: int) -> tuple[str, int]:
    """s[pos] == '{'. Вернуть (содержимое без внешних скобок, позиция после '}')."""
    depth = 1
    i = pos + 1
    n = len(s)
    while i < n:
        c = s[i]
        if c == "'":
            if i + 1 < n and s[i + 1] == "'":
                i += 2
                continue
            if _opens_quote(s, i):
                # Кавычки экранируют всё внутри, включая фигурные скобки — их не считаем.
                j = s.find("'", i + 1)
                i = n if j == -1 else j + 1
                continue
            i += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[pos + 1 : i], i + 1
        i += 1
    raise ValueError(f"незакрытая '{{' в строке каталога: {s!r}")


def _split_top_level(s: str, sep: str, maxsplit: int) -> list[str]:
    """Разбить по sep, но только вне {..} и вне '...' — там разделитель не считается."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    i = 0
    n = len(s)
    count = 0
    while i < n:
        c = s[i]
        if c == "'":
            if i + 1 < n and s[i + 1] == "'":
                buf.append("''")
                i += 2
                continue
            if _opens_quote(s, i):
                # Кавычки переносим как есть: строку ещё будет разбирать parse_nodes.
                j = s.find("'", i + 1)
                if j == -1:
                    buf.append(s[i:])
                    i = n
                else:
                    buf.append(s[i : j + 1])
                    i = j + 1
                continue
            buf.append("'")
            i += 1
            continue
        if c == "{":
            depth += 1
            buf.append(c)
            i += 1
            continue
        if c == "}":
            depth -= 1
            buf.append(c)
            i += 1
            continue
        if c == sep and depth == 0 and count < maxsplit:
            parts.append("".join(buf))
            buf = []
            count += 1
            i += 1
            continue
        buf.append(c)
        i += 1
    parts.append("".join(buf))
    return parts


def _parse_branches(s: str) -> dict[str, list[tuple]]:
    """s — это ' one{...} few{...} other{...}' (текст после типа плюрализации/select)."""
    branches: dict[str, list[tuple]] = {}
    i = 0
    n = len(s)
    while i < n:
        while i < n and s[i].isspace():
            i += 1
        if i >= n:
            break
        start = i
        while i < n and (s[i].isalnum() or s[i] == "_"):
            i += 1
        label = s[start:i]
        if not label:
            raise ValueError(f"не удалось разобрать ветки в строке каталога: {s!r}")
        while i < n and s[i].isspace():
            i += 1
        if i >= n or s[i] != "{":
            raise ValueError(f"после метки {label!r} ожидалась '{{' в {s!r}")
        inner, i = _extract_balanced(s, i)
        branches[label] = parse_nodes(inner)
    return branches


def _parse_placeholder(inner: str) -> tuple:
    parts = [p.strip() for p in _split_top_level(inner, ",", maxsplit=2)]
    if len(parts) == 1:
        return ("var", parts[0])
    name, kind = parts[0], parts[1]
    rest = parts[2] if len(parts) > 2 else ""
    if kind not in ("plural", "select"):
        raise ValueError(f"неизвестный ICU-тип {kind!r} в плейсхолдере {{{inner}}}")
    return (kind, name, _parse_branches(rest))


def parse_nodes(s: str) -> list[tuple]:
    """Разобрать строку каталога в список узлов: текст / '#' / var / plural / select."""
    nodes: list[tuple] = []
    buf: list[str] = []
    i = 0
    n = len(s)

    def flush() -> None:
        if buf:
            nodes.append(("text", "".join(buf)))
            buf.clear()

    while i < n:
        c = s[i]
        if c == "'":
            if i + 1 < n and s[i + 1] == "'":
                buf.append("'")
                i += 2
                continue
            if _opens_quote(s, i):
                j = s.find("'", i + 1)
                if j == -1:
                    buf.append(s[i + 1 :])
                    i = n
                else:
                    buf.append(s[i + 1 : j])
                    i = j + 1
                continue
            buf.append("'")
            i += 1
            continue
        if c == "#":
            flush()
            nodes.append(("hash",))
            i += 1
            continue
        if c == "{":
            flush()
            inner, i = _extract_balanced(s, i)
            nodes.append(_parse_placeholder(inner))
            continue
        buf.append(c)
        i += 1
    flush()
    return nodes


def _get_ast(template: str) -> list[tuple]:
    ast = _ast_cache.get(template)
    if ast is None:
        ast = parse_nodes(template)
        _ast_cache[template] = ast
    return ast


def _plural_category(n: int, lang: str) -> str:
    n = abs(n)
    if lang == "ru":
        if n % 10 == 1 and n % 100 != 11:
            return "one"
        if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
            return "few"
        return "many"
    # en и всё остальное — обычное английское правило.
    return "one" if n == 1 else "other"


def _render_nodes(
    nodes: list[tuple],
    params: dict[str, Any],
    catalog_key: str,
    lang: str,
    plural_n: int | None = None,
) -> str:
    out: list[str] = []
    for node in nodes:
        kind = node[0]
        if kind == "text":
            out.append(node[1])
        elif kind == "hash":
            # '#' значим только внутри ветки plural — вне неё это просто литерал.
            out.append(str(plural_n) if plural_n is not None else "#")
        elif kind == "var":
            name = node[1]
            if name not in params:
                raise KeyError(f"каталог: ключ {catalog_key!r}, не хватает параметра {name!r}")
            out.append(str(params[name]))
        elif kind in ("plural", "select"):
            _, varname, branches = node
            if varname not in params:
                raise KeyError(f"каталог: ключ {catalog_key!r}, не хватает параметра {varname!r}")
            value = params[varname]
            if kind == "plural":
                n_int = int(value)
                category = _plural_category(n_int, lang)
                # `in`, а не `or`: пустая ветка (`one{}`) — законный приём, ею
                # гасят слово целиком. У `or` пустой список ложный, и такая
                # ветка молча подменялась бы веткой `other`.
                branch_nodes = branches[category] if category in branches else branches.get("other")
                if branch_nodes is None:
                    raise ValueError(
                        f"каталог: ключ {catalog_key!r}, нет ветки {category!r} и нет 'other' для plural"
                    )
                out.append(_render_nodes(branch_nodes, params, catalog_key, lang, plural_n=n_int))
            else:
                category = str(value)
                branch_nodes = branches[category] if category in branches else branches.get("other")
                if branch_nodes is None:
                    raise ValueError(
                        f"каталог: ключ {catalog_key!r}, нет ветки {category!r} и нет 'other' для select"
                    )
                out.append(_render_nodes(branch_nodes, params, catalog_key, lang, plural_n=plural_n))
    return "".join(out)


def t_in(lang: str, key: str, **params: Any) -> str:
    template, resolved_lang = _resolve(lang, key)
    if resolved_lang is None:
        # Ключа нет ни в запрошенном языке, ни в ru-fallback — отдаём его как есть,
        # WARNING уже залогирован внутри _resolve (один раз на ключ).
        return template
    nodes = _get_ast(template)
    return _render_nodes(nodes, params, key, resolved_lang)


def t(key: str, **params: Any) -> str:
    return t_in(get_lang(), key, **params)
