"""🗂 Программы — saved workout templates (splits).

A routine is an ordered list of exercises. Starting a workout from a routine
fills the FSM's `planned_blocks` so the existing "▶️ Следующее по шаблону"
flow (handlers/workout.py) walks the user through it one exercise at a time.
Routines are created from the user's most recent finished workout — do the
session once, then save it as your split.
"""

from html import escape

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import db
import keyboards
import ui
from fsm import RoutineFlow, WorkoutFlow
from seed_data import PROGRAM_BY_KEY, WORKOUT_PROGRAMS

router = Router(name="routines")


async def _last_finished_workout_id(user_id: int) -> int | None:
    workouts = await db.list_workouts(user_id, limit=1)
    return workouts[0]["id"] if workouts else None


async def show_manage(event, state: FSMContext) -> None:
    user_id = event.from_user.id
    routines = await db.list_routines(user_id)
    has_last = await _last_finished_workout_id(user_id) is not None
    if routines:
        text = "🗂 <b>ПРОГРАММЫ</b>\n\nВыбери программу или создай новую из последней тренировки."
    else:
        text = (
            "🗂 <b>ПРОГРАММЫ</b>\n\nУ тебя пока нет сохранённых программ.\n"
            "Проведи тренировку и сохрани её как программу — потом начнёшь такую же в один тап."
        )
    kb = keyboards.routines_manage_keyboard(routines, has_last_workout=has_last)
    if isinstance(event, CallbackQuery):
        await ui.safe_edit(event, text, reply_markup=kb, parse_mode="HTML")
    else:
        await event.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "rt:manage")
async def rt_manage(callback: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await show_manage(callback, state)
    await callback.answer()


@router.callback_query(F.data == "rt:menu")
async def rt_menu(callback: CallbackQuery, state: FSMContext):
    from handlers.workout import _show_main_menu
    await _show_main_menu(callback, state)
    await callback.answer()


# ---------- ready-made programs ----------

@router.callback_query(F.data == "rt:programs")
async def rt_programs(callback: CallbackQuery, state: FSMContext):
    text = (
        "✨ <b>ГОТОВЫЕ ПРОГРАММЫ</b>\n\n"
        "Выбери готовую программу — её дни добавятся тебе в «Программы», и ты "
        "начнёшь тренировку в один тап. Все нужные упражнения появятся в твоём списке."
    )
    kb = keyboards.programs_catalog_keyboard(WORKOUT_PROGRAMS)
    await ui.safe_edit(callback, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("rt:prog:"))
async def rt_program_detail(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 2)[2]
    program = PROGRAM_BY_KEY.get(key)
    if program is None:
        await callback.answer("Программа не найдена", show_alert=True)
        return
    days = program["days"]
    lines = [
        f"✨ <b>{escape(program['name'])}</b>",
        f"<i>{escape(program['meta'])}</i>",
        "",
        escape(program["description"]),
        "",
        f"<b>{len(days)} {_days_word(len(days))}:</b>",
        "",
    ]
    for day_name, exercises in days:
        lines.append(f"<b>{escape(day_name)}</b>")
        lines.extend(f"• {escape(ex)}" for ex in exercises)
        lines.append("")
    kb = keyboards.program_detail_keyboard(key)
    await ui.safe_edit(callback, "\n".join(lines).strip(), reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("rt:progadd:"))
async def rt_program_add(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 2)[2]
    program = PROGRAM_BY_KEY.get(key)
    if program is None:
        await callback.answer("Программа не найдена", show_alert=True)
        return
    # Create days in reverse so day 1 ends up newest and thus tops the routines
    # list (list_routines orders by created_at/id DESC).
    for day_name, exercises in reversed(program["days"]):
        await db.create_routine_from_program(callback.from_user.id, day_name, exercises)
    await callback.answer(f"Программа добавлена: {len(program['days'])} дн.")
    await show_manage(callback, state)


def _days_word(n: int) -> str:
    """Russian plural for «день» (1 день, 2 дня, 5 дней)."""
    if 11 <= n % 100 <= 14:
        return "дней"
    last = n % 10
    if last == 1:
        return "день"
    if 2 <= last <= 4:
        return "дня"
    return "дней"


async def _owned_routine(event, routine_id: int):
    routine = await db.get_routine(routine_id)
    if routine is None or routine["user_id"] != event.from_user.id:
        if isinstance(event, CallbackQuery):
            await event.answer("Программа не найдена", show_alert=True)
        return None
    return routine


async def _show_routine_detail(event, state: FSMContext, routine_id: int) -> None:
    routine = await _owned_routine(event, routine_id)
    if routine is None:
        return
    exercises = await db.list_routine_exercises(routine_id)
    lines = [f"🗂 <b>{escape(routine['name'])}</b>", ""]
    if exercises:
        lines.extend(f"{i}. {escape(ex['display_name'])}" for i, ex in enumerate(exercises, start=1))
    else:
        lines.append("В программе нет упражнений (возможно, они были архивированы).")
    kb = keyboards.routine_detail_keyboard(routine_id)
    text = "\n".join(lines)
    if isinstance(event, CallbackQuery):
        await ui.safe_edit(event, text, reply_markup=kb, parse_mode="HTML")
    else:
        await event.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("rt:view:"))
