"""«🔁 Замена» в боте: живой трекер и карточка упражнения.

Серверная половина — exercise_alternatives.for_exercise (её же отдаёт
приложению GET /v1/exercises/{id}/alternatives). Здесь проверяем экранную:
кнопка стоит только у упражнения из каталога, список строится из семейства,
тап по ещё не заведённому шаблону форкает его и открывает тем же путём, что
обычный выбор упражнения в трекере, а текущее упражнение закрывается как по
«✅ Закончить упражнение».
"""

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import i18n
from fsm import ExerciseManage, WorkoutFlow
from handlers import exercises, workout

pytestmark = pytest.mark.asyncio

BENCH = "Жим штанги лёжа"
DB_BENCH = "Жим гантелей лёжа"


def _callbacks(kb) -> list[str]:
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _labels(kb) -> list[str]:
    return [b.text for row in kb.inline_keyboard for b in row]


@pytest.fixture
def screens(monkeypatch):
    """Каждый экран трекера, который хендлер поставил: (hint, keyboard)."""
    shown: list[tuple[str, object]] = []

    async def fake_refresh_live(bot, state, user, workout_id, hint, keyboard, note=None):
        shown.append((hint, keyboard))

    monkeypatch.setattr(workout, "_refresh_live", fake_refresh_live)
    monkeypatch.setattr(workout, "_sync_sticky_photo", AsyncMock())
    return shown


def _callback(user_id: int, data: str):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 1
    message.text = "экран"
    message.delete = AsyncMock()
    message.edit_text = AsyncMock(return_value=message)
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=2))
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=999))
    message.bot = bot
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, language_code=None)
    callback.bot = bot
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _template(db, name: str):
    return (await db.find_global_templates_by_names([name]))[name]


async def _tracker_with(db, user_id: int, ex_id: int) -> FSMContext:
    workout_id = await db.create_workout(user_id)
    state = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )
    await state.set_state(WorkoutFlow.picking_exercise)
    await state.update_data(
        workout_id=workout_id, live_chat_id=user_id, live_message_id=1,
        open_exercises=[], open_blocks={}, active_exercise_id=None,
    )
    await workout._on_exercise_chosen(_callback(user_id, "pick:ex"), state, ex_id)
    return state


# ---------- живой трекер ----------


async def test_tracker_shows_swap_for_catalog_exercise(fresh_db, user_id, screens):
    db = fresh_db
    bench = await db.fork_exercise_from_template(user_id, (await _template(db, BENCH))["id"])
    await _tracker_with(db, user_id, bench)
    _hint, kb = screens[-1]
    assert "live:swap" in _callbacks(kb)


def test_swap_sits_on_its_own_row_and_leaves_after_a_set():
    """Третьей в ряду с «Суперсет»/«Заметка» кнопка обрезала все три
    («…рсет / З…етка / …ивы»), а после подхода снаряд уже нашёлся."""
    import keyboards
    kb = keyboards.logging_keyboard([(1, "Жим")], 1, has_sets=False, can_swap=True)
    rows = [[b.callback_data for b in row] for row in kb.inline_keyboard]
    assert ["live:swap"] in rows
    assert all(len(row) == 1 for row in rows if "live:swap" in row)
    assert "live:swap" not in _callbacks(keyboards.logging_keyboard([(1, "Жим")], 1, can_swap=False))


async def test_tracker_hides_swap_after_first_set(fresh_db, user_id, screens):
    db = fresh_db
    bench = await db.fork_exercise_from_template(user_id, (await _template(db, BENCH))["id"])
    state = await _tracker_with(db, user_id, bench)
    data = await state.get_data()
    block_id = (data.get("open_blocks") or {}).get(str(bench)) or (data.get("open_blocks") or {}).get(bench)
    await db.add_set(block_id, bench, 1, 1, 60, 8)
    user = await db.get_user(user_id)
    await workout._render_logging_screen(_callback(user_id, "x").bot, state, user)
    _hint, kb = screens[-1]
    assert "live:swap" not in _callbacks(kb)


async def test_tracker_has_no_swap_for_own_exercise(fresh_db, user_id, screens):
    db = fresh_db
    own = await db.create_exercise(user_id, "Мой хитрый жим на резинке", None)
    await _tracker_with(db, user_id, own)
    _hint, kb = screens[-1]
    assert "live:swap" not in _callbacks(kb)


