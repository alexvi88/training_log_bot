"""§A2 — edit a past (finished) workout: add/remove/edit sets, change date."""

import datetime as dt
from contextlib import suppress
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

import achievement_sync
import config
import db
import formatting
import keyboards
import timeutil
import ui
from fsm import EditWorkoutFlow
from parser import ParseError, parse_ru_date, parse_sets_line, parse_single_token

router = Router(name="edit_workout")


async def _delete_message(message: Message):
    with suppress(TelegramBadRequest):
        await message.delete()


async def _on_workout_edited(workout_id: int, keep_block_id: int | None = None) -> None:
    """Common housekeeping after any change to a past workout's sets or date:
    drop blocks a delete_set/rmex left with no sets (they'd otherwise linger
    as a "подходов нет" row forever, since delete_empty_blocks is normally
    only run once, at the moment a workout finishes), and drop the cached
    AI-trainer comment — it describes numbers that just changed underneath it.
    keep_block_id spares the block the user is currently on, so deleting a
    last set keeps the exercise screen open (its own empty state handles the
    display) instead of reaping the block out from under them.
    """
    await db.delete_empty_blocks(workout_id, keep_block_id=keep_block_id)
    await db.set_workout_ai_comment(workout_id, None)
    # Badges are derived from the sets: fixing a mistyped 500кг down to 50кг (or
    # deleting that set) has to take back the weight-club/tonnage badges it
    # unlocked, and a corrected date can just as well complete a streak.
    workout = await db.get_workout(workout_id)
    if workout is not None:
        await achievement_sync.resync(workout["user_id"])


