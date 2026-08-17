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
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery

import formatting
import i18n
import i18n_coverage
import keyboards
import view_builder
from handlers import edit_workout, exercises, history, routines, workout

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
    """Экран настроек: подписи кнопок — единицы, формула, часовой пояс, язык,
    все тумблеры (см. keyboards.settings_keyboard). Целиком переведён
    keyboards.py'ем: единственная кириллица, которая тут в принципе может
    всплыть, — автоним «Русский» в подписи кнопки языка, а его вырезает
    _strip_autonyms ниже, как и в test_en_catalog_has_no_cyrillic."""
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
    """Экран выбора языка: заголовок из каталога + подписи кнопок. Автоним
    «Русский» — законная кириллица даже на английском экране (см.
    keyboards.LANG_NAMES, _strip_autonyms ниже вырезает его перед проверкой)."""
    title = i18n.t_in("en", "screen.language.title")
    kb = keyboards.language_keyboard("en")
    buttons = "\n".join(button.text for row in kb.inline_keyboard for button in row)
    return f"{title}\n{buttons}"


async def _screen_main_menu(db, user_id: int) -> str:
    """Главное меню (keyboards.main_menu) — первый экран после /start, со всеми
    необязательными строками включёнными разом (импорт, донат, чат сообщества)."""
    kb = keyboards.main_menu(
        has_active_workout=False,
        show_import_button=True,
        community_url="https://t.me/example",
        show_donate=True,
    )
    return "\n".join(button.text for row in kb.inline_keyboard for button in row)


async def _screen_exercise_picker(db, user_id: int) -> str:
    """Экран «⚙️ Упражнения»: список групп мышц + список упражнений одной
    группы (keyboards.groups_keyboard/exercises_keyboard)."""
    kb_groups = keyboards.groups_keyboard(
        groups=[{"id": 1, "name": "legs"}], prefix="exm", show_all=True
    )
    kb_list = keyboards.exercises_keyboard(
        exercises=[{"id": 1, "display_name": "Squat"}], prefix="exm", show_catalog_button=True
    )
    buttons = [b.text for row in kb_groups.inline_keyboard for b in row]
    buttons += [b.text for row in kb_list.inline_keyboard for b in row]
    return "\n".join(buttons)


async def _screen_programs(db, user_id: int) -> str:
    """Экран «🗂 Программы» (keyboards.routines_manage_keyboard): многодневки,
    одиночные программы, каталог готового и сборка с AI-тренером разом."""
    kb = keyboards.routines_manage_keyboard(
        programs=[{"id": 1, "name": "Split", "day_count": 3}],
        routines=[{"id": 2, "name": "Full body"}],
        has_workouts=True,
    )
    return "\n".join(button.text for row in kb.inline_keyboard for button in row)


async def _screen_food_diary(db, user_id: int) -> str:
    """Экран одного дня дневника питания (keyboards.food_day_keyboard) — с
    записями, чтобы кнопки удаления и переход в историю тоже попали в проверку."""
    today = dt.date(2026, 8, 17)
    kb = keyboards.food_day_keyboard(today, entry_ids=[1, 2], today=today)
    return "\n".join(button.text for row in kb.inline_keyboard for button in row)


async def _screen_history_list(db, user_id: int) -> str:
    """Список тренировок в истории (keyboards.history_list_keyboard), со
    страницей вперёд, чтобы захватить и подпись листалки."""
    kb = keyboards.history_list_keyboard(
        workouts=[{"id": 1, "label": "Aug 10"}], page=0, has_next=True
    )
    return "\n".join(button.text for row in kb.inline_keyboard for button in row)


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


async def _screen_weekly_summary(db, user_id: int) -> str:
    """Недельная сводка (formatting.build_weekly_summary) — текстовый фолбэк
    для клиентов без rich-таблиц, тот же экран, что и build_weekly_table."""
    rows = [formatting.WeeklyRow(name="Squat", top_weight=100.0, tonnage=1500.0, sets_count=5)]
    return formatting.build_weekly_summary(rows, workouts=3, total_tonnage=4200.0, period="Aug 11-17")


async def _screen_achievements(db, user_id: int) -> str:
    """Экран «🏅 Достижения» (formatting.build_achievements_screen) — сетка
    открытых и запертых бейджей."""
    import achievements

    earned = {achievements.CATALOG[0].code} if achievements.CATALOG else set()
    return formatting.build_achievements_screen(earned)


async def _screen_hall_of_fame(db, user_id: int) -> str:
    """Личные рекорды и лайфтайм-цифры над сеткой достижений
    (formatting.build_hall_of_fame)."""
    return formatting.build_hall_of_fame(
        total_workouts=42,
        tonnage_kg=15000.0,
        tonnage_equivalent=formatting.format_tonnage_equivalent(15000.0),
        best_week_streak=6,
        longest_workout_seconds=5400,
        top_lifts=[("Squat", 140.0, 5, 160.0)],
    )