async def test_swap_lists_alternatives_and_back(fresh_db, user_id, screens):
    db = fresh_db
    bench = await db.fork_exercise_from_template(user_id, (await _template(db, BENCH))["id"])
    state = await _tracker_with(db, user_id, bench)

    await workout.live_swap(_callback(user_id, "live:swap"), state)
    hint, kb = screens[-1]
    callbacks = _callbacks(kb)
    db_bench = await _template(db, DB_BENCH)
    assert f"live:swaptpl:{db_bench['id']}" in callbacks
    assert callbacks[-1] == "live:swapback"
    assert 1 <= len(callbacks) - 1 <= 8
    assert BENCH in hint


async def test_swap_to_template_forks_and_replaces_empty_exercise(fresh_db, user_id, screens):
    db = fresh_db
    bench = await db.fork_exercise_from_template(user_id, (await _template(db, BENCH))["id"])
    state = await _tracker_with(db, user_id, bench)
    old_block = (await state.get_data())["open_blocks"][bench]
    db_bench = await _template(db, DB_BENCH)

    await workout.live_swap_to_template(_callback(user_id, f"live:swaptpl:{db_bench['id']}"), state)

    data = await state.get_data()
    new_id = data["active_exercise_id"]
    new_ex = await db.get_exercise(new_id)
    assert new_ex["user_id"] == user_id and not new_ex["is_template"]
    assert new_ex["original_name"] == DB_BENCH
    assert data["open_exercises"] == [new_id]
    assert bench not in data["open_blocks"]
    # Пустой блок заменённого упражнения не остаётся висеть в тренировке.
    assert await db.get_block(old_block) is None
    assert await state.get_state() == WorkoutFlow.logging_set.state


async def test_swap_keeps_logged_sets_of_the_replaced_exercise(fresh_db, user_id, screens):
    db = fresh_db
    bench = await db.fork_exercise_from_template(user_id, (await _template(db, BENCH))["id"])
    state = await _tracker_with(db, user_id, bench)
    old_block = (await state.get_data())["open_blocks"][bench]
    await db.add_set(old_block, bench, 1, 1, 80, 8)

    await workout.live_swap(_callback(user_id, "live:swap"), state)
    hint, _kb = screens[-1]
    assert i18n.t("workout.swap.sets_stay") in hint

    # Уже заведённая копия — открывается она сама, второй форк не нужен.
    own_db = await db.fork_exercise_from_template(user_id, (await _template(db, DB_BENCH))["id"])
    await workout.live_swap_to_own(_callback(user_id, f"live:swapex:{own_db}"), state)

    data = await state.get_data()
    assert data["active_exercise_id"] == own_db
    assert await db.get_block(old_block) is not None
    assert len(await db.list_sets_for_block(old_block)) == 1


async def test_swap_list_is_localized_in_english(fresh_db, user_id, screens):
    db = fresh_db
    await db.set_user_lang(user_id, "en")
    with i18n.use_lang("en"):
        bench = await db.fork_exercise_from_template(user_id, (await _template(db, BENCH))["id"])
        state = await _tracker_with(db, user_id, bench)
        await workout.live_swap(_callback(user_id, "live:swap"), state)
    hint, kb = screens[-1]
    text = hint + "\n" + "\n".join(_labels(kb))
    assert not re.search("[А-Яа-яЁё]", text), text


# ---------- карточка упражнения ----------


async def test_card_has_swap_button_only_for_catalog_exercise(fresh_db, user_id):
    db = fresh_db
    bench = await db.get_exercise(
        await db.fork_exercise_from_template(user_id, (await _template(db, BENCH))["id"])
    )
    _text, kb = exercises._exercise_detail_view(bench)
    assert f"exm:alt:{bench['id']}" in _callbacks(kb)

    own = await db.get_exercise(await db.create_exercise(user_id, "Мой хитрый жим на резинке", None))
    _text, kb = exercises._exercise_detail_view(own)
    assert not any(cb.startswith("exm:alt:") for cb in _callbacks(kb))


async def test_card_alternatives_open_own_card_or_add_template(fresh_db, user_id, monkeypatch):
    db = fresh_db
    bench = await db.fork_exercise_from_template(user_id, (await _template(db, BENCH))["id"])
    own_db = await db.fork_exercise_from_template(user_id, (await _template(db, DB_BENCH))["id"])
    edit = AsyncMock()
    monkeypatch.setattr(exercises.ui, "safe_edit", edit)
    state = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )

    await exercises.exm_alternatives(_callback(user_id, f"exm:alt:{bench}"), state)

    kb = edit.await_args.kwargs["reply_markup"]
    callbacks = _callbacks(kb)
    assert f"exm:ex:{own_db}" in callbacks
    assert any(cb.startswith("exm:tpladd:") for cb in callbacks)
    assert callbacks[-1] == f"exm:ex:{bench}"
    assert await state.get_state() == ExerciseManage.picking_exercise.state
