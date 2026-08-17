"""Editing a saved routine's exercise list: remove a single exercise (the
first tap only arms the same 🗑 button as "❗ Точно?" — the DB write happens
only on the second tap, rt:rmexyes) and add one via the groups→exercises
picker, including a catalog template forked on the fly or browsed from the
group's whole template list."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery

import formatting
import i18n
import keyboards
from fsm import RoutineFlow
from handlers import routines
from seed_data import PROGRAM_BY_KEY

pytestmark = pytest.mark.asyncio


def _make_callback(user_id: int, data: str = ""):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 1
    message.delete = AsyncMock()
    message.edit_text = AsyncMock()
    message.edit_reply_markup = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=1))
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    callback.bot = MagicMock()
    callback.bot.edit_message_reply_markup = AsyncMock()
    return callback


def _make_message(user_id: int, text: str):
    msg = MagicMock()
    msg.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    msg.text = text
    msg.answer = AsyncMock()
    msg.reply = AsyncMock()
    return msg


async def _make_state(user_id: int) -> FSMContext:
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=MemoryStorage(), key=key)


async def _make_routine(db, user_id: int, exercise_names: list[str]) -> tuple[int, int]:
    group_id = await db.create_muscle_group(user_id, "Грудь")
    routine_id = await db.create_routine(user_id, "Push day")
    for i, name in enumerate(exercise_names):
        ex_id = await db.create_exercise(user_id, name, group_id)
        await db.add_routine_exercise(routine_id, ex_id, i)
    return routine_id, group_id


# ---------- remove ----------


async def test_remove_first_tap_arms_the_button_without_deleting(fresh_db, user_id):
    """First tap on 🗑 only arms that row's button as "❗ Точно?" — the DB
    write waits for a second tap on rt:rmexyes."""
    db = fresh_db
    routine_id, _ = await _make_routine(db, user_id, ["Жим лёжа", "Разведения"])
    entries = await db.list_routine_exercises(routine_id)
    target = next(e for e in entries if e["display_name"] == "Жим лёжа")
    state = await _make_state(user_id)
    callback = _make_callback(user_id, f"rt:rmex:{routine_id}:{target['id']}")

    await routines.rt_remove_exercise_confirm(callback, state)

    assert [r["display_name"] for r in await db.list_routine_exercises(routine_id)] == [
        "Жим лёжа", "Разведения",
    ]
    kb = callback.message.edit_reply_markup.await_args.kwargs["reply_markup"]
    buttons = {b.callback_data: b.text for row in kb.inline_keyboard for b in row}
    assert buttons[f"rt:rmexyes:{routine_id}:{target['id']}"] == "❗ Точно?"
    # соседнюю строку не тронули — там всё ещё обычный 🗑
    other = next(e for e in entries if e["display_name"] == "Разведения")
    assert buttons[f"rt:rmex:{routine_id}:{other['id']}"] == "🗑"


async def test_arm_reverts_to_bin_after_timeout_without_confirmation(fresh_db, user_id):
    """No second tap in time — the button reverts to a plain 🗑 by itself."""
    db = fresh_db
    routine_id, _ = await _make_routine(db, user_id, ["Жим лёжа", "Разведения"])
    entries = await db.list_routine_exercises(routine_id)
    target = next(e for e in entries if e["display_name"] == "Жим лёжа")
    bot = MagicMock()
    bot.edit_message_reply_markup = AsyncMock()

    await routines._revert_rmex_arm(bot, user_id, 1, routine_id, target["id"], delay=0)

    kb = bot.edit_message_reply_markup.await_args.kwargs["reply_markup"]
    buttons = {b.callback_data: b.text for row in kb.inline_keyboard for b in row}
    assert buttons[f"rt:rmex:{routine_id}:{target['id']}"] == "🗑"


async def test_arm_revert_is_a_no_op_once_already_removed(fresh_db, user_id):
    """The exercise got deleted (second tap) before the revert timer fired —
    it must not resurrect a stale keyboard for a row that's already gone."""
    db = fresh_db
    routine_id, _ = await _make_routine(db, user_id, ["Жим лёжа", "Разведения"])
    entries = await db.list_routine_exercises(routine_id)
    target = next(e for e in entries if e["display_name"] == "Жим лёжа")
    await db.remove_routine_exercise(target["id"])
    bot = MagicMock()
    bot.edit_message_reply_markup = AsyncMock()

    await routines._revert_rmex_arm(bot, user_id, 1, routine_id, target["id"], delay=0)

    bot.edit_message_reply_markup.assert_not_awaited()


