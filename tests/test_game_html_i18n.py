"""Храповик локализации мини-игр: `game.html`/`game_squad.html` — статические
файлы, отдаются `FileResponse` (см. game_server.py), шаблонизатора нет — язык
выбирается прямо в браузере через словарь `STR`/массив `QUIPS` в самом JS (по
`Telegram.WebApp.initDataUnsafe.user.language_code`, см. комментарий у `LANG`
в обоих файлах).

`i18n_coverage.py` этого не видит вообще: он читает только `.py`-модули через
`ast`, а тут — HTML с инлайновым JS. Без отдельной защиты новый русский текст
в `STR`/`QUIPS` мог бы попасть в код без английской пары, и это не поймала бы
ни одна из трёх защит test_i18n_no_leaks.py — все они про `locales/*.json` и
`i18n_coverage.LOCALIZED`, где мини-игр нет и не будет (см. TASK/README при
переводе).

Разбор — свой маленький парсер, а не полноценный JS-движок: формат объекта и
массива, которые мы сами пишем в этих файлах, простой и стабильный (пары
`ключ: ['ru', 'en']` и `LANG === 'en' ? [...] : [...]`), и разбирать его через
node — лишняя внешняя зависимость для теста, которая не гарантированно есть в
CI. Кавычки — одинарные или двойные (двойные — когда в английской строке есть
апостроф, см. `subtitle` в game.html), с обычным `\\`-экранированием.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_GAME_FILES = ["game.html", "game_squad.html"]

# Строка в одинарных ИЛИ двойных кавычках, с `\`-экранированием внутри —
# ровно то, что умеет писать наш собственный код (см. game.html/game_squad.html).
_STRING = r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\""
_ITEM_RE = re.compile(_STRING, re.S)
_PAIR_RE = re.compile(
    r"(?P<key>[A-Za-z_][A-Za-z0-9_]*):\s*\[\s*"
    r"(?P<ru>" + _STRING + r")\s*,\s*"
    r"(?P<en>" + _STRING + r")\s*,?\s*\]",
    re.S,
)
_DECLARED_KEY_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*):\s*\[", re.M)


def _unquote(token: str) -> str:
    quote = token[0]
    body = token[1:-1]
    return body.replace("\\" + quote, quote).replace("\\\\", "\\")


def _matching_brace(text: str, open_pos: int) -> int:
    """text[open_pos] == '{' -> позиция ПОСЛЕ парной '}' (кавычки не считаем)."""
    depth = 0
    i = open_pos
    in_str: str | None = None
    while i < len(text):
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
        else:
            if c in ("'", '"'):
                in_str = c
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return i + 1
        i += 1
    raise AssertionError("не нашёл парную '}' — файл повреждён или маркер сдвинулся")


def _extract_str_dict(text: str) -> dict[str, tuple[str, str]]:
    """Все пары `ключ: ['ru', 'en']` из `const STR = { ... };`."""
    marker = "const STR = {"
    start = text.index(marker)
    brace_start = text.index("{", start)
    end = _matching_brace(text, brace_start)
    block = text[brace_start:end]

    pairs: dict[str, tuple[str, str]] = {}
    for m in _PAIR_RE.finditer(block):
        pairs[m.group("key")] = (_unquote(m.group("ru")), _unquote(m.group("en")))

    # Сверка «нашли столько же записей, сколько объявлено» — страховка от
    # того, что регэксп молча пропустил запись с непривычным форматированием.
    declared = _DECLARED_KEY_RE.findall(block)
    missing = [k for k in declared if k not in pairs]
    assert not missing, f"не разобрал записи STR: {missing} (проверь формат в исходнике)"
    return pairs


def _extract_bracket(text: str, open_pos: int) -> tuple[list[str], int]:
    """text[open_pos] == '[' -> (JS-строки внутри, позиция после ']')."""
    assert text[open_pos] == "["
    i = open_pos + 1
    items: list[str] = []
    while True:
        while text[i] in " \t\r\n":
            i += 1
        if text[i] == "]":
            return items, i + 1
        m = _ITEM_RE.match(text, i)
        assert m, f"ожидал строку в массиве QUIPS на позиции {i}: {text[i:i + 40]!r}"
        items.append(_unquote(m.group()))
        i = m.end()
        while text[i] in " \t\r\n":
            i += 1
        if text[i] == ",":
            i += 1


def _extract_quips(text: str) -> tuple[list[str], list[str]]:
    """(ru-ветка, en-ветка) массива `const QUIPS = LANG === 'en' ? [...] : [...]`."""
    marker = "const QUIPS = LANG === 'en' ? "
    start = text.index(marker) + len(marker)
    en_items, after_en = _extract_bracket(text, start)
    colon = text.index(":", after_en)
    ru_bracket = text.index("[", colon)
    ru_items, _ = _extract_bracket(text, ru_bracket)
    return ru_items, en_items


@pytest.mark.parametrize("filename", _GAME_FILES)
def test_every_str_entry_has_a_real_english_translation(filename):
    """Каждый ключ словаря `STR` обязан иметь непустую и отличную от русской
    английскую пару — иначе `LANG === 'en'` молча покажет русский текст."""
    text = (_ROOT / filename).read_text(encoding="utf-8")
    pairs = _extract_str_dict(text)

    assert pairs, f"{filename}: словарь STR пуст или не найден"
    for key, (ru, en) in pairs.items():
        assert ru.strip(), f"{filename}: STR.{key} — пустая русская строка"
        assert en.strip(), f"{filename}: STR.{key} — нет английского перевода"
        assert ru != en, f"{filename}: STR.{key} — английский совпадает с русским (не перевели)"


@pytest.mark.parametrize("filename", _GAME_FILES)
def test_quips_have_a_matching_english_branch(filename):
    """QUIPS — не словарь (порядок важен, `pick(QUIPS)` берёт по индексу),
    поэтому проверяем отдельно: обе ветки одной длины, ни одна фраза не путает
    языки и не остаётся непереведённой."""
    text = (_ROOT / filename).read_text(encoding="utf-8")
    ru_items, en_items = _extract_quips(text)

    assert ru_items and en_items, f"{filename}: не нашёл QUIPS"
    assert len(ru_items) == len(en_items), (
        f"{filename}: в ru-ветке QUIPS {len(ru_items)} фраз, в en-ветке {len(en_items)} — "
        "pick(QUIPS) на одном языке взял бы фразу не в тон"
    )
    for i, (ru, en) in enumerate(zip(ru_items, en_items, strict=True)):
        assert ru.strip(), f"{filename}: QUIPS[ru][{i}] пустая"
        assert en.strip(), f"{filename}: QUIPS[en][{i}] пустая"
        assert ru != en, f"{filename}: QUIPS[{i}] — английская фраза совпадает с русской"


@pytest.mark.parametrize("filename", _GAME_FILES)
def test_lang_detection_and_html_lang_are_wired(filename):
    """Регресс на сам приём: без `LANG`/`document.documentElement.lang` вся эта
    защита проверяла бы словарь, который никто не читает."""
    text = (_ROOT / filename).read_text(encoding="utf-8")
    assert "initDataUnsafe" in text, f"{filename}: не читает язык из Telegram WebApp initData"
    assert "document.documentElement.lang = LANG" in text
    assert "document.title = tr('title')" in text