async def _screen_progress_screen(db, user_id: int) -> str:
    """Экран «📈 Прогресс» по одному упражнению (formatting.format_progress_screen)."""
    import analytics

    session = analytics.SessionStats(
        workout_id=1,
        started_at="2026-08-10T12:00:00",
        sets=[analytics.SetRow(weight=100.0, reps=5)],
    )
    records = analytics.compute_personal_records([session])
    return formatting.format_progress_screen("Squat", [session], None, records)


async def _screen_bodyweight(db, user_id: int) -> str:
    """Экран «⚖️ Вес тела» (formatting.build_bodyweight_screen), с историей —
    пустое состояние переведено отдельным ключом и тоже стоит проверить."""
    logs = [{"weight": 82.5, "logged_at": "2026-08-10T08:00:00"}]
    return formatting.build_bodyweight_screen(logs)


async def _screen_food_history(db, user_id: int) -> str:
    """Вкладка истории питания (formatting.build_food_history_list)."""
    day = formatting.FoodDayView(date=dt.date(2026, 8, 10), entries=2, calories=1800.0)
    return formatting.build_food_history_list([day])


async def _screen_workout_onboarding(db, user_id: int) -> str:
    """Приветствие новичка на главном меню (handlers.workout._onboarding) —
    самый важный текст продукта, формула «YO ATHLETE!»."""
    return workout._onboarding()


async def _screen_workout_help(db, user_id: int) -> str:
    """Оба экрана справки (/help): короткий и развёрнутый по кнопке «Ещё»."""
    return workout._help_short() + "\n" + workout._help_full()


async def _screen_workout_logging_hint(db, user_id: int) -> str:
    """Подсказка над клавиатурой записи подхода (handlers.workout._logging_hint)
    со всеми необязательными строками включёнными разом: план на сегодня,
    предупреждение о подозрительном весе, ряд «тот же вес, другие повторы» с
    подсказкой для новичка и «Прошлый раз» с прогрессией."""
    return workout._logging_hint(
        last_session=[(100.0, 8, None)],
        has_sets=True,
        unit="kg",
        show_progression=True,
        today_sets=[(500.0, 5)],
        show_instruction=True,
        show_format_hint=True,
        reps_row=(100.0, 8),
        reps_row_hint=i18n.t("workout.reps_row_hint_text"),
        target="3×8-12",
    )


async def _screen_edit_workout(db, user_id: int) -> str:
    """Экран правки прошлой тренировки: список упражнений и один подход внутри
    (handlers.edit_workout._edit_screen_payload/_exercise_screen_payload)."""
    group_id = await db.create_muscle_group(user_id, "Legs")
    squat = await db.create_exercise(user_id, "Squat", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, squat, 0)
    await db.add_set(block_id, squat, 1, 0, 100.0, 8)
    top_text, _ = await edit_workout._edit_screen_payload(workout_id)
    ex_text, _ = await edit_workout._exercise_screen_payload(workout_id, block_id, squat)
    return f"{top_text}\n{ex_text}"


async def _screen_exercises_groups(db, user_id: int) -> str:
    """«⚙️ Упражнения»: список групп мышц (handlers.exercises._groups_payload).
    Стандартные группы («Грудь», «Ноги» и т.д.) хранятся в БД канонической
    русской строкой навсегда (seed_data._seed_globals) — экран обязан
    показывать их через seed_data.localized_muscle_group_name
    (handlers.exercises._group_display_name), а не голый formatting.format_group,
    иначе английский атлет видел бы русские названия групп."""
    text, kb = await exercises._groups_payload(user_id)
    buttons = "\n".join(b.text for row in kb.inline_keyboard for b in row)
    return f"{text}\n{buttons}"


async def _screen_exercises_templates(db, user_id: int) -> str:
    """Список каталожных шаблонов одной группы мышц
    (handlers.exercises._localized_templates + keyboards.templates_keyboard).
    Шаблоны — общая на всех строка (`is_template=1`), её `name`/`display_name`
    остаются русскими навсегда (см. handlers.exercises._template_display_name);
    экран обязан локализовать их сам, при рендере."""
    groups = await db.list_muscle_groups(user_id)
    group_id = next(g["id"] for g in groups if g["user_id"] is None)
    templates = await db.list_templates_in_group(group_id)
    kb = keyboards.templates_keyboard(
        exercises._localized_templates(templates), prefix="exm", back_cb="newback"
    )
    return "\n".join(b.text for row in kb.inline_keyboard for b in row)