async def test_confirming_removal_drops_only_that_exercise(fresh_db, user_id):
    db = fresh_db
    routine_id, _ = await _make_routine(db, user_id, ["Жим лёжа", "Разведения"])
    entries = await db.list_routine_exercises(routine_id)
    target = next(e for e in entries if e["display_name"] == "Жим лёжа")
    state = await _make_state(user_id)

    await routines.rt_remove_exercise(
        _make_callback(user_id, f"rt:rmexyes:{routine_id}:{target['id']}"), state
    )

    remaining = await db.list_routine_exercises(routine_id)
    assert [r["display_name"] for r in remaining] == ["Разведения"]


async def test_remove_confirm_rejects_someone_elses_routine(fresh_db, user_id):
    db = fresh_db
    other_group = await db.create_muscle_group(999, "Грудь")
    other_ex = await db.create_exercise(999, "Жим", other_group)
    other_routine = await db.create_routine(999, "Not yours")
    await db.add_routine_exercise(other_routine, other_ex, 0)
    entries = await db.list_routine_exercises(other_routine)
    state = await _make_state(user_id)

    callback = _make_callback(user_id, f"rt:rmex:{other_routine}:{entries[0]['id']}")
    await routines.rt_remove_exercise_confirm(callback, state)

    callback.answer.assert_awaited_once_with(i18n.t("routine.alert.program_not_found"), show_alert=True)
    assert await db.list_routine_exercises(other_routine) == entries  # untouched


async def test_confirming_removal_rejects_someone_elses_routine(fresh_db, user_id):
    db = fresh_db
    other_group = await db.create_muscle_group(999, "Грудь")
    other_ex = await db.create_exercise(999, "Жим", other_group)
    other_routine = await db.create_routine(999, "Not yours")
    await db.add_routine_exercise(other_routine, other_ex, 0)
    entries = await db.list_routine_exercises(other_routine)
    state = await _make_state(user_id)

    callback = _make_callback(user_id, f"rt:rmexyes:{other_routine}:{entries[0]['id']}")
    await routines.rt_remove_exercise(callback, state)

    callback.answer.assert_awaited_once_with(i18n.t("routine.alert.program_not_found"), show_alert=True)
    assert await db.list_routine_exercises(other_routine) == entries  # untouched


# ---------- add: groups → exercises picker ----------


async def test_add_entry_sets_state_and_lists_groups(fresh_db, user_id):
    db = fresh_db
    routine_id, _ = await _make_routine(db, user_id, [])
    state = await _make_state(user_id)

    await routines.rt_add_exercise_start(_make_callback(user_id, f"rt:addex:{routine_id}"), state)

    assert await state.get_state() == RoutineFlow.adding_exercise_group
    assert (await state.get_data())["rtadd_routine_id"] == routine_id


async def test_picking_group_then_exercise_appends_it(fresh_db, user_id):
    db = fresh_db
    routine_id, group_id = await _make_routine(db, user_id, ["Жим лёжа"])
    ex_id = await db.create_exercise(user_id, "Отжимания", group_id)
    state = await _make_state(user_id)
    await state.update_data(rtadd_routine_id=routine_id)

    await routines.rtadd_pick_group(_make_callback(user_id, f"rtadd:grp:{group_id}"), state)
    assert await state.get_state() == RoutineFlow.adding_exercise_pick

    await routines.rtadd_pick_exercise(_make_callback(user_id, f"rtadd:ex:{ex_id}"), state)

    # Not appended yet — picking an exercise now asks for a sets/reps target first.
    assert await state.get_state() == RoutineFlow.adding_exercise_target
    names = [r["display_name"] for r in await db.list_routine_exercises(routine_id)]
    assert names == ["Жим лёжа"]

    await routines.rtadd_skip_target(_make_callback(user_id, "rtadd:notarget"), state)

    names = [r["display_name"] for r in await db.list_routine_exercises(routine_id)]
    assert names == ["Жим лёжа", "Отжимания"]  # appended after the existing one