async def rt_view(callback: CallbackQuery, state: FSMContext):
    routine_id = int(callback.data.split(":")[2])
    await _show_routine_detail(callback, state, routine_id)
    await callback.answer()


@router.callback_query(F.data == "rt:createlast")
async def rt_create_from_last(callback: CallbackQuery, state: FSMContext):
    workout_id = await _last_finished_workout_id(callback.from_user.id)
    if workout_id is None:
        await callback.answer("Нет завершённых тренировок", show_alert=True)
        return
    await state.set_state(RoutineFlow.naming)
    await state.update_data(routine_source_workout_id=workout_id)
    await ui.safe_edit(
        callback,
        "Как назвать программу? (например «День груди» или «Тяни»)",
        reply_markup=keyboards.cancel_keyboard("rt:manage"),
    )
    await callback.answer()


@router.message(StateFilter(RoutineFlow.naming))
async def rt_name_entered(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.reply("Название не может быть пустым")
        return
    data = await state.get_data()
    workout_id = data["routine_source_workout_id"]
    routine_id = await db.create_routine_from_workout(message.from_user.id, workout_id, name)
    await state.set_state(None)
    await _show_routine_detail(message, state, routine_id)


@router.callback_query(F.data.startswith("rt:rename:"))
async def rt_rename(callback: CallbackQuery, state: FSMContext):
    routine_id = int(callback.data.split(":")[2])
    if await _owned_routine(callback, routine_id) is None:
        return
    await state.set_state(RoutineFlow.renaming)
    await state.update_data(routine_rename_id=routine_id)
    await ui.safe_edit(
        callback, "Напиши новое название программы:", reply_markup=keyboards.cancel_keyboard(f"rt:view:{routine_id}")
    )
    await callback.answer()


@router.message(StateFilter(RoutineFlow.renaming))
async def rt_rename_entered(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.reply("Название не может быть пустым")
        return
    data = await state.get_data()
    routine_id = data["routine_rename_id"]
    await db.rename_routine(routine_id, name)
    await state.set_state(None)
    await _show_routine_detail(message, state, routine_id)


@router.callback_query(F.data.startswith("rt:delask:"))
async def rt_delete_confirm(callback: CallbackQuery, state: FSMContext):
    routine_id = int(callback.data.split(":")[2])
    if await _owned_routine(callback, routine_id) is None:
        return
    kb = keyboards.yes_no_keyboard(
        yes_cb=f"rt:delyes:{routine_id}", no_cb=f"rt:view:{routine_id}",
        yes_text="🗑 Удалить", no_text="❌ Отмена",
    )
    await ui.safe_edit(callback, "Удалить программу? История тренировок не пострадает.", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("rt:delyes:"))
async def rt_delete(callback: CallbackQuery, state: FSMContext):
    routine_id = int(callback.data.split(":")[2])
    if await _owned_routine(callback, routine_id) is None:
        return
    await db.delete_routine(routine_id)
    await callback.answer("Программа удалена")
    await show_manage(callback, state)


@router.callback_query(F.data.startswith("rt:start:"))
async def rt_start(callback: CallbackQuery, state: FSMContext):
    from handlers.workout import _delete_message as wk_delete
    from handlers.workout import _enter_live, _load_next_planned_block, _picker_screen_groups

    routine_id = int(callback.data.split(":")[2])
    routine = await _owned_routine(callback, routine_id)
    if routine is None:
        return

    active = await db.get_active_workout(callback.from_user.id)
    if active:
        await callback.answer("У тебя уже есть активная тренировка", show_alert=True)
        await _enter_live(callback, state, active["id"])
        return

    exercises = await db.list_routine_exercises(routine_id)
    planned = [{"exercise_ids": [ex["exercise_id"]]} for ex in exercises]

    workout_id = await db.create_workout(callback.from_user.id)
    await wk_delete(callback.message)
    sent = await callback.message.answer(f"🏋️ Тренировка по программе «{routine['name']}»")
    await state.update_data(
        workout_id=workout_id, live_chat_id=sent.chat.id, live_message_id=sent.message_id,
        last_by_exercise={}, planned_blocks=planned,
    )
    if planned:
        await _load_next_planned_block(callback, state)
    else:
        await state.set_state(WorkoutFlow.picking_group)
        await _picker_screen_groups(callback, state)
    await callback.answer()