async def _edit_screen_payload(workout_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Top level: one row per exercise, with its set count."""
    workout = await db.get_workout(workout_id)
    started = dt.datetime.fromisoformat(workout["started_at"])

    exercises: list[tuple[int, int, str]] = []
    for block in await db.list_blocks_for_workout(workout_id):
        for be in await db.get_block_exercises(block["id"]):
            exercises.append((block["id"], be["exercise_id"], be["display_name"]))

    text = f"✏️ Редактирование · {formatting.format_date_ru(started)}"
    if exercises:
        text += "\nВыбери упражнение."
    else:
        text += "\n\nВ тренировке пока нет упражнений."
    kb = keyboards.edit_workout_keyboard(exercises)
    return text, kb


async def _exercise_screen_payload(
    workout_id: int, block_id: int, exercise_id: int
) -> tuple[str, InlineKeyboardMarkup] | None:
    """Second level: one exercise's sets. None if it's no longer in the workout."""
    block_exs = await db.get_block_exercises(block_id)
    name = next((be["display_name"] for be in block_exs if be["exercise_id"] == exercise_id), None)
    if name is None:
        return None
    sets = [s for s in await db.list_sets_for_block(block_id) if s["exercise_id"] == exercise_id]
    items = [
        (s["id"], f"{i} - {formatting.format_set(s['weight'], s['reps'], s['rpe'])}")
        for i, s in enumerate(sets, start=1)
    ]
    text = f"✏️ <b>{escape(name)}</b>"
    if items:
        text += "\nНажми на подход, чтобы изменить или удалить.\n<i>Или напиши новый подход: «100 8».</i>"
    else:
        text += "\n<i>Подходов нет. Напиши подход, например «100 8».</i>"
    return text, keyboards.edit_exercise_keyboard(block_id, exercise_id, items)


async def show_edit_screen(event, state: FSMContext, workout_id: int) -> bool:
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != event.from_user.id:
        if isinstance(event, CallbackQuery):
            await event.answer("Тренировка не найдена", show_alert=True)
        else:
            await event.reply("Тренировка не найдена")
        return False
    # Landing on the top-level list means leaving whichever exercise screen (if
    # any) was open — reap blocks that were emptied there and abandoned, same
    # as the original always-reap behaviour, just deferred to this point.
    await db.delete_empty_blocks(workout_id)
    await state.set_state(EditWorkoutFlow.viewing)
    await state.update_data(edit_workout_id=workout_id, edit_block_id=None, edit_exercise_id=None)
    text, kb = await _edit_screen_payload(workout_id)
    if isinstance(event, CallbackQuery):
        await ui.safe_edit(event, text, reply_markup=kb)
    else:
        await event.answer(text, reply_markup=kb)
    return True


async def show_exercise_screen(
    event, state: FSMContext, workout_id: int, block_id: int, exercise_id: int
) -> bool:
    payload = await _exercise_screen_payload(workout_id, block_id, exercise_id)
    if payload is None:
        # The exercise was removed (e.g. its last set deleted took the block with
        # it) — fall back to the list rather than showing an empty dead screen.
        return await show_edit_screen(event, state, workout_id)
    text, kb = payload
    await state.set_state(EditWorkoutFlow.viewing_exercise)
    await state.update_data(
        edit_workout_id=workout_id, edit_block_id=block_id, edit_exercise_id=exercise_id
    )
    if isinstance(event, CallbackQuery):
        await ui.safe_edit(event, text, reply_markup=kb, parse_mode="HTML")
    else:
        await event.answer(text, reply_markup=kb, parse_mode="HTML")
    return True


async def _back_to_current_screen(event, state: FSMContext, workout_id: int) -> bool:
    """Redraw whichever level the user is on — set edits should return to the
    exercise they came from, not all the way up to the exercise list."""
    data = await state.get_data()
    block_id, exercise_id = data.get("edit_block_id"), data.get("edit_exercise_id")
    if block_id is not None and exercise_id is not None:
        return await show_exercise_screen(event, state, workout_id, block_id, exercise_id)
    return await show_edit_screen(event, state, workout_id)


@router.callback_query(StateFilter(EditWorkoutFlow.viewing), F.data.startswith("editw:ex:"))
async def editw_pick_exercise(callback: CallbackQuery, state: FSMContext):
    _, _, block_id_str, ex_id_str = callback.data.split(":")
    workout_id = await _require_edit_workout_id(callback, state)
    if workout_id is None:
        return
    await show_exercise_screen(callback, state, workout_id, int(block_id_str), int(ex_id_str))
    await callback.answer()


@router.callback_query(
    StateFilter(EditWorkoutFlow.viewing, EditWorkoutFlow.viewing_exercise), F.data == "editw:top"
)
async def editw_to_top(callback: CallbackQuery, state: FSMContext):
    workout_id = await _require_edit_workout_id(callback, state)
    if workout_id is None:
        return
    await show_edit_screen(callback, state, workout_id)
    await callback.answer()


@router.callback_query(StateFilter(EditWorkoutFlow.viewing_exercise), F.data.startswith("editw:set:"))
async def editw_pick_set(callback: CallbackQuery, state: FSMContext):
    set_id = int(callback.data.split(":")[2])
    if await db.get_set_owner(set_id) != callback.from_user.id:
        await callback.answer("Подход не найден", show_alert=True)
        return
    row = await db.get_set(set_id)
    ex = await db.get_exercise(row["exercise_id"])
    text = f"{ex['display_name']}: {formatting.format_set(row['weight'], row['reps'], row['rpe'])}"
    await ui.safe_edit(callback, text, reply_markup=keyboards.set_actions_keyboard(set_id))
    await callback.answer()


async def _require_edit_workout_id(callback: CallbackQuery, state: FSMContext) -> int | None:
    data = await state.get_data()
    workout_id = data.get("edit_workout_id")
    if workout_id is None:
        await callback.answer("Сессия истекла, открой тренировку из истории заново", show_alert=True)
    return workout_id


@router.callback_query(F.data == "editw:back")
async def editw_back(callback: CallbackQuery, state: FSMContext):
    workout_id = await _require_edit_workout_id(callback, state)
    if workout_id is None:
        return
    if await _back_to_current_screen(callback, state, workout_id):
        await callback.answer()


@router.callback_query(F.data.startswith("editw:delset:"))
async def editw_delset(callback: CallbackQuery, state: FSMContext):
    set_id = int(callback.data.split(":")[2])
    if await db.get_set_owner(set_id) != callback.from_user.id:
        await callback.answer("Подход не найден", show_alert=True)
        return
    workout_id = await _require_edit_workout_id(callback, state)
    if workout_id is None:
        return
    data = await state.get_data()
    await db.delete_set(set_id)
    # Keep the block the user is on even if this was its last set — they stay
    # on the exercise screen and see its "Подходов нет" empty state, rather
    # than getting bounced up to the list. The block still gets reaped once
    # they leave (editw_to_top / editw_done), same as before.
    await _on_workout_edited(workout_id, keep_block_id=data.get("edit_block_id"))
    await callback.answer("Подход удалён")
    await _back_to_current_screen(callback, state, workout_id)


@router.callback_query(F.data.startswith("editw:editset:"))
async def editw_editset_prompt(callback: CallbackQuery, state: FSMContext):
    set_id = int(callback.data.split(":")[2])
    if await db.get_set_owner(set_id) != callback.from_user.id:
        await callback.answer("Подход не найден", show_alert=True)
        return
    await state.update_data(edit_set_id=set_id)
    await state.set_state(EditWorkoutFlow.editing_set)
    row = await db.get_set(set_id)
    await ui.safe_edit(
        callback,
        f"Текущее значение: {formatting.format_set(row['weight'], row['reps'], row['rpe'])}\n"
        "Напиши новый вес и повторы (например «100 8»):",
        reply_markup=keyboards.cancel_keyboard("editw:back"),
    )
    await callback.answer()


@router.message(StateFilter(EditWorkoutFlow.editing_set), F.text)
async def editw_editset_entered(message: Message, state: FSMContext):
    try:
        parsed = parse_single_token(message.text)
    except ParseError as e:
        await ui.reply_transient(message, e.message)
        return
    data = await state.get_data()
    await db.update_set(data["edit_set_id"], parsed[0].weight, parsed[0].reps, parsed[0].rpe)
    await _on_workout_edited(data["edit_workout_id"])
    # No "Готово." reply: the redrawn screen below already shows the new value,
    # and a confirmation reply would stay in the chat forever (the user's own
    # message is deleted, so five edits used to leave five stray "Готово.").
    await _delete_message(message)
    await _back_to_current_screen(message, state, data["edit_workout_id"])


@router.callback_query(F.data.startswith("editw:addset:"))
async def editw_addset_prompt(callback: CallbackQuery, state: FSMContext):
    _, _, block_id_str, ex_id_str = callback.data.split(":")
    block_id = int(block_id_str)
    if await db.get_block_owner(block_id) != callback.from_user.id:
        await callback.answer("Блок не найден", show_alert=True)
        return
    await state.update_data(add_block_id=block_id, add_exercise_id=int(ex_id_str))
    await state.set_state(EditWorkoutFlow.adding_set)
    ex = await db.get_exercise(int(ex_id_str))
    await ui.safe_edit(
        callback,
        f"Новый подход для «{ex['display_name']}» — напиши вес и повторы (например «100 8», можно «100x8x3»):",
        reply_markup=keyboards.cancel_keyboard("editw:back"),
    )
    await callback.answer()


@router.message(StateFilter(EditWorkoutFlow.adding_set), F.text)
async def editw_addset_entered(message: Message, state: FSMContext):
    try:
        parsed = parse_sets_line(message.text)
    except ParseError as e:
        await ui.reply_transient(message, e.message)
        return
    data = await state.get_data()
    ex_id = data["add_exercise_id"]
    block_id = data.get("add_block_id")
    if block_id is None:
        # A brand-new exercise for this workout (via "➕ Новое упражнение") — the
        # block is only created now, on the first real set, so cancelling the
        # weight/reps prompt never leaves an empty exercise behind.
        block_id = await db.create_block(data["edit_workout_id"], "single")
        await db.add_block_exercise(block_id, ex_id, 0)
        await db.touch_exercise_last_used(ex_id)
        order_in_round = 0
    else:
        block_exs = await db.get_block_exercises(block_id)
        order_in_round = next((be["order_in_block"] for be in block_exs if be["exercise_id"] == ex_id), 0)
    for ps in parsed:
        await db.append_set(block_id, ex_id, order_in_round, ps.weight, ps.reps, ps.rpe)
    await _on_workout_edited(data["edit_workout_id"])
    await _delete_message(message)
    # Land on the exercise the set belongs to, so several sets can be typed in a
    # row without walking back down from the list each time.
    await state.update_data(edit_block_id=block_id, edit_exercise_id=ex_id)
    await _back_to_current_screen(message, state, data["edit_workout_id"])


@router.message(StateFilter(EditWorkoutFlow.viewing_exercise), F.text)
async def editw_typed_set(message: Message, state: FSMContext):
    """"100 8" typed on an exercise's screen adds that set, same as in the live
    tracker — the parser and the "➕ Сет" prompt already accept exactly this, so
    dropping it into the "Не понял" fallback was the odd one out."""
    data = await state.get_data()
    workout_id, block_id, ex_id = (
        data.get("edit_workout_id"), data.get("edit_block_id"), data.get("edit_exercise_id")
    )
    if workout_id is None or block_id is None or ex_id is None:
        return
    try:
        parsed = parse_sets_line(message.text)
    except ParseError as e:
        await ui.reply_transient(message, e.message)
        return
    block_exs = await db.get_block_exercises(block_id)
    order_in_round = next((be["order_in_block"] for be in block_exs if be["exercise_id"] == ex_id), 0)
    for ps in parsed:
        await db.append_set(block_id, ex_id, order_in_round, ps.weight, ps.reps, ps.rpe)
    await _on_workout_edited(workout_id)
    await _delete_message(message)
    await show_exercise_screen(message, state, workout_id, block_id, ex_id)


@router.message(StateFilter(EditWorkoutFlow.viewing))
async def editw_typed_at_top(message: Message, state: FSMContext):
    """At the exercise list there's no active exercise to attach a set to, so
    this points at the next step instead of falling through to "Не понял"."""
    await message.reply("Открой упражнение и напиши подход там — или добавь новое кнопкой ниже.")


@router.callback_query(F.data.startswith("editw:rmexask:"))
async def editw_remove_exercise_confirm(callback: CallbackQuery, state: FSMContext):
    """Removing an exercise takes every set it has with it — unlike deleting one
    set, that's not something to do on a single mistap, so it asks first and says
    how much is going."""
    block_id = int(callback.data.split(":")[2])
    if await db.get_block_owner(block_id) != callback.from_user.id:
        await callback.answer("Упражнение не найдено", show_alert=True)
        return
    data = await state.get_data()
    ex_id = data.get("edit_exercise_id")
    block_exs = await db.get_block_exercises(block_id)
    name = next((be["display_name"] for be in block_exs if be["exercise_id"] == ex_id), None)
    if name is None:
        name = block_exs[0]["display_name"] if block_exs else "упражнение"
    count = sum(1 for s in await db.list_sets_for_block(block_id) if s["exercise_id"] == ex_id)
    # Творительный падеж — фраза ниже ставит слово после «вместе с» («с 1
    # подходом», «с 2 подходами»), а не «N подходов сделано», где верны
    # именительные формы. «Сет» запрещён словарём TONE_OF_VOICE — «подход».
    word = formatting.plural_ru(count, ("подходом", "подходами", "подходами"))
    kb = keyboards.yes_no_keyboard(
        yes_cb=f"editw:rmex:{block_id}",
        no_cb="editw:back",
        yes_text="🗑 Убрать",
        no_text="❌ Отмена",
    )
    await ui.safe_edit(
        callback,
        f"Убрать <b>{escape(name)}</b> из тренировки вместе с {count} {word}?",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("editw:rmex:"))
async def editw_remove_exercise(callback: CallbackQuery, state: FSMContext):
    """Drop an exercise from a past workout entirely — every set it has.
    Reached only through editw:rmexask, which asks first."""
    block_id = int(callback.data.split(":")[2])
    if await db.get_block_owner(block_id) != callback.from_user.id:
        await callback.answer("Упражнение не найдено", show_alert=True)
        return
    workout_id = await _require_edit_workout_id(callback, state)
    if workout_id is None:
        return
    await db.delete_block_and_sets(block_id)
    # Через общий хелпер, а не «сбросить комментарий и всё»: убрать упражнение
    # целиком — то же самое для данных, что удалить его подходы по одному, и
    # значки должны сниматься так же. Раньше «Клуб 140» за единственный сет на
    # 150 кг оставался в профиле навсегда, хотя самого сета в истории уже нет.
    await _on_workout_edited(workout_id)
    await callback.answer("Упражнение убрано из тренировки")
    await state.update_data(edit_block_id=None, edit_exercise_id=None)
    await show_edit_screen(callback, state, workout_id)


# ---------- adding a wholly new exercise to a past workout ----------
#
# Same groups → exercises (+ search, + template forking) picker shape as the
# routine editor's "➕ Добавить упражнение", under yet another prefix
# ("editwex") so its callback data can't collide. Landing state is the
# existing EditWorkoutFlow.adding_set with add_block_id left None — see
# editw_addset_entered for why the block itself waits for a real set.

async def _editwex_groups_screen(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(EditWorkoutFlow.adding_exercise_group)
    groups = await db.list_muscle_groups(callback.from_user.id)
    kb = keyboards.groups_keyboard(
        groups, prefix="editwex", extra_buttons=[("❌ Отмена", "editwex:cancel")], show_all=True
    )
    await ui.safe_edit(
        callback, "Выбери группу мышц или найди упражнение по названию:", reply_markup=kb
    )


@router.callback_query(F.data == "editw:newex")
async def editw_new_exercise_start(callback: CallbackQuery, state: FSMContext):
    workout_id = await _require_edit_workout_id(callback, state)
    if workout_id is None:
        return
    await state.update_data(editwex_group_id=None, editwex_page=0)
    await _editwex_groups_screen(callback, state)
    await callback.answer()


@router.callback_query(
    StateFilter(EditWorkoutFlow.adding_exercise_group, EditWorkoutFlow.adding_exercise_pick),
    F.data == "editwex:cancel",
)
async def editwex_cancel(callback: CallbackQuery, state: FSMContext):
    workout_id = await _require_edit_workout_id(callback, state)
    if workout_id is None:
        return
    await show_edit_screen(callback, state, workout_id)
    await callback.answer()


async def _editwex_exercise_list_screen(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(EditWorkoutFlow.adding_exercise_pick)
    data = await state.get_data()
    group_id = data.get("editwex_group_id")
    page = data.get("editwex_page", 0)
    offset = page * config.RECENT_EXERCISES_LIMIT
    if group_id is None:
        exercises = await db.list_user_exercises(
            callback.from_user.id, limit=config.RECENT_EXERCISES_LIMIT, offset=offset
        )
        total = await db.count_user_exercises(callback.from_user.id)
    else:
        exercises = await db.list_user_exercises_in_group(
            callback.from_user.id, group_id, limit=config.RECENT_EXERCISES_LIMIT, offset=offset
        )
        total = await db.count_user_exercises_in_group(callback.from_user.id, group_id)
    has_next = offset + len(exercises) < total
    kb = keyboards.exercises_keyboard(
        exercises, prefix="editwex", show_new_button=False, back_cb="back", page=page, has_next=has_next,
    )
    text = "Выбери упражнение или напиши название для поиска:" if exercises else "Пусто здесь — напиши название для поиска."
    await ui.safe_edit(callback, text, reply_markup=kb)


@router.callback_query(StateFilter(EditWorkoutFlow.adding_exercise_group), F.data.startswith("editwex:grp:"))
async def editwex_pick_group(callback: CallbackQuery, state: FSMContext):
    raw = callback.data.split(":")[2]
    group_id = None if raw == "all" else int(raw)
    await state.update_data(editwex_group_id=group_id, editwex_page=0)
    await _editwex_exercise_list_screen(callback, state)
    await callback.answer()


@router.callback_query(StateFilter(EditWorkoutFlow.adding_exercise_pick), F.data.startswith("editwex:page:"))
async def editwex_page(callback: CallbackQuery, state: FSMContext):
    page = int(callback.data.split(":")[2])
    await state.update_data(editwex_page=page)
    await _editwex_exercise_list_screen(callback, state)
    await callback.answer()


@router.callback_query(StateFilter(EditWorkoutFlow.adding_exercise_pick), F.data == "editwex:back")
async def editwex_back_to_groups(callback: CallbackQuery, state: FSMContext):
    await _editwex_groups_screen(callback, state)
    await callback.answer()


async def _editwex_finish(event, state: FSMContext, ex_id: int) -> None:
    await state.update_data(add_block_id=None, add_exercise_id=ex_id)
    await state.set_state(EditWorkoutFlow.adding_set)
    ex = await db.get_exercise(ex_id)
    text = (
        f"«{ex['display_name']}» — напиши вес и повторы для первого подхода "
        "(например «100 8», можно «100x8x3»):"
    )
    kb = keyboards.cancel_keyboard("editw:back")
    if isinstance(event, CallbackQuery):
        await ui.safe_edit(event, text, reply_markup=kb)
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb)


@router.callback_query(StateFilter(EditWorkoutFlow.adding_exercise_pick), F.data.startswith("editwex:ex:"))
async def editwex_pick_exercise(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    await _editwex_finish(callback, state, ex_id)


@router.callback_query(
    StateFilter(EditWorkoutFlow.adding_exercise_group, EditWorkoutFlow.adding_exercise_pick),
    F.data.startswith("editwex:tpladd:"),
)
async def editwex_pick_template(callback: CallbackQuery, state: FSMContext):
    template_id = int(callback.data.split(":")[2])
    ex_id = await db.fork_exercise_from_template(callback.from_user.id, template_id)
    await _editwex_finish(callback, state, ex_id)


@router.message(StateFilter(EditWorkoutFlow.adding_exercise_group, EditWorkoutFlow.adding_exercise_pick), F.text)
async def editwex_search_text(message: Message, state: FSMContext):
    query = message.text.strip()
    if not query:
        return
    results = await db.search_exercises(message.from_user.id, query)
    templates = await db.search_exercise_templates(message.from_user.id, query)
    kb = keyboards.exercises_keyboard(results, prefix="editwex", show_new_button=False, templates=templates)
    if results or templates:
        text = f"Результаты поиска «{escape(query)}»:"
    else:
        text = f"Ничего не нашлось по «{escape(query)}»."
    await state.set_state(EditWorkoutFlow.adding_exercise_pick)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "editw:date")
async def editw_date_prompt(callback: CallbackQuery, state: FSMContext):
    await state.set_state(EditWorkoutFlow.awaiting_date)
    today = timeutil.user_today(await db.get_user(callback.from_user.id))
    await ui.safe_edit(
        callback,
        "Выбери новую дату в календаре или напиши в формате дд.мм.гггг:",
        reply_markup=keyboards.calendar_keyboard("editwd", today.year, today.month, today=today),
    )
    await callback.answer()


@router.callback_query(StateFilter(EditWorkoutFlow.awaiting_date), F.data.startswith("editwd:cal:"))
async def editw_date_cal_nav(callback: CallbackQuery, state: FSMContext):
    year, month = (int(x) for x in callback.data.split(":")[2].split("-"))
    today = timeutil.user_today(await db.get_user(callback.from_user.id))
    with suppress(TelegramBadRequest):
        await callback.message.edit_reply_markup(
            reply_markup=keyboards.calendar_keyboard("editwd", year, month, today=today)
        )
    await callback.answer()


@router.callback_query(F.data == "editwd:noop")
async def editw_date_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data == "editwd:cancel")
async def editw_date_cancel(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await show_edit_screen(callback, state, data["edit_workout_id"])
    await callback.answer()


async def _apply_edit_workout_date(workout_id: int, new_date: dt.date) -> None:
    """Move a finished workout to a new calendar day, preserving its start time
    and duration."""
    workout = await db.get_workout(workout_id)
    started = dt.datetime.fromisoformat(workout["started_at"])
    finished = dt.datetime.fromisoformat(workout["finished_at"]) if workout["finished_at"] else started
    delta = finished - started
    new_started = dt.datetime.combine(new_date, started.time())
    new_finished = new_started + delta
    await db.update_workout_date(
        workout_id, new_started.isoformat(timespec="seconds"), new_finished.isoformat(timespec="seconds")
    )
    # Метки подходов едут следом: длительность на карточке считается по их
    # разбегу, и оставшись на старом дне они целиком выпадали за границу
    # «не позже finished_at» — тренировка молча теряла свои «· 32 мин».
    await db.shift_workout_set_timestamps(workout_id, (new_started - started).total_seconds())
    # The date shift changes which prior session counts as "previous" for every
    # exercise in the workout, so a cached AI comment describing the old
    # comparison would go stale.
    await db.set_workout_ai_comment(workout_id, None)
    # Streaks and the "1 января" badge are read off the calendar day, so moving
    # a workout can win or lose either one.
    await achievement_sync.resync(workout["user_id"])


@router.callback_query(StateFilter(EditWorkoutFlow.awaiting_date), F.data.startswith("editwd:date:"))
async def editw_date_calendar_pick(callback: CallbackQuery, state: FSMContext):
    new_date = dt.date.fromisoformat(callback.data.split(":", 2)[2])
    data = await state.get_data()
    await _apply_edit_workout_date(data["edit_workout_id"], new_date)
    await show_edit_screen(callback, state, data["edit_workout_id"])
    await callback.answer("Дата обновлена")


@router.message(StateFilter(EditWorkoutFlow.awaiting_date), F.text)
async def editw_date_entered(message: Message, state: FSMContext):
    try:
        new_date = parse_ru_date(
            message.text, today=timeutil.user_today(await db.get_user(message.from_user.id))
        )
    except ParseError as e:
        await ui.reply_transient(message, e.message)
        return
    data = await state.get_data()
    workout_id = data["edit_workout_id"]
    await _apply_edit_workout_date(workout_id, new_date)
    await message.reply("Дата обновлена.")
    await show_edit_screen(message, state, workout_id)


@router.callback_query(F.data == "editw:done")
async def editw_done(callback: CallbackQuery, state: FSMContext):
    workout_id = await _require_edit_workout_id(callback, state)
    if workout_id is None:
        return
    await db.delete_empty_blocks(workout_id)
    await state.set_state(None)
    from handlers.history import show_history_item
    await show_history_item(callback, workout_id)
    await callback.answer()