async def test_adding_a_template_forks_it_first(fresh_db, user_id):
    db = fresh_db
    routine_id, _ = await _make_routine(db, user_id, [])
    template = await db._find_global_template_by_name("Жим штанги лёжа")
    assert template is not None, "seed template must exist for this test to mean anything"
    state = await _make_state(user_id)
    await state.update_data(rtadd_routine_id=routine_id)

    await routines.rtadd_pick_template(_make_callback(user_id, f"rtadd:tpladd:{template['id']}"), state)
    assert await state.get_state() == RoutineFlow.adding_exercise_target

    await routines.rtadd_skip_target(_make_callback(user_id, "rtadd:notarget"), state)

    names = [r["display_name"] for r in await db.list_routine_exercises(routine_id)]
    assert names == ["Жим штанги лёжа"]
    # It's a real owned exercise now, not the template row itself.
    owned = await db.find_exercise_by_name(user_id, "Жим штанги лёжа")
    assert owned is not None and owned["user_id"] == user_id


async def test_entering_a_target_saves_it_on_the_routine_exercise(fresh_db, user_id):
    db = fresh_db
    routine_id, group_id = await _make_routine(db, user_id, [])
    ex_id = await db.create_exercise(user_id, "Отжимания", group_id)
    state = await _make_state(user_id)
    await state.update_data(rtadd_routine_id=routine_id)

    await routines.rtadd_pick_exercise(_make_callback(user_id, f"rtadd:ex:{ex_id}"), state)
    await routines.rtadd_target_entered(_make_message(user_id, "3x8-12"), state)

    entries = await db.list_routine_exercises(routine_id)
    # Введённое человеком приводится к тому же виду, в котором схемы пишем мы
    # сами: иначе в одном дне соседствуют «4×6–10» от генератора и «3x8-12» от
    # руки — одна и та же вещь двумя наборами символов.
    assert entries[0]["target"] == "3×8–12"
    assert await state.get_state() is None


async def test_skipping_the_target_leaves_it_blank(fresh_db, user_id):
    db = fresh_db
    routine_id, group_id = await _make_routine(db, user_id, [])
    ex_id = await db.create_exercise(user_id, "Отжимания", group_id)
    state = await _make_state(user_id)
    await state.update_data(rtadd_routine_id=routine_id)

    await routines.rtadd_pick_exercise(_make_callback(user_id, f"rtadd:ex:{ex_id}"), state)
    await routines.rtadd_skip_target(_make_callback(user_id, "rtadd:notarget"), state)

    entries = await db.list_routine_exercises(routine_id)
    assert entries[0]["target"] is None


# ---------- reordering ----------


async def test_move_up_swaps_with_previous_exercise(fresh_db, user_id):
    db = fresh_db
    routine_id, _ = await _make_routine(db, user_id, ["Жим лёжа", "Разведения", "Отжимания"])
    entries = await db.list_routine_exercises(routine_id)
    middle = entries[1]
    state = await _make_state(user_id)

    await routines.rt_move_exercise(
        _make_callback(user_id, f"rt:mvex:{routine_id}:{middle['id']}:up"), state
    )

    names = [r["display_name"] for r in await db.list_routine_exercises(routine_id)]
    assert names == ["Разведения", "Жим лёжа", "Отжимания"]


async def test_move_down_swaps_with_next_exercise(fresh_db, user_id):
    db = fresh_db
    routine_id, _ = await _make_routine(db, user_id, ["Жим лёжа", "Разведения", "Отжимания"])
    entries = await db.list_routine_exercises(routine_id)
    first = entries[0]
    state = await _make_state(user_id)

    await routines.rt_move_exercise(
        _make_callback(user_id, f"rt:mvex:{routine_id}:{first['id']}:down"), state
    )

    names = [r["display_name"] for r in await db.list_routine_exercises(routine_id)]
    assert names == ["Разведения", "Жим лёжа", "Отжимания"]


