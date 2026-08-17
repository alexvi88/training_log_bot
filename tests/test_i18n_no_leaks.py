"""Механическая защита от русского текста в английской версии продукта — три
независимых слоя, см. постановку задачи. Каждый слой ловит свой класс утечки:

  1. каталог (locales/*.json) сам по себе не протекает и не бьётся при парсинге;
  2. модули из i18n_coverage.LOCALIZED не заводят новую кириллицу украдкой;
  3. живой рендер нескольких ключевых экранов на lang="en" не содержит кириллицы
     — это единственный слой, который ловит текст, идущий МИМО каталога
     (хардкод в formatting.py/keyboards.py и т.п.), поэтому он самый ценный и
     единственный, что реально трогает прод-код напрямую.

Слои 1 и 2 не трогают ни один существующий .py/.json — они читают то, что уже
лежит в repo, и разбирают его тем же способом, что и остальной проект
(i18n.parse_nodes, ast, как в scripts/i18n_extract.py).
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery

import formatting
import i18n
import i18n_coverage
import keyboards
import view_builder
from handlers import history

# asyncio_mode = auto (pyproject.toml) детектит async def сам — pytestmark тут
# не нужен, а на синхронных тестах этого файла (слои 1 и 2) он бы только
# сыпал PytestWarning.

_ROOT = Path(__file__).resolve().parent.parent

# Ключи test.* — фикстуры для юнит-тестов самого i18n.py (tests/test_i18n.py),
# в том числе намеренно однобокие вроде "test.ru_only" (проверяет ru-fallback,
# когда ключа в en нет). Это не продуктовый каталог — слой 1 их не касается.
_TEST_FIXTURE_PREFIX = "test."


def _product_keys(catalog: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in catalog.items() if not k.startswith(_TEST_FIXTURE_PREFIX)}


_LOCALES = _ROOT / "locales"

# Автонимы названий языков — единственное официально разрешённое исключение
# из "английский каталог без кириллицы": слово "Русский" в переключателе языка
# не переводится, см. keyboards.LANG_NAMES и TONE_OF_VOICE.md (English voice).
_AUTONYM_WHITELIST = {"Русский"}


def _load_catalog(lang: str) -> dict[str, str]:
    with (_LOCALES / f"{lang}.json").open(encoding="utf-8") as f:
        return json.load(f)


def _placeholder_names(nodes: list[tuple], out: set[str] | None = None) -> set[str]:
    """Имена {var}/plural/select-переменных, вложенность разбирается рекурсивно
    через те же узлы, что строит i18n.parse_nodes — а не регуляркой, которая
    не видит вложенные {..} внутри веток plural/select."""
    if out is None:
        out = set()
    for node in nodes:
        kind = node[0]
        if kind == "var":
            out.add(node[1])
        elif kind in ("plural", "select"):
            _, varname, branches = node
            out.add(varname)
            for branch_nodes in branches.values():
                _placeholder_names(branch_nodes, out)
    return out


def _plural_branch_labels(nodes: list[tuple], out: set[str] | None = None) -> set[str]:
    """Метки веток (one/few/many/other/...) у всех plural-узлов дерева, включая
    вложенные внутрь select/plural — чтобы не пропустить plural, спрятанный
    внутри {g, select, ...}."""
    if out is None:
        out = set()
    for node in nodes:
        kind = node[0]
        if kind == "plural":
            _, _varname, branches = node
            out |= set(branches.keys())
            for branch_nodes in branches.values():
                _plural_branch_labels(branch_nodes, out)
        elif kind == "select":
            _, _varname, branches = node
            for branch_nodes in branches.values():
                _plural_branch_labels(branch_nodes, out)
    return out


# === Слой 1: каталог сам по себе ============================================


def test_catalogs_have_matching_keys():
    ru, en = _product_keys(_load_catalog("ru")), _product_keys(_load_catalog("en"))
    missing_in_en = sorted(set(ru) - set(en))
    missing_in_ru = sorted(set(en) - set(ru))
    assert not missing_in_en, f"есть в ru.json, нет в en.json: {missing_in_en}"
    assert not missing_in_ru, f"есть в en.json, нет в ru.json: {missing_in_ru}"


def test_catalogs_parse_with_icu_parser():
    """Битый ICU (незакрытая скобка, неизвестный тип плейсхолдера, ветка без
    'other') должен падать здесь, а не первым рендером в проде."""
    for lang in ("ru", "en"):
        catalog = _load_catalog(lang)
        for key, template in catalog.items():
            try:
                i18n.parse_nodes(template)
            except ValueError as exc:
                pytest.fail(f"{lang}.json[{key!r}] не парсится: {exc}")


def test_en_catalog_has_no_cyrillic():
    en = _load_catalog("en")
    leaks = {}
    for key, value in en.items():
        # Автонимы вырезаем как подстроку, а не целиком значение: строка вида
        # "🌐 Language: Русский" легитимна — кириллица в ней ровно название
        # языка, а остальное должно быть на английском.
        stripped = value
        for autonym in _AUTONYM_WHITELIST:
            stripped = stripped.replace(autonym, "")
        if i18n_coverage.has_cyrillic(stripped):
            leaks[key] = value
    assert not leaks, f"кириллица в en.json (не автоним): {leaks}"


def test_placeholders_match_between_languages():
    """Разошедшийся набор плейсхолдеров — {weight} есть в ru, но пропал в en —
    тихо подставит пустоту вместо значения, а не упадёт. Ловим на уровне AST,
    не регуляркой: она не видит {name}, спрятанный внутри ветки plural/select."""
    ru, en = _load_catalog("ru"), _load_catalog("en")
    mismatched = {}
    for key in set(ru) & set(en):
        ru_vars = _placeholder_names(i18n.parse_nodes(ru[key]))
        en_vars = _placeholder_names(i18n.parse_nodes(en[key]))
        if ru_vars != en_vars:
            mismatched[key] = {"ru": sorted(ru_vars), "en": sorted(en_vars)}
    assert not mismatched, f"плейсхолдеры разошлись между языками: {mismatched}"


def test_plural_branches_are_complete():
    """ru обязан покрывать one/few/many (иначе '5 подход' на числе, которое
    ни под одну явную ветку не попало и утекло в fallback 'many'/'other'
    — тут именно СПИСОК веток, отсутствие 'few' видно сразу, а не по
    поведению на конкретном n). en обязан иметь one/other."""
    ru, en = _load_catalog("ru"), _load_catalog("en")
    bad = {}
    for key, template in ru.items():
        branches = _plural_branch_labels(i18n.parse_nodes(template))
        if not branches:
            continue  # в этом ключе вообще нет plural — не про этот тест
        missing = {"one", "few", "many"} - branches
        if missing:
            bad[f"ru:{key}"] = sorted(missing)
    for key, template in en.items():
        branches = _plural_branch_labels(i18n.parse_nodes(template))
        if not branches:
            continue
        missing = {"one", "other"} - branches
        if missing:
            bad[f"en:{key}"] = sorted(missing)
    assert not bad, f"неполные ветки плюрализации: {bad}"


# === Слой 2: реестр локализованных модулей (храповик) =======================


def test_every_module_is_classified():
    """Новый .py в ROOTS обязан попасть в один из трёх списков реестра — иначе
    его локализация молча выпадает из решения "переводим/не переводим"."""
    all_modules = set(i18n_coverage.discover_modules())
    localized = set(i18n_coverage.LOCALIZED)
    never = set(i18n_coverage.NEVER_LOCALIZED)
    todo = set(i18n_coverage.TODO)

    unclassified = all_modules - localized - never - todo
    assert not unclassified, f"не в LOCALIZED/NEVER_LOCALIZED/TODO: {sorted(unclassified)}"

    stray = (localized | never | todo) - all_modules
    assert not stray, f"в реестре, но файла больше нет — почистить: {sorted(stray)}"

    overlaps = (localized & never) | (localized & todo) | (never & todo)
    assert not overlaps, f"модуль в двух списках сразу: {sorted(overlaps)}"


@pytest.mark.parametrize("module_path", i18n_coverage.LOCALIZED)
def test_localized_module_has_no_cyrillic(module_path):
    """Храповик: модуль однажды объявили переведённым (i18n_coverage.LOCALIZED)
    — с этого момента любой новый русский литерал в нём (докстринги/комментарии
    не в счёт, см. i18n_coverage.cyrillic_literals) обязан ронять именно этот
    тест, а не тихо доехать до прода."""
    leaks = i18n_coverage.cyrillic_literals(module_path)
    assert not leaks, f"{module_path}: кириллица там, где её быть не должно: {leaks}"


# === Слой 3: рантаймовый дым =================================================
#
# Самый ценный слой — статикой не поймать текст, собранный кодом мимо каталога
# (f-строки в formatting.py/keyboards.py и т.п.). Каждый экран — это (имя,
# асинхронная функция(db, user_id) -> str). Добавить новый экран — дописать
# один элемент в SCREENS, копировать тест не нужно.


def _make_fake_callback(user_id: int, data: str) -> CallbackQuery:
    """Тот же фейковый callback, что и в tests/test_history_item_card.py:
    достаточно, чтобы handlers.history.show_history_item отработал целиком
    без реального Telegram и отдал текст экрана через .answer()."""
    message = MagicMock()
    message.text = "some previous screen"
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 1
    message.edit_text = AsyncMock(return_value=message)
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=2))
    message.delete = AsyncMock()
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _screen_settings_keyboard(db, user_id: int) -> str:
    """Экран настроек: подписи кнопок. Единственная уже переведённая строка на
    этом экране — подпись перехода на язык (см. keyboards.settings_keyboard);
    всё остальное (единицы, формула, часовой пояс, тумблеры) — пока хардкод."""
    kb = keyboards.settings_keyboard(
        unit="kg",
        formula="epley",
        pushes_enabled=True,
        ai_comments_enabled=True,
        progression_enabled=True,
        tz_offset=0,
        food_macros_enabled=True,
        show_extra_stats=True,
        show_mcp=False,
        lang="en",
    )
    return "\n".join(button.text for row in kb.inline_keyboard for button in row)


async def _screen_language_picker(db, user_id: int) -> str:
    """Экран выбора языка: заголовок из каталога + подписи кнопок. "⬅️ Назад"
    в keyboards.language_keyboard пока не переведена — экран целиком ещё
    протекает, несмотря на то что заголовок/алерт уже в en.json."""
    title = i18n.t_in("en", "screen.language.title")
    kb = keyboards.language_keyboard("en")
    buttons = "\n".join(button.text for row in kb.inline_keyboard for button in row)
    return f"{title}\n{buttons}"


async def _screen_workout_summary(db, user_id: int) -> str:
    """Карточка завершённой тренировки (formatting.build_workout_summary) —
    самый частый экран в продукте, целиком хардкод по-русски сегодня."""
    group_id = await db.create_muscle_group(user_id, "Legs")
    squat = await db.create_exercise(user_id, "Squat", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, squat, 0)
    await db.add_set(block_id, squat, 1, 0, 100.0, 5)
    blocks = await view_builder.build_block_views(workout_id)
    return formatting.build_workout_summary(dt.datetime(2026, 8, 17, 12, 0), blocks)


async def _screen_dashboard_menu(db, user_id: int) -> str:
    """Заголовок и плитки главного меню (formatting.menu_headline/menu_tiles) —
    первое, что видит атлет после /start."""
    import analytics

    dashboard = analytics.compute_dashboard(
        [dt.date(2026, 8, 10), dt.date(2026, 8, 12), dt.date(2026, 8, 15)],
        dt.date(2026, 8, 17),
    )
    headline = formatting.menu_headline(dashboard)
    tiles = formatting.menu_tiles(dashboard, tonnage=1234.0, records=2, unit="kg")
    tiles_text = "\n".join(f"{title} {value}" for title, value in tiles)
    return f"{headline}\n{tiles_text}"


async def _screen_history_item_card(db, user_id: int) -> str:
    """Детальная карточка тренировки в истории (handlers.history.show_history_item).
    ai_trainer.ensure_workout_comment внутри неё не делает сетевых вызовов,
    пока не настроен XAI_API_KEY (см. ai_trainer.is_configured) — в тестовом
    окружении его нет, так что экран рендерится офлайн, как и в
    tests/test_history_item_card.py."""
    group_id = await db.create_muscle_group(user_id, "Legs")
    squat = await db.create_exercise(user_id, "Squat", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, squat, 0)
    await db.add_set(block_id, squat, 1, 0, 200.0, 10)
    await db.finish_workout(workout_id)

    callback = _make_fake_callback(user_id, f"hist:item:{workout_id}")
    assert await history.show_history_item(callback, workout_id)
    return callback.message.answer.await_args.args[0]


# (имя экрана, сборщик) — списком, чтобы новый экран не требовал новой копии
# теста, только новую строку здесь.
SCREENS: list[tuple[str, object]] = [
    ("settings_keyboard", _screen_settings_keyboard),
    ("language_picker", _screen_language_picker),
    ("workout_summary_card", _screen_workout_summary),
    ("dashboard_menu", _screen_dashboard_menu),
    ("history_item_card", _screen_history_item_card),
]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "экраны ещё не переведены (см. i18n_coverage.TODO: formatting.py, keyboards.py, "
        "handlers/history.py); xfail снимется само, когда очередной экран очистят от "
        "кириллицы — strict=True не даст забыть снять пометку"
    ),
)
@pytest.mark.parametrize("screen_name, builder", SCREENS, ids=[name for name, _ in SCREENS])
async def test_screen_has_no_cyrillic_in_english(fresh_db, user_id, screen_name, builder):
    db = fresh_db
    await db.set_user_lang(user_id, "en")
    with i18n.use_lang("en"):
        text = await builder(db, user_id)
    leaks = [word for word in text.split() if i18n_coverage.has_cyrillic(word)]
    assert not leaks, f"{screen_name}: русские слова на английском экране: {leaks}"