async def _screen_routines_manage_empty(db, user_id: int) -> str:
    """«🗂 Программы» с пустым состоянием (handlers.routines.show_manage) —
    экран-эталон гайда: три выхода (готовая программа/AI-тренер/из тренировки),
    отранжированные по усилию."""
    callback = _make_fake_callback(user_id, "rt:manage")
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await routines.show_manage(callback, state)
    return callback.message.answer.await_args.args[0]


async def _screen_routine_source_empty(db, user_id: int) -> str:
    """Экран «Из какой тренировки создать программу?» без единой завершённой
    тренировки (handlers.routines._show_routine_source_picker) — своё пустое
    состояние, отдельное от программ."""
    callback = _make_fake_callback(user_id, "rt:pickw:page:0")
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await routines._show_routine_source_picker(callback, state, 0)
    return callback.message.answer.await_args.args[0]


def _strip_autonyms(text: str) -> str:
    """Тот же вырез, что и в test_en_catalog_has_no_cyrillic: автоним «Русский»
    в подписи кнопки языка — законная кириллица даже на английском экране."""
    for autonym in _AUTONYM_WHITELIST:
        text = text.replace(autonym, "")
    return text


def _leaks(text: str) -> list[str]:
    return [word for word in _strip_autonyms(text).split() if i18n_coverage.has_cyrillic(word)]


# (имя экрана, сборщик) — списком, чтобы новый экран не требовал новой копии
# теста, только новую строку здесь.
#
# Разделены на два списка, а не xfail-декоратор с strict=True на одном общем:
# как только очередной экран из "ещё протекает" реально очищается (весь его
# модуль переведён), xfail(strict=True) на НЕМ превращается в XPASS — и это
# падение теста, а не успех, потому что strict=True требует, чтобы xfail
# сбылся. Экран в этот момент обязан физически переехать сюда, в SCREENS —
# только так тест снова зелёный и продолжает защищать именно то, что уже
# сделано.
SCREENS: list[tuple[str, object]] = [
    ("settings_keyboard", _screen_settings_keyboard),
    ("language_picker", _screen_language_picker),
    ("main_menu", _screen_main_menu),
    ("exercise_picker", _screen_exercise_picker),
    ("programs", _screen_programs),
    ("food_diary", _screen_food_diary),
    ("history_list", _screen_history_list),
    ("workout_summary_card", _screen_workout_summary),
    ("dashboard_menu", _screen_dashboard_menu),
    ("weekly_summary", _screen_weekly_summary),
    ("achievements", _screen_achievements),
    ("hall_of_fame", _screen_hall_of_fame),
    ("progress_screen", _screen_progress_screen),
    ("bodyweight_screen", _screen_bodyweight),
    ("food_history", _screen_food_history),
    ("history_item_card", _screen_history_item_card),
    ("workout_onboarding", _screen_workout_onboarding),
    ("workout_help", _screen_workout_help),
    ("workout_logging_hint", _screen_workout_logging_hint),
    ("edit_workout_screen", _screen_edit_workout),
    ("exercises_groups", _screen_exercises_groups),
    ("exercises_templates", _screen_exercises_templates),
    ("routines_manage_empty", _screen_routines_manage_empty),
    ("routine_source_empty", _screen_routine_source_empty),
]

# Экраны, которые всё ещё протекают кириллицей мимо каталога. Список
# существует, чтобы падение здесь было ожидаемым сигналом "этот конкретный
# экран ещё не готов", а не молчаливым исключением из проверки целиком.
#
# Сейчас список пуст: handlers/history.py (последний оставшийся) переехал в
# SCREENS выше — см. i18n_coverage.LOCALIZED. Пустой список, а не удалённый —
# следующий модуль, который потянет за собой протекающий экран, заводит
# запись здесь же, не изобретая инфраструктуру заново.
SCREENS_STILL_LEAKING: list[tuple[str, object]] = []


@pytest.mark.parametrize("screen_name, builder", SCREENS, ids=[name for name, _ in SCREENS])
async def test_screen_has_no_cyrillic_in_english(fresh_db, user_id, screen_name, builder):
    db = fresh_db
    await db.set_user_lang(user_id, "en")
    with i18n.use_lang("en"):
        text = await builder(db, user_id)
    leaks = _leaks(text)
    assert not leaks, f"{screen_name}: русские слова на английском экране: {leaks}"


@pytest.mark.parametrize(
    "screen_name, builder", SCREENS_STILL_LEAKING, ids=[name for name, _ in SCREENS_STILL_LEAKING]
)
async def test_screen_still_leaks_cyrillic_in_english(fresh_db, user_id, screen_name, builder):
    db = fresh_db
    await db.set_user_lang(user_id, "en")
    with i18n.use_lang("en"):
        text = await builder(db, user_id)
    leaks = _leaks(text)
    assert not leaks, f"{screen_name}: русские слова на английском экране: {leaks}"