async def test_move_up_at_top_wraps_to_the_end(fresh_db, user_id):
    """Стрелка в редакторе одна, и без переноса через край первое упражнение
    было бы намертво приколочено к первому месту."""
    db = fresh_db
    routine_id, _ = await _make_routine(db, user_id, ["Жим лёжа", "Разведения", "Брусья"])
    entries = await db.list_routine_exercises(routine_id)
    first = entries[0]
    state = await _make_state(user_id)

    await routines.rt_move_exercise(
        _make_callback(user_id, f"rt:mvex:{routine_id}:{first['id']}:up"), state
    )

    names = [r["display_name"] for r in await db.list_routine_exercises(routine_id)]
    assert names == ["Разведения", "Брусья", "Жим лёжа"]


async def test_move_rejects_someone_elses_routine(fresh_db, user_id):
    db = fresh_db
    other_group = await db.create_muscle_group(999, "Грудь")
    other_ex = await db.create_exercise(999, "Жим", other_group)
    other_routine = await db.create_routine(999, "Not yours")
    await db.add_routine_exercise(other_routine, other_ex, 0)
    entries = await db.list_routine_exercises(other_routine)
    state = await _make_state(user_id)

    callback = _make_callback(user_id, f"rt:mvex:{other_routine}:{entries[0]['id']}:up")
    await routines.rt_move_exercise(callback, state)

    callback.answer.assert_awaited_once_with(i18n.t("routine.alert.program_not_found"), show_alert=True)


async def test_group_screen_offers_a_catalog_button(fresh_db, user_id):
    """Раньше в списке группы были только свои упражнения — шаблон можно было
    добавить лишь угадав его название в поиске. Теперь есть прямой путь."""
    db = fresh_db
    template = await db._find_global_template_by_name("Жим штанги лёжа")
    group_id = template["primary_group_id"]
    routine_id, _ = await _make_routine(db, user_id, [])
    state = await _make_state(user_id)
    await state.update_data(rtadd_routine_id=routine_id)

    callback = _make_callback(user_id, f"rtadd:grp:{group_id}")
    await routines.rtadd_pick_group(callback, state)

    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    cbs = {b.callback_data for row in kb.inline_keyboard for b in row}
    assert "rtadd:catalog" in cbs


async def test_catalog_button_browses_the_groups_templates(fresh_db, user_id):
    db = fresh_db
    template = await db._find_global_template_by_name("Жим штанги лёжа")
    group_id = template["primary_group_id"]
    routine_id, _ = await _make_routine(db, user_id, [])
    state = await _make_state(user_id)
    await state.update_data(rtadd_routine_id=routine_id, rtadd_group_id=group_id)
    await state.set_state(RoutineFlow.adding_exercise_pick)

    callback = _make_callback(user_id, "rtadd:catalog")
    await routines.rtadd_catalog(callback, state)

    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    cbs = {b.callback_data for row in kb.inline_keyboard for b in row}
    assert f"rtadd:tpladd:{template['id']}" in cbs
    assert "rtadd:catalogback" in cbs


async def test_picking_from_the_catalog_forks_and_appends_it(fresh_db, user_id):
    db = fresh_db
    template = await db._find_global_template_by_name("Жим штанги лёжа")
    routine_id, _ = await _make_routine(db, user_id, [])
    state = await _make_state(user_id)
    await state.update_data(rtadd_routine_id=routine_id)

    await routines.rtadd_pick_template(
        _make_callback(user_id, f"rtadd:tpladd:{template['id']}"), state
    )
    await routines.rtadd_skip_target(_make_callback(user_id, "rtadd:notarget"), state)

    names = [r["display_name"] for r in await db.list_routine_exercises(routine_id)]
    assert names == [template["display_name"]]


async def test_catalogback_returns_to_the_exercise_list(fresh_db, user_id):
    db = fresh_db
    template = await db._find_global_template_by_name("Жим штанги лёжа")
    group_id = template["primary_group_id"]
    routine_id, _ = await _make_routine(db, user_id, [])
    await db.create_exercise(user_id, "Отжимания", group_id)
    state = await _make_state(user_id)
    await state.update_data(rtadd_routine_id=routine_id, rtadd_group_id=group_id)
    await state.set_state(RoutineFlow.adding_exercise_pick)

    callback = _make_callback(user_id, "rtadd:catalogback")
    await routines.rtadd_catalog_back(callback, state)

    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert any("Отжимания" in t for t in texts)


