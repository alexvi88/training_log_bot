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
import seed_data
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
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
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


async def _screen_history_list_empty(db, user_id: int) -> str:
    """Пустая история (is_empty=True) — свой набор кнопок: «Начать тренировку»
    вместо «По месяцам», раз отмечать в календаре ещё нечего."""
    kb = keyboards.history_list_keyboard(workouts=[], page=0, has_next=False, is_empty=True)
    return "\n".join(button.text for row in kb.inline_keyboard for button in row)


async def _screen_history_calendar(db, user_id: int) -> str:
    """«📅 По месяцам»: заголовок экрана плюс сетка календаря с одним отмеченным
    днём (keyboards.calendar_keyboard, marked=...) — покрывает и intro-текст,
    и кнопку «⬅️ К списку», которой у обычного bf-календаря нет."""
    header = i18n.t("history.calendar_header")
    kb = keyboards.calendar_keyboard(
        "hist:cal", 2026, 8, today=dt.date(2026, 8, 17), marked={"2026-08-10"},
        show_quick_dates=False, back_text=i18n.t("btn.to_list"), back_cb="hist:back",
    )
    buttons = "\n".join(button.text for row in kb.inline_keyboard for button in row)
    return f"{header}\n{buttons}"


async def _screen_history_calendar_day_list(db, user_id: int) -> str:
    """День с 2+ тренировками — тот же build_history_list, что у обычного
    списка, но с заголовком history.calendar_day_header."""
    return formatting.build_history_list(
        [(dt.datetime(2026, 8, 10, 9, 0), ["Squat"], 3), (dt.datetime(2026, 8, 10, 18, 0), [], 0)],
        header=i18n.t("history.calendar_day_header", date=formatting.format_date_ru(dt.datetime(2026, 8, 10))),
        footer="",
    )


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
    самый важный текст продукта, формула «HEY ATHLETE!»."""
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


async def _screen_catalog_program_card(db, user_id: int) -> str:
    """Карточка каталожной программы, уже добавленной себе
    (handlers.routines._show_program).

    Экран собирает текст из ДВУХ источников: имена дней и описание — снимок из
    базы на языке добавления, а строка «кому это и как часто» (meta) в базе не
    лежит вовсе и берётся из seed_data по ключу. Ровно она и приезжала
    по-русски на английском аккаунте — единственная кириллица на экране, где
    всё остальное перевелось.
    """
    program_id = await seed_data.instantiate_program(
        user_id, "ppl", seed_data.localized_program_name("ppl", i18n.get_lang())
    )
    callback = _make_fake_callback(user_id, f"rt:prg:{program_id}")
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await routines._show_program(callback, state, program_id)
    return callback.message.answer.await_args.args[0]


async def _screen_routine_source_empty(db, user_id: int) -> str:
    """Экран «Из какой тренировки создать программу?» без единой завершённой
    тренировки (handlers.routines._show_routine_source_picker) — своё пустое
    состояние, отдельное от программ."""
    callback = _make_fake_callback(user_id, "rt:pickw:page:0")
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await routines._show_routine_source_picker(callback, state, 0)
    return callback.message.answer.await_args.args[0]


async def _screen_settings_profile(db, user_id: int) -> str:
    """«🧬 Обо мне» (handlers.settings.settings_profile) — пустой профиль, то
    есть ветка "tail_unknown": ни одно поле ещё не записано с твоих слов."""
    from handlers import settings

    callback = _make_fake_callback(user_id, "settings:profile")
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await settings.settings_profile(callback, state)
    return callback.message.answer.await_args.args[0]


async def _screen_mcp_access_disabled(db, user_id: int) -> str:
    """Экран /mcp без публичного адреса (handlers.mcp_access._show,
    config.mcp_available() == False в тестовом окружении) — тот самый экран,
    что раньше был английским независимо от языка бота (см. TONE_OF_VOICE.md,
    «Известные долги»)."""
    from handlers import mcp_access

    callback = _make_fake_callback(user_id, "mcp:open")
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await mcp_access.mcp_open(callback, state)
    return callback.message.answer.await_args.args[0]


async def _screen_community_intro(db, user_id: int) -> str:
    """/community с настроенным адресом группы (handlers.community.cmd_community)."""
    import config
    from handlers import community

    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=2))
    community_url = config.COMMUNITY_CHAT_URL
    config.COMMUNITY_CHAT_URL = "https://t.me/example_community"
    try:
        await community.cmd_community(message)
    finally:
        config.COMMUNITY_CHAT_URL = community_url
    return message.answer.await_args.args[0]


async def _screen_donate(db, user_id: int) -> str:
    """Экран доната (handlers.donate.open_donate) при включённых донатах."""
    import config
    from handlers import donate

    donations_enabled = config.DONATIONS_ENABLED
    config.DONATIONS_ENABLED = True
    try:
        callback = _make_fake_callback(user_id, "menu:donate")
        await donate.open_donate(callback)
        return callback.message.answer.await_args.args[0]
    finally:
        config.DONATIONS_ENABLED = donations_enabled


async def _screen_backfill_prompt(db, user_id: int) -> str:
    """«📅 На какую дату занести тренировку?» (handlers.backfill.backfill_start)."""
    from handlers import backfill

    callback = _make_fake_callback(user_id, "menu:backfill_workout")
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await backfill.backfill_start(callback, state)
    return callback.message.answer.await_args.args[0]


async def _screen_fallback_generic(db, user_id: int) -> str:
    """Единый ответ на непонятый текст (handlers.fallback.unhandled_text) —
    короткий текст, где кнопка «Спросить тренера» ещё не появляется."""
    from aiogram.enums import ContentType

    from handlers import fallback

    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    message.content_type = ContentType.TEXT
    message.text = "бред"
    message.reply = AsyncMock(return_value=SimpleNamespace(message_id=2))
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await fallback.unhandled_text(message, state)
    return message.reply.await_args.args[0]


async def _screen_fallback_ask_trainer(db, user_id: int) -> str:
    """Длинный непонятый текст — кнопка «🤖 Спросить тренера» вместо общего
    ответа (handlers.fallback.unhandled_text, AI-тренер настроен)."""
    from aiogram.enums import ContentType

    from handlers import fallback

    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    message.content_type = ContentType.TEXT
    message.text = "непонятная бессвязная фраза без числа и без названия"
    message.reply = AsyncMock(return_value=SimpleNamespace(message_id=2))
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fallback.ai_trainer, "is_configured", lambda: True)
        await fallback.unhandled_text(message, state)
    return message.reply.await_args.args[0]


async def _screen_main_error_fallback(db, user_id: int) -> str:
    """Последний рубеж (main._generic_error_text) — текст, который увидит
    человек, если у него сломалось решительно всё."""
    import main

    return main._generic_error_text()


async def _screen_ai_trainer_intro(db, user_id: int) -> str:
    """Интро/резюме AI-тренера и готовые вопросы (handlers/ai_trainer.py),
    прочитанные и как модульные атрибуты (module __getattr__), и через
    intro_presets — тот же путь, что и menu:ai."""
    from handlers import ai_trainer as at

    presets = await at.intro_presets(user_id)
    preset_labels = "\n".join(label for label, _ in presets)
    return f"{at.INTRO_TEXT}\n{at.RESUME_TEXT}\n{at.ACTIONS_HINT_TEXT}\n{preset_labels}"


async def _screen_ai_trainer_new_chat(db, user_id: int) -> str:
    """«🆕 Новый разговор»: кнопка на экране возврата в разговор и экран,
    который встаёт после тапа (handlers.ai_trainer.ai_new_chat)."""
    from handlers import ai_trainer as at

    kb = await at.ai_keyboard(user_id, presets=await at.intro_presets(user_id), new_chat=True)
    buttons = "\n".join(b.text for row in kb.inline_keyboard for b in row)
    return f"{at.NEW_CHAT_TEXT}\n{buttons}"


async def _screen_exercise_alternatives(db, user_id: int) -> str:
    """«🔁 Замена» (трекер и карточка упражнения): заголовки, подписи и список
    замен — имена из каталога шаблонов, а не русская идентичность."""
    import exercise_alternatives

    template = (await db.find_global_templates_by_names(["Жим штанги лёжа"]))["Жим штанги лёжа"]
    ex = await db.get_exercise(await db.fork_exercise_from_template(user_id, template["id"]))
    alternatives = await exercise_alternatives.for_exercise(user_id, ex)
    kb = keyboards.exercise_alternatives_keyboard(
        alternatives, own_cb="o:{}", template_cb="t:{}", back_cb="b"
    )
    live = keyboards.logging_keyboard([(ex["id"], ex["display_name"])], ex["id"], can_swap=True)
    name = ex["display_name"]
    lines = [
        i18n.t("workout.swap.title", name=name), i18n.t("workout.swap.sets_stay"),
        i18n.t("workout.swap.empty", name=name),
        i18n.t("exercises.alternatives.title", name=name),
        i18n.t("exercises.alternatives.empty", name=name),
    ]
    lines += [b.text for row in kb.inline_keyboard for b in row]
    lines += [b.text for row in live.inline_keyboard for b in row]
    return "\n".join(lines)


async def _screen_workout_new_exercise_group(db, user_id: int) -> str:
    """«➕ Создать «…»» без открытой группы: вопрос, в какую группу положить
    новое упражнение (handlers.workout._create_or_ask_group)."""
    groups = await db.list_muscle_groups(user_id, order_by_usage=True)
    kb = keyboards.groups_keyboard(
        groups, prefix="picknewgrp", extra_buttons=[(i18n.t("btn.cancel"), "picknewgrp:cancel")]
    )
    lines = [i18n.t("workout.new_exercise_pick_group", name="Side plank"), i18n.t("workout.group_gone")]
    lines += [b.text for row in kb.inline_keyboard for b in row]
    return "\n".join(lines)


async def _screen_ai_trainer_setup_question(db, user_id: int) -> str:
    """Вопрос опросника перед сборкой программы: заголовок «ВОПРОС N ИЗ M»,
    сам вопрос про цель (первый и обязательный, см. handlers.ai_trainer.
    _setup_goal_question), подсказка и кнопки вариантов."""
    from handlers import ai_trainer as at

    goal = at.SETUP_GOAL_QUESTION
    html = at._setup_question_html(0, 4, goal["question"], has_choices=True)
    kb = keyboards.ai_setup_question_keyboard(0, goal["choices"])
    buttons = "\n".join(b.text for row in kb.inline_keyboard for b in row)
    return f"{html}\n{buttons}"


async def _screen_ai_trainer_thinking_pools(db, user_id: int) -> str:
    """Все английские пулы placeholder-фраз «тренер думает» разом
    (running_texts.POOLS_EN) — по одному ключу на вариант в каталоге, но
    один живой рендер сразу ловит утечку в любом из них."""
    import running_texts

    lines = [line for pool in running_texts.POOLS_EN.values() for line in pool]
    return "\n".join(lines)


async def _screen_factcheck(db, user_id: int) -> str:
    """Разбор пересланного поста: занятость, отказ и пул placeholder-фраз
    (handlers/factcheck.py, running_texts.fact_check_pool)."""
    import running_texts

    busy = i18n.t("factcheck.busy")
    failed = i18n.t("factcheck.failed")
    pool = "\n".join(running_texts.fact_check_pool())
    return f"{busy}\n{failed}\n{pool}"


async def _screen_invite(db, user_id: int) -> str:
    """«🤝 Пригласить» (handlers/history.py:invite_show) — ссылка в <pre>
    плюс короткая реплика тренера."""
    import acquisition

    link = acquisition.referral_link("testbot", user_id)
    return i18n.t("invite.screen", link=link)


async def _screen_ios_link(db, user_id: int) -> str:
    """/ios (handlers/ios_link.py) — код связки для iOS-приложения."""
    from handlers import ios_link

    return ios_link._ios_link_text("12345678")


def _strip_autonyms(text: str) -> str:
    """Тот же вырез, что и в test_en_catalog_has_no_cyrillic: автоним «Русский»
    в подписи кнопки языка — законная кириллица даже на английском экране."""
    for autonym in _AUTONYM_WHITELIST:
        text = text.replace(autonym, "")
    return text


def _kb_texts(kb) -> str:
    return " ".join(b.text for row in kb.inline_keyboard for b in row)


async def _screen_import_confirmation(db, user_id: int) -> str:
    """Подтверждение импорта: сессии с временем и названием, пропуски по
    причинам, предупреждение о датах и «всё уже есть» — плюс кнопки."""
    import handlers.csv_import as csv_import

    stats: dict = {"date_order_ambiguous": True, "date_order_example": {"text": "03/09/2026", "date": "2026-09-03"}}
    for reason in csv_import.SKIP_REASONS:
        csv_import.add_skip(stats, reason, "Bench Press (Barbell) 50×10")
    entries = [(dt.date(2026, 8, 7), ["Bench Press (Barbell)"], True, "08:27 · Push")]
    text = formatting.build_import_confirmation_list(entries, set(), i18n.t("import.confirm_header", n=1))
    text += "\n" + "\n".join(csv_import._report_lines(stats)) + "\n" + i18n.t("import.all_already_there")
    text += " " + _kb_texts(keyboards.csv_import_all_duplicates_keyboard(0, 1))
    text += " " + i18n.t("import.dupes_submenu", n=3) + " " + _kb_texts(keyboards.csv_import_dupes_submenu_keyboard(3))
    return text


async def _screen_import_result(db, user_id: int) -> str:
    """Итог импорта с отменой, отмена, список прошлых импортов, согласие на
    разбор заметок и короткий разбор от тренера (ai_trainer)."""
    import ai_trainer
    import handlers.csv_import as csv_import

    result = {"imported": 6, "sets": 11, "achievements": ["first", "club100"], "batch_id": "b" * 32}
    text = csv_import._result_text(result, 2, {})
    text += " " + _kb_texts(keyboards.import_result_keyboard("b" * 32))
    text += " " + csv_import.undo_message({"removed_workouts": 6, "removed_sets": 11, "removed_exercises": 2})
    text += " " + i18n.t("import.undo_confirm", n=6, sets=11, date="03.10.2026")
    text += " " + _kb_texts(keyboards.import_undo_confirm_keyboard("b" * 32))
    text += " " + i18n.t("import.batch_label", date="03.10.2026", source=i18n.t("import.source.notes"), n=3)
    text += " " + _kb_texts(keyboards.import_intro_keyboard(True)) + " " + i18n.t("import.batches_header")
    text += " " + i18n.t("import.notes_consent") + " " + _kb_texts(keyboards.import_notes_consent_keyboard())
    text += " " + await ai_trainer._small_import_overview(2, ["Bench Press (Barbell)", "Squat"])
    return text


async def _screen_import_resolve(db, user_id: int) -> str:
    """Вопрос «куда это имя»: шаблоны каталога — на языке атлета, хотя в базе
    они хранятся русским именем-идентичностью."""
    templates = await db.search_exercise_templates(user_id, "Bench Press")
    assert templates
    kb = keyboards.exercise_resolve_keyboard([], "Bench Press", "resolve", remaining=3, templates=templates)
    return i18n.t("resolve.progress_similar", position=1, total=4, name="Bench Press") + " " + _kb_texts(kb)


async def _screen_exercise_copy(db, user_id: int) -> str:
    """«⧉ Сделать копию»: кнопка в меню правки, вопрос об имени с «Отменой»,
    три отказа и подтверждение — всё, что человек видит по дороге."""
    from handlers import exercises as exercises_handlers

    ex = {"id": 1, "name": "Bench", "display_name": "Bench", "description": None,
          "custom_photo_file_id": None}
    text = _kb_texts(exercises_handlers._exercise_edit_menu_keyboard(ex))
    text += " " + i18n.t("exercises.copy.prompt", source="Bench")
    text += " " + _kb_texts(exercises_handlers._copy_prompt_keyboard(1))
    # Копия шаблона: «⧉ Сделать копию» в превью шаблона и «Отмена» назад в него.
    text += " " + _kb_texts(keyboards.template_preview_keyboard(1, with_copy=True))
    text += " " + _kb_texts(keyboards.template_preview_keyboard(1, as_question=True, with_copy=True))
    text += " " + _kb_texts(exercises_handlers._template_copy_prompt_keyboard(1))
    for key in ("exercises.copy.same_name", "exercises.copy.name_taken", "exercises.copy.name_archived"):
        text += " " + i18n.t(key, source="Bench")
    text += " " + i18n.t("exercises.copy.too_long", max=60)
    text += " " + i18n.t("exercises.copy.done", name="Bench close grip")
    return text


async def _screen_merge_done(db, user_id: int) -> str:
    text = i18n.t("exercises.merge.done_undo", source="Bench", target="Bench Press")
    text += " " + _kb_texts(keyboards.merge_done_keyboard(1, "m" * 32))
    text += " " + i18n.t("exercise.unmerged", name="Bench", n=3)
    return text


async def _coach_pair(db, user_id: int) -> tuple[int, int]:
    """Тренер (user_id) и связанный с ним атлет с английскими именами и одной
    законченной тренировкой: кириллица на экранах сервиса тренеров может
    взяться только из кода, не из данных."""
    athlete = user_id + 1
    await db.get_or_create_user(telegram_id=athlete, username="athlete")
    await db.set_user_lang(athlete, "en")
    await db.upsert_coach_profile(user_id, "Coach Alex")
    await db.replace_coach_invite(user_id, "K7MP2QXA")
    await db.create_coach_link(user_id, athlete, "Sam", show_bodyweight=True)
    gid = await db.create_muscle_group(athlete, "Chest")
    ex = await db.create_exercise(athlete, "Bench Press", gid)
    wid = await db.create_workout(athlete)
    block = await db.create_block(wid, "single")
    await db.add_block_exercise(block, ex, 0)
    await db.add_set(block, ex, 1, 0, 80.0, 5)
    await db.finish_workout(wid)
    await db.add_coach_comment(user_id, athlete, wid, "Nice pace")
    return athlete, wid


async def _coach_screen(user_id: int, data: str) -> str:
    """Экран сервиса тренеров через его роутер: текст плюс подписи кнопок."""
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from handlers import coach

    callback = _make_fake_callback(user_id, data)
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None, first_name="Sam")
    callback.bot = MagicMock()
    callback.bot.get_me = AsyncMock(return_value=SimpleNamespace(username="testbot"))
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await coach.router.callback_query.trigger(callback, state=state)
    texts = [c.args[0] for c in callback.message.answer.await_args_list]
    alerts = [c.args[0] for c in callback.answer.await_args_list if c.args]
    buttons = [
        _kb_texts(c.kwargs["reply_markup"]) for c in callback.message.answer.await_args_list
        if c.kwargs.get("reply_markup") is not None
    ]
    return "\n".join(texts + alerts + buttons)


async def _screen_coach_hub_and_invite(db, user_id: int) -> str:
    """🧑‍🏫 Тренер: «Мой тренер» и «Я тренер», приглашение с кодом и ссылкой,
    подтверждение нового кода."""
    athlete, _wid = await _coach_pair(db, user_id)
    out = [await _coach_screen(user_id, "coach:hub"), await _coach_screen(athlete, "coach:hub")]
    out.append(await _coach_screen(user_id, "coach:invite"))
    out.append(await _coach_screen(user_id, "coach:renew"))
    out.append(await _coach_screen(user_id, "coach:become"))
    return "\n".join(out)


async def _screen_coach_athlete_side(db, user_id: int) -> str:
    """Атлет: ввод кода, превью приглашения с переключателями, имя, «Мой
    тренер», закрыть доступ, сообщения о комментарии, программе от тренера и
    новом клиенте (тренеру), блок под тренировкой."""
    import coach_notify
    from handlers import coach

    athlete, wid = await _coach_pair(db, user_id)
    newcomer = user_id + 2
    await db.get_or_create_user(telegram_id=newcomer, username="newcomer")
    await db.set_user_lang(newcomer, "en")
    out = [
        await _coach_screen(newcomer, "coach:code"),
        await _coach_screen(newcomer, "coach:inv:K7MP2QXA:1"),
        await _coach_screen(newcomer, "coach:ok:K7MP2QXA:0"),
        await _coach_screen(newcomer, "coach:no"),
        await _coach_screen(newcomer, "coach:inv:NOPE2345:0"),
        await _coach_screen(user_id, "coach:inv:K7MP2QXA:0"),
        await _coach_screen(athlete, "coach:inv:K7MP2QXA:0"),
        await _coach_screen(athlete, f"coach:my:{user_id}"),
        await _coach_screen(athlete, f"coach:rv:{user_id}"),
    ]
    body, kb = await coach_notify.telegram_comment_message(await db.get_user(athlete), "Coach Alex", "Nice", wid)
    out += [body, _kb_texts(kb), await coach.athlete_comments_block(wid, await db.get_user(athlete))]
    # Программа от тренера — атлету; «новый клиент» — тренеру.
    body, kb = await coach_notify.telegram_program_message(await db.get_user(athlete), "Coach Alex", "Push Pull", 1)
    out += [body, _kb_texts(kb)]
    body, kb = await coach_notify.telegram_new_client_message(await db.get_user(user_id), "Sam")
    out += [body, _kb_texts(kb)]
    out += [
        coach._validated_name("")[1], coach._validated_name("x" * 99)[1],
        i18n.t("coach.code.empty"), i18n.t("coach.linked_done", name="Coach Alex"),
        i18n.t("coach.linked_done_hint"), i18n.t("coach.access_closed", name="Coach Alex"),
        i18n.t("coach.link_gone"), i18n.t("coach.name_saved", name="Coach Alex"),
        i18n.t("coach.invite_renewed"), i18n.t("program.from_coach", name="Coach Alex"),
    ]
    out.append(await _coach_screen(athlete, f"coach:rvy:{user_id}"))
    return "\n".join(out)


async def _screen_coach_clients(db, user_id: int) -> str:
    """Тренер: «Мои клиенты» с сигналами, карточка клиента, все тренировки,
    тренировка с комментариями, ввод комментария, вес, назначение,
    «убрать», отказ чужому."""
    from handlers import coach

    athlete, wid = await _coach_pair(db, user_id)
    await db.add_bodyweight_log(athlete, 70.5)
    out = [
        await _coach_screen(user_id, "coach:clients:0"),
        await _coach_screen(user_id, f"coach:cl:{athlete}"),
        await _coach_screen(user_id, f"coach:ws:{athlete}:0"),
        await _coach_screen(user_id, f"coach:w:{athlete}:{wid}"),
        await _coach_screen(user_id, f"coach:cm:{athlete}:{wid}"),
        await _coach_screen(user_id, f"coach:bw:{athlete}"),
        await _coach_screen(user_id, f"coach:asg:{athlete}"),
        await _coach_screen(user_id, f"coach:asc:{athlete}"),
        await _coach_screen(user_id, f"coach:ascp:{athlete}:ppl"),
        await _coach_screen(user_id, f"coach:asai:{athlete}"),
        await _coach_screen(user_id, f"coach:rm:{athlete}"),
        await _coach_screen(athlete, f"coach:cl:{user_id}"),
        await _coach_screen(athlete, "coach:clients:0"),
    ]
    signals = {"missing": True, "days_since_last_workout": 9, "behind_program": True,
               "new_records": 2, "workouts_this_week": 3}
    out += coach.signal_texts(signals)
    out += coach.signal_texts({**signals, "days_since_last_workout": None})
    for key in (
        "coach.visibility.bodyweight", "coach.visibility.none",
        "coach.comment.draft_gone", "coach.assign.retry", "coach.summary_none", "coach.summary_none_one",
        "coach.ai_summary.thinking", "coach.ai_summary.header_all", "coach.clients.empty",
        "coach.bodyweight.empty", "coach.assign.empty", "coach.signal.none",
    ):
        out.append(i18n.t(key))
    out += [
        i18n.t("coach.comment.preview", name="Sam", text="Nice"), i18n.t("coach.comment.sent", name="Sam"),
        i18n.t("coach.comment.too_long", n=1200, max=1000), i18n.t("coach.summary_skipped", n=2),
        i18n.t("coach.ai_summary.header_one", name="Sam"), i18n.t("coach.removed", name="Sam"),
        i18n.t("coach.program_assigned", name="Split"),
        i18n.t("coach.assign.catalog_have", name="Split"),
        i18n.t("coach.program_ai.done", name="Split"), i18n.t("coach.program_ai.thinking"),
        i18n.t("api.error.client_no_ai_consent"), i18n.t("api.error.ai_program_failed"),
    ]
    out.append(await _coach_screen(user_id, f"coach:rmy:{athlete}"))
    return "\n".join(out)


async def _screen_coach_client_progress(db, user_id: int) -> str:
    """Тренер: «📈 Прогресс» клиента — частота, недели, рекорды, график
    основного движения (подпись под картинкой и кнопки), пустое состояние и
    отказ упражнению без графика."""
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from handlers import coach

    athlete, wid = await _coach_pair(db, user_id)
    earlier = await db.create_workout(athlete, started_at="2020-01-06T10:00:00")
    cur = await db.conn().execute(
        "SELECT exercise_id FROM sets s JOIN workout_blocks b ON b.id = s.block_id WHERE b.workout_id = ?", (wid,)
    )
    ex = (await cur.fetchone())["exercise_id"]
    block = await db.create_block(earlier, "single")
    await db.add_block_exercise(block, ex, 0)
    await db.add_set(block, ex, 1, 0, 70.0, 5)
    await db.finish_workout(earlier, finished_at="2020-01-06T11:00:00")
    week_ago = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=8)).isoformat(
        timespec="seconds"
    )
    recent = await db.create_workout(athlete, started_at=week_ago)
    block = await db.create_block(recent, "single")
    await db.add_block_exercise(block, ex, 0)
    await db.add_set(block, ex, 1, 0, 75.0, 5)
    await db.finish_workout(recent, finished_at=week_ago)

    out = [await _coach_screen(user_id, f"coach:pg:{athlete}")]
    callback = _make_fake_callback(user_id, f"coach:pgl:{athlete}:{ex}")
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None, first_name="Sam")
    callback.message.answer_photo = AsyncMock(return_value=SimpleNamespace(message_id=5))
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await coach.router.callback_query.trigger(callback, state=state)
    sent = callback.message.answer_photo.await_args
    out += [sent.kwargs["caption"], _kb_texts(sent.kwargs["reply_markup"])]
    out.append(await _coach_screen(user_id, f"coach:pgl:{athlete}:999999"))
    for key in ("coach.progress.empty", "coach.progress.charts_few", "coach.progress.lift_gone"):
        out.append(i18n.t(key))
    out.append(i18n.t("coach.progress.reps", n=12))
    return "\n".join(out)


async def _screen_coach_client_exercises(db, user_id: int) -> str:
    """Тренер: «📈 Прогресс» → «Все упражнения» клиента (список страницами) →
    прогресс одного упражнения тем же экраном, что у атлета (подпись под
    графиком и кнопки периодов), одна тренировка без графика, отказ чужому
    упражнению и пустой список."""
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from handlers import coach

    athlete, wid = await _coach_pair(db, user_id)
    cur = await db.conn().execute(
        "SELECT exercise_id FROM sets s JOIN workout_blocks b ON b.id = s.block_id WHERE b.workout_id = ?", (wid,)
    )
    ex = (await cur.fetchone())["exercise_id"]
    out = [await _coach_screen(user_id, f"coach:px:{athlete}:0")]
    out.append(await _coach_screen(user_id, f"coach:pe:{athlete}:{ex}:20:0"))
    week_ago = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=8)).isoformat(
        timespec="seconds"
    )
    earlier = await db.create_workout(athlete, started_at=week_ago)
    block = await db.create_block(earlier, "single")
    await db.add_block_exercise(block, ex, 0)
    await db.add_set(block, ex, 1, 0, 70.0, 5)
    await db.finish_workout(earlier, finished_at=week_ago)
    callback = _make_fake_callback(user_id, f"coach:pe:{athlete}:{ex}:9999:0")
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None, first_name="Sam")
    callback.message.answer_photo = AsyncMock(return_value=SimpleNamespace(message_id=5))
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await coach.router.callback_query.trigger(callback, state=state)
    sent = callback.message.answer_photo.await_args
    out += [sent.kwargs["caption"], _kb_texts(sent.kwargs["reply_markup"])]
    out.append(await _coach_screen(user_id, f"coach:pe:{athlete}:999999:20:0"))
    for key in ("coach.exercises.empty", "coach.exercises.gone", "coach.btn.all_exercises"):
        out.append(i18n.t(key))
    return "\n".join(out)


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
    ("history_list_empty", _screen_history_list_empty),
    ("history_calendar", _screen_history_calendar),
    ("history_calendar_day_list", _screen_history_calendar_day_list),
    ("workout_summary_card", _screen_workout_summary),
    ("dashboard_menu", _screen_dashboard_menu),
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
    ("catalog_program_card", _screen_catalog_program_card),
    ("settings_profile", _screen_settings_profile),
    ("mcp_access_disabled", _screen_mcp_access_disabled),
    ("community_intro", _screen_community_intro),
    ("donate_screen", _screen_donate),
    ("backfill_prompt", _screen_backfill_prompt),
    ("fallback_generic", _screen_fallback_generic),
    ("fallback_ask_trainer", _screen_fallback_ask_trainer),
    ("main_error_fallback", _screen_main_error_fallback),
    ("ai_trainer_intro", _screen_ai_trainer_intro),
    ("ai_trainer_new_chat", _screen_ai_trainer_new_chat),
    ("exercise_alternatives", _screen_exercise_alternatives),
    ("workout_new_exercise_group", _screen_workout_new_exercise_group),
    ("ai_trainer_setup_question", _screen_ai_trainer_setup_question),
    ("ai_trainer_thinking_pools", _screen_ai_trainer_thinking_pools),
    ("factcheck_screen", _screen_factcheck),
    ("invite_screen", _screen_invite),
    ("ios_link_screen", _screen_ios_link),
    ("import_confirmation", _screen_import_confirmation),
    ("import_result_and_undo", _screen_import_result),
    ("import_resolve_templates", _screen_import_resolve),
    ("exercise_merge_done", _screen_merge_done),
    ("exercise_copy", _screen_exercise_copy),
    ("coach_hub_and_invite", _screen_coach_hub_and_invite),
    ("coach_athlete_side", _screen_coach_athlete_side),
    ("coach_clients_and_card", _screen_coach_clients),
    ("coach_client_progress", _screen_coach_client_progress),
    ("coach_client_exercises", _screen_coach_client_exercises),
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
