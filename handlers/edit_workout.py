"""§A2 — edit a past (finished) workout: add/remove/edit sets, change date."""

import datetime as dt

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

import db
import formatting
import keyboards
from fsm import EditWorkoutFlow
from parser import ParseError, parse_ru_date, parse_single_token

router = Router(name="edit_workout")


async def _delete_message(message: Message):
    try:
        await message.delete()
    except TelegramBadRequest:
        pass


async def _edit_screen_payload(workout_id: int) -> tuple[str, InlineKeyboardMarkup]:
    workout = await db.get_workout(workout_id)
    started = dt.datetime.fromisoformat(workout["started_at"])

    sets_rows: list[tuple[int, str]] = []
    add_buttons: list[tuple[int, int, str]] = []
    blocks = await db.list_blocks_for_workout(workout_id)
    for block in blocks:
        block_exs = await db.get_block_exercises(block["id"])
        name_by_ex = {be["exercise_id"]: be["display_name"] for be in block_exs}
        sets = await db.list_sets_for_block(block["id"])
        for s in sets:
            ex_name = name_by_ex.get(s["exercise_id"], "?")
            label = f"{ex_name} · {formatting.format_set(s['weight'], s['reps'], bool(s['is_warmup']))}"
            sets_rows.append((s["id"], label))
        for be in block_exs:
            add_buttons.append((block["id"], be["exercise_id"], be["display_name"]))

    text = f"✏️ Редактирование · {formatting.format_date_ru(started)}\nНажми на сет, чтобы изменить или удалить."
    if not sets_rows:
        text += "\n\nВ тренировке пока нет сетов."
    kb = keyboards.edit_workout_keyboard(sets_rows, add_buttons)
    return text, kb


async def show_edit_screen(event, state: FSMContext, workout_id: int) -> None:
    await state.set_state(EditWorkoutFlow.viewing)
    await state.update_data(edit_workout_id=workout_id)
    text, kb = await _edit_screen_payload(workout_id)
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=kb)
    else:
        await event.answer(text, reply_markup=kb)


@router.callback_query(StateFilter(EditWorkoutFlow.viewing), F.data.startswith("editw:set:"))
async def editw_pick_set(callback: CallbackQuery, state: FSMContext):
    set_id = int(callback.data.split(":")[2])
    row = await db.get_set(set_id)
    ex = await db.get_exercise(row["exercise_id"])
    text = f"{ex['display_name']}: {formatting.format_set(row['weight'], row['reps'], bool(row['is_warmup']))}"
    await callback.message.edit_text(text, reply_markup=keyboards.set_actions_keyboard(set_id))
    await callback.answer()


@router.callback_query(F.data == "editw:back")
async def editw_back(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await show_edit_screen(callback, state, data["edit_workout_id"])
    await callback.answer()


@router.callback_query(F.data.startswith("editw:delset:"))
async def editw_delset(callback: CallbackQuery, state: FSMContext):
    set_id = int(callback.data.split(":")[2])
    await db.delete_set(set_id)
    data = await state.get_data()
    await callback.answer("Сет удалён")
    await show_edit_screen(callback, state, data["edit_workout_id"])


@router.callback_query(F.data.startswith("editw:editset:"))
async def editw_editset_prompt(callback: CallbackQuery, state: FSMContext):
    set_id = int(callback.data.split(":")[2])
    await state.update_data(edit_set_id=set_id)
    await state.set_state(EditWorkoutFlow.editing_set)
    row = await db.get_set(set_id)
    await callback.message.edit_text(
        f"Текущее значение: {formatting.format_set(row['weight'], row['reps'])}\n"
        "Напиши новый вес и повторы (например «100 8»):",
        reply_markup=keyboards.cancel_keyboard("editw:back"),
    )
    await callback.answer()


@router.message(StateFilter(EditWorkoutFlow.editing_set))
async def editw_editset_entered(message: Message, state: FSMContext):
    try:
        parsed = parse_single_token(message.text)
    except ParseError as e:
        await message.reply(e.message)
        return
    data = await state.get_data()
    await db.update_set(data["edit_set_id"], parsed[0].weight, parsed[0].reps)
    await message.reply("Готово.")
    await _delete_message(message)
    await show_edit_screen(message, state, data["edit_workout_id"])


@router.callback_query(F.data.startswith("editw:addset:"))
async def editw_addset_prompt(callback: CallbackQuery, state: FSMContext):
    _, _, block_id_str, ex_id_str = callback.data.split(":")
    await state.update_data(add_block_id=int(block_id_str), add_exercise_id=int(ex_id_str))
    await state.set_state(EditWorkoutFlow.adding_set)
    ex = await db.get_exercise(int(ex_id_str))
    await callback.message.edit_text(
        f"Новый сет для «{ex['display_name']}» — напиши вес и повторы (например «100 8», можно «100x8x3»):",
        reply_markup=keyboards.cancel_keyboard("editw:back"),
    )
    await callback.answer()


@router.message(StateFilter(EditWorkoutFlow.adding_set))
async def editw_addset_entered(message: Message, state: FSMContext):
    try:
        parsed = parse_single_token(message.text)
    except ParseError as e:
        await message.reply(e.message)
        return
    data = await state.get_data()
    block_id, ex_id = data["add_block_id"], data["add_exercise_id"]
    block_exs = await db.get_block_exercises(block_id)
    order_in_round = next((be["order_in_block"] for be in block_exs if be["exercise_id"] == ex_id), 0)
    for ps in parsed:
        round_idx = await db.next_round_index(block_id, ex_id)
        await db.add_set(block_id, ex_id, round_idx, order_in_round, ps.weight, ps.reps, ps.is_warmup)
    await message.reply("Сет добавлен.")
    await _delete_message(message)
    await show_edit_screen(message, state, data["edit_workout_id"])


@router.callback_query(F.data == "editw:date")
async def editw_date_prompt(callback: CallbackQuery, state: FSMContext):
    await state.set_state(EditWorkoutFlow.awaiting_date)
    await callback.message.edit_text(
        "Напиши новую дату в формате дд.мм.гггг:", reply_markup=keyboards.cancel_keyboard("editw:back")
    )
    await callback.answer()


@router.message(StateFilter(EditWorkoutFlow.awaiting_date))
async def editw_date_entered(message: Message, state: FSMContext):
    try:
        new_date = parse_ru_date(message.text)
    except ParseError as e:
        await message.reply(e.message)
        return
    data = await state.get_data()
    workout_id = data["edit_workout_id"]
    workout = await db.get_workout(workout_id)
    started = dt.datetime.fromisoformat(workout["started_at"])
    finished = dt.datetime.fromisoformat(workout["finished_at"]) if workout["finished_at"] else started
    delta = finished - started
    new_started = dt.datetime.combine(new_date, started.time())
    new_finished = new_started + delta
    await db.update_workout_date(
        workout_id, new_started.isoformat(timespec="seconds"), new_finished.isoformat(timespec="seconds")
    )
    await message.reply("Дата обновлена.")
    await show_edit_screen(message, state, workout_id)


@router.callback_query(F.data == "editw:done")
async def editw_done(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    workout_id = data["edit_workout_id"]
    await state.set_state(None)
    from handlers.history import show_history_item
    await show_history_item(callback, workout_id)
    await callback.answer()