async def test_search_text_offers_both_own_matches_and_templates(fresh_db, user_id):
    db = fresh_db
    routine_id, group_id = await _make_routine(db, user_id, [])
    await db.create_exercise(user_id, "Жим гантелей лёжа", group_id)
    state = await _make_state(user_id)
    await state.update_data(rtadd_routine_id=routine_id)
    await state.set_state(RoutineFlow.adding_exercise_group)

    message = _make_message(user_id, "жим")
    await routines.rtadd_search_text(message, state)

    kb = message.answer.await_args.kwargs["reply_markup"]
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert any("Жим гантелей лёжа" in t for t in texts)
    assert any(t.startswith("📋") for t in texts)  # a catalog template is offered too
    assert await state.get_state() == RoutineFlow.adding_exercise_pick


async def test_cancel_returns_to_routine_detail_without_changes(fresh_db, user_id):
    db = fresh_db
    routine_id, _ = await _make_routine(db, user_id, ["Жим лёжа"])
    state = await _make_state(user_id)
    await state.update_data(rtadd_routine_id=routine_id)
    await state.set_state(RoutineFlow.adding_exercise_group)

    await routines.rtadd_cancel(_make_callback(user_id, "rtadd:cancel"), state)

    names = [r["display_name"] for r in await db.list_routine_exercises(routine_id)]
    assert names == ["Жим лёжа"]  # unchanged


# ---------- create from workout: tap-to-preview before naming ----------


async def _finished_workout_with(db, user_id: int, ex_names: list[str], started_at: str = "2026-07-15T10:00:00") -> int:
    group_id = await db.create_muscle_group(user_id, "Грудь")
    wid = await db.create_finished_workout(user_id, started_at, "2026-07-15T11:00:00")
    for name in ex_names:
        ex_id = await db.create_exercise(user_id, name, group_id)
        block_id = await db.create_block(wid, "single")
        await db.add_block_exercise(block_id, ex_id, 0)
        await db.add_set(block_id, ex_id, 1, 0, 100.0, 8)
    return wid


async def test_tapping_a_workout_previews_its_full_exercise_list_before_naming(fresh_db, user_id):
    db = fresh_db
    wid = await _finished_workout_with(
        db, user_id, ["Приседания со штангой на плечах", "Жим штанги лёжа широким хватом"]
    )
    state = await _make_state(user_id)

    callback = _make_callback(user_id, f"rt:pickw:item:{wid}")
    await routines.rt_pickw_item(callback, state)

    # Both full names show up untruncated — unlike the picker button label, which
    # is capped and would otherwise hide half of a long exercise name.
    text = callback.message.answer.await_args.args[0]
    assert "Приседания со штангой на плечах" in text
    assert "Жим штанги лёжа широким хватом" in text
    # Naming hasn't started yet — the preview asks for confirmation first.
    assert await state.get_state() != RoutineFlow.naming
    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"rt:pickw:use:{wid}" in cbs
    assert "rt:pickw:back" in cbs


async def test_confirming_the_preview_moves_to_naming(fresh_db, user_id):
    db = fresh_db
    wid = await _finished_workout_with(db, user_id, ["Жим лёжа"])
    state = await _make_state(user_id)

    await routines.rt_pickw_use(_make_callback(user_id, f"rt:pickw:use:{wid}"), state)

    assert await state.get_state() == RoutineFlow.naming
    assert (await state.get_data())["routine_source_workout_id"] == wid


async def test_the_source_list_spells_out_every_workout_above_numbered_buttons(fresh_db, user_id):
    """Кнопка несёт номер и дату, а что за тренировка — расписано в тексте, как
    на экране «повторить тренировку». Раньше упражнения жили в подписи кнопки и
    обрезались, из-за чего однотипные тренировки выглядели одинаково."""
    db = fresh_db
    wid = await _finished_workout_with(
        db, user_id, ["Приседания со штангой на плечах", "Жим штанги лёжа широким хватом"]
    )
    callback = _make_callback(user_id, "rt:pickw")
    await routines._show_routine_source_picker(callback, await _make_state(user_id), page=0)

    text = callback.message.answer.await_args.args[0]
    assert text.startswith("🗂 Из какой тренировки создать программу?")
    assert "<b>1 · 15.07.2026 (ср)</b>" in text
    assert "• Приседания со штангой на плечах [ГРУДЬ]" in text
    assert "• Жим штанги лёжа широким хватом [ГРУДЬ]" in text

    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    button = next(b for row in kb.inline_keyboard for b in row if b.callback_data == f"rt:pickw:item:{wid}")
    assert button.text == "1 - 15.07.2026 (ср)"


async def test_back_from_preview_returns_to_the_stored_list_page(fresh_db, user_id):
    db = fresh_db
    # 9 workouts so page 1 (ROUTINE_SOURCE_PAGE_SIZE=6) actually holds some — the
    # oldest among them, since list_workouts orders newest-first. Distinct dates
    # (rather than 9 identical ones) so that ordering doesn't depend on however
    # SQLite happens to break ties.
    for day in range(8, 0, -1):
        await _finished_workout_with(db, user_id, ["Жим лёжа"], started_at=f"2026-07-{day:02d}T10:00:00")
    oldest_wid = await _finished_workout_with(db, user_id, ["Тяга"], started_at="2026-06-01T10:00:00")
    state = await _make_state(user_id)

    await routines.rt_pickw_page(_make_callback(user_id, "rt:pickw:page:1"), state)
    assert (await state.get_data())["routine_source_page"] == 1

    callback = _make_callback(user_id, "rt:pickw:back")
    await routines.rt_pickw_back(callback, state)

    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"rt:pickw:item:{oldest_wid}" in cbs


# ---------- programs list is two-stage: program → its days ----------


async def _manage_buttons(fresh_db, user_id: int) -> list[tuple[str, str]]:
    callback = _make_callback(user_id, "rt:manage")
    await routines.rt_manage(callback, await _make_state(user_id))
    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    return [(b.text, b.callback_data) for row in kb.inline_keyboard for b in row]


async def test_adding_a_catalog_program_shows_one_row_not_one_per_day(fresh_db, user_id):
    """Раньше трёхдневный сплит занимал три строки списка и день было не
    соотнести с программой; теперь это одна кнопка со вторым экраном."""
    callback = _make_callback(user_id, "rt:progadd:ppl")
    await routines.rt_program_add(callback, await _make_state(user_id))

    program = PROGRAM_BY_KEY["ppl"]
    buttons = await _manage_buttons(fresh_db, user_id)
    program_rows = [b for b in buttons if b[1].startswith("rt:prg:")]

    assert len(program_rows) == 1
    assert program["name"] in program_rows[0][0]
    # Ни один день не просочился в верхний уровень списка.
    assert not [b for b in buttons if b[1].startswith("rt:view:")]


async def test_opening_a_program_lists_its_days(fresh_db, user_id):
    await routines.rt_program_add(
        _make_callback(user_id, "rt:progadd:ppl"), await _make_state(user_id)
    )
    buttons = await _manage_buttons(fresh_db, user_id)
    program_cb = next(b[1] for b in buttons if b[1].startswith("rt:prg:"))

    callback = _make_callback(user_id, program_cb)
    await routines.rt_program(callback, await _make_state(user_id))

    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    labels = [b.text for row in kb.inline_keyboard for b in row]
    day_names = [day_name for day_name, _ex in PROGRAM_BY_KEY["ppl"]["days"]]
    # На новой программе дни идут просто списком: очереди ещё нет, а выделять
    # первый день незачем — он и так первый (см. keyboards.program_days_keyboard).
    assert labels[: len(day_names)] == day_names
    assert labels[-1] == "⬅️ Назад"


async def test_a_standalone_routine_still_opens_straight_into_its_card(fresh_db, user_id):
    await fresh_db.create_routine(user_id, "Своя тренировка")

    buttons = await _manage_buttons(fresh_db, user_id)

    assert [b for b in buttons if b[1].startswith("rt:view:")]
    assert not [b for b in buttons if b[1].startswith("rt:prg:")]


async def test_a_program_day_goes_back_to_its_day_list_not_the_top(fresh_db, user_id):
    await routines.rt_program_add(
        _make_callback(user_id, "rt:progadd:ppl"), await _make_state(user_id)
    )
    day = (await fresh_db.list_program_days(user_id, PROGRAM_BY_KEY["ppl"]["name"]))[0]

    callback = _make_callback(user_id, f"rt:view:{day['id']}")
    await routines.rt_view(callback, await _make_state(user_id))

    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    back = [b.callback_data for row in kb.inline_keyboard for b in row][-1]
    assert back.startswith("rt:prg:")


async def test_a_standalone_routine_goes_back_to_the_top_list(fresh_db, user_id):
    rid = await fresh_db.create_routine(user_id, "Своя тренировка")

    callback = _make_callback(user_id, f"rt:view:{rid}")
    await routines.rt_view(callback, await _make_state(user_id))

    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    back = [b.callback_data for row in kb.inline_keyboard for b in row][-1]
    assert back == "rt:manage"


async def test_renaming_a_program_from_its_day_screen(fresh_db, user_id):
    """Программы, восстановленные из старых данных, получают датированную
    заглушку вместо имени — переименование и есть то, чем она перестаёт ей быть."""
    for day in ("День 1", "День 2"):
        await fresh_db.create_routine(user_id, day, program_name="Программа от 28.07")
    program_id = (await fresh_db.list_programs(user_id))[0]["id"]
    state = await _make_state(user_id)

    await routines.rt_program_rename(_make_callback(user_id, f"rt:pgmrename:{program_id}"), state)
    assert await state.get_state() == RoutineFlow.renaming_program

    await routines.rt_program_rename_entered(_make_message(user_id, "Верх/низ"), state)

    assert [p["program_name"] for p in await fresh_db.list_programs(user_id)] == ["Верх/низ"]
    assert await state.get_state() is None


async def test_renaming_rejects_something_that_is_not_a_program(fresh_db, user_id):
    """rt:pgmrename адресуется программе, а не её «якорному» дню, так что id
    одиночной программы (это routine, не program) сюда просто не подходит."""
    rid = await fresh_db.create_routine(user_id, "Своя тренировка")
    callback = _make_callback(user_id, f"rt:pgmrename:{rid}")

    await routines.rt_program_rename(callback, await _make_state(user_id))

    callback.answer.assert_awaited_once_with(i18n.t("routine.alert.program_not_found"), show_alert=True)


async def test_target_normalization_keeps_what_it_cannot_parse():
    """В каталоге есть «3×30–60 сек» — схема по времени. Строго отбивать
    неразобранное значило бы запретить формат, который бот использует сам."""
    assert formatting.normalize_routine_target("5 x 3 - 5") == "5×3–5"
    assert formatting.normalize_routine_target("4Х8") == "4×8"
    assert formatting.normalize_routine_target("3×30–60 сек") == "3×30–60 сек"
    assert formatting.normalize_routine_target("как пойдёт") == "как пойдёт"
    # Диапазон наоборот — не схема, а опечатка: оставляем как есть, не выдумываем.
    assert formatting.normalize_routine_target("3x10-5") == "3x10-5"

async def test_editor_row_puts_the_numbered_name_first_then_the_icons():
    """Раскладка ряда: номер с именем, ⬆️, 🗑 — три колонки, не четыре.

    Карандаша нет: он вёл туда же, куда тап по имени, а забирал четверть ряда
    (Telegram делит ряд поровну) — и рядом стояли «Жим гантелей лё…» и «Жим
    гантелей си…». Номер оставлен потому, что обрезка всё равно возможна, а по
    номеру видно, какое именно упражнение из списка в тексте это ряд."""
    kb = keyboards.routine_edit_keyboard(1, [(10, "Присед в Смите", "3x5-10"), (11, "Тяга", None)])

    first = kb.inline_keyboard[0]
    assert len(first) == 3
    assert first[0].text == "1. Присед в Смите"
    assert [b.text for b in first[1:]] == ["⬆️", "🗑"]
    assert kb.inline_keyboard[1][0].text == "2. Тяга"


async def test_every_row_has_an_arrow_because_the_move_is_cyclic():
    kb = keyboards.routine_edit_keyboard(1, [(10, "Жим", None), (11, "Тяга", None), (12, "Присед", None)])

    ups = [b.callback_data for row in kb.inline_keyboard[:3] for b in row if b.text == "⬆️"]
    assert ups == ["rt:mvex:1:10:up", "rt:mvex:1:11:up", "rt:mvex:1:12:up"]


async def test_tapping_the_name_opens_the_set_scheme():
    """Имя — не украшение: тап по нему делает то же, что ✏️, иначе самая крупная
    кнопка ряда была бы единственной неработающей."""
    kb = keyboards.routine_edit_keyboard(1, [(10, "Жим", None)])

    assert kb.inline_keyboard[0][0].callback_data == "rt:extarget:1:10"


async def test_a_long_name_is_cut_by_us_with_an_ellipsis():
    kb = keyboards.routine_edit_keyboard(1, [(10, "Жим в тренажёре на плечи", None)])

    assert kb.inline_keyboard[0][0].text.endswith("…")


async def test_a_day_cannot_take_the_name_of_another_day_in_the_same_program(
    fresh_db, user_id
):
    """Два «Дня 1» в одной программе — это две одинаковые кнопки на экране, из
    которых не выбрать. Многодневную программу от этого защищал отдельный
    обработчик, а день переименовывался во что угодно молча."""
    program_id = await fresh_db.create_program(user_id, "Сплит")
    first = await fresh_db.create_routine(user_id, "День 1", program_id=program_id)
    second = await fresh_db.create_routine(user_id, "День 2", program_id=program_id)

    state = await _make_state(user_id)
    await state.update_data(routine_rename_id=second)
    message = _make_message(user_id, "День 1")

    await routines.rt_rename_entered(message, state)

    message.reply.assert_awaited_once()
    assert "уже есть" in message.reply.await_args.args[0]
    assert (await fresh_db.get_routine(second))["name"] == "День 2"
    assert (await fresh_db.get_routine(first))["name"] == "День 1"


async def test_the_same_day_name_in_a_different_program_is_fine(fresh_db, user_id):
    """«День 1» есть в каждой второй программе — сверяемся только со своими."""
    other = await fresh_db.create_program(user_id, "Старая")
    await fresh_db.create_routine(user_id, "День 1", program_id=other)
    mine = await fresh_db.create_program(user_id, "Новая")
    day = await fresh_db.create_routine(user_id, "Первый", program_id=mine)

    state = await _make_state(user_id)
    await state.update_data(routine_rename_id=day)
    message = _make_message(user_id, "День 1")

    await routines.rt_rename_entered(message, state)

    assert (await fresh_db.get_routine(day))["name"] == "День 1"


async def test_a_standalone_routine_cannot_take_a_saved_program_name(fresh_db, user_id):
    """В списке 🗂 Программы одиночные и многодневки лежат вперемешку — тёзки
    там неразличимы ровно так же."""
    await fresh_db.create_program(user_id, "Домашка")
    solo = await fresh_db.create_routine(user_id, "Зал")

    state = await _make_state(user_id)
    await state.update_data(routine_rename_id=solo)
    message = _make_message(user_id, "домашка")

    await routines.rt_rename_entered(message, state)

    message.reply.assert_awaited_once()
    assert "уже есть" in message.reply.await_args.args[0]
    assert (await fresh_db.get_routine(solo))["name"] == "Зал"


async def test_a_new_day_cannot_be_named_after_an_existing_one(fresh_db, user_id):
    """Живой прогон завёл в одной программе два «Тест верх 2» подряд: проверка
    стояла только на переименовании, а создание дня её не знало. На экране
    программы это две одинаковые кнопки, из которых не выбрать."""
    program_id = await fresh_db.create_program(user_id, "Сплит")
    await fresh_db.create_routine(user_id, "Верх", program_id=program_id)

    state = await _make_state(user_id)
    await state.update_data(day_program_id=program_id, day_copy_from=None)
    message = _make_message(user_id, "верх")

    await routines.rt_day_named(message, state)

    message.reply.assert_awaited_once()
    assert "уже есть" in message.reply.await_args.args[0]
    assert len(await fresh_db.list_program_days_by_id(program_id)) == 1


async def test_an_empty_day_does_not_blame_archiving(fresh_db, user_id):
    """«Возможно, они были архивированы» на только что созданном пустом дне —
    объяснение событием, которого не было. Пустой день заводят как раз затем,
    чтобы наполнить его руками."""
    program_id = await fresh_db.create_program(user_id, "Сплит")
    day = await fresh_db.create_routine(user_id, "Руки", program_id=program_id)

    blocks = await routines._day_composition_blocks(
        await fresh_db.list_program_days_by_id(program_id)
    )

    assert "архивирован" not in blocks[0]
    assert "Редактировать" in blocks[0]
    assert day is not None
