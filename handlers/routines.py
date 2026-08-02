"""🗂 Программы — saved workout templates (splits).

A routine is an ordered list of exercises. Starting a workout from a routine
fills the FSM's `planned_blocks` so the existing "▶️ Следующее по шаблону"
flow (handlers/workout.py) walks the user through it one exercise at a time.
Routines can be created from any past finished workout — do the session
once, then save it as your split.
"""

import datetime as dt
from html import escape

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import config
import db
import formatting
import keyboards
import ui
import view_builder
from fsm import RoutineFlow, WorkoutFlow
from seed_data import PROGRAM_BY_KEY, WORKOUT_PROGRAMS

router = Router(name="routines")

ROUTINE_SOURCE_PAGE_SIZE = 8


async def show_manage(event, state: FSMContext) -> None:
    user_id = event.from_user.id
    routines = await db.list_routines(user_id)
    has_workouts = await db.count_workouts(user_id) > 0
    if routines:
        text = "🗂 <b>ПРОГРАММЫ</b>\n\nВыбери программу или создай новую из тренировки."
    else:
        text = (
            "🗂 <b>ПРОГРАММЫ</b>\n\nУ тебя пока нет сохранённых программ.\n"
            "Выбери готовую программу ниже или проведи тренировку и сохрани её как "
            "программу — потом начнёшь такую же в один тап."
        )
    kb = keyboards.routines_manage_keyboard(routines, has_workouts=has_workouts)
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
    # Name, pitch and day count decide whether the program is for you; the
    # exercise-by-exercise breakdown is 20+ lines of detail, so it folds away
    # and the catalog stays scannable.
    day_blocks = [
        "\n".join([
            f"<b>{escape(day_name)}</b>",
            *(f"• {escape(ex)} — {escape(target)}" for ex, target in exercises),
        ])
        for day_name, exercises in days
    ]
    text = "\n\n".join([
        f"✨ <b>{escape(program['name'])}</b>\n<i>{escape(program['meta'])}</i>",
        escape(program["description"]),
        f"<b>{len(days)} {_days_word(len(days))}:</b>\n"
        + formatting.collapsible_if_long("\n\n".join(day_blocks)),
    ])
    kb = keyboards.program_detail_keyboard(key)
    await ui.safe_edit(callback, text, reply_markup=kb, parse_mode="HTML")
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
        for i, ex in enumerate(exercises, start=1):
            suffix = f" — {escape(ex['target'])}" if ex["target"] else ""
            lines.append(f"{i}. {escape(ex['display_name'])}{suffix}")
    else:
        lines.append("В программе нет упражнений (возможно, они были архивированы).")
    kb = keyboards.routine_detail_keyboard(routine_id)
    text = "\n".join(lines)
    if isinstance(event, CallbackQuery):
        await ui.safe_edit(event, text, reply_markup=kb, parse_mode="HTML")
    else:
        await event.answer(text, reply_markup=kb, parse_mode="HTML")


async def _show_routine_editor(event, state: FSMContext, routine_id: int) -> None:
    routine = await _owned_routine(event, routine_id)
    if routine is None:
        return
    exercises = await db.list_routine_exercises(routine_id)
    lines = [f"✏️ <b>{escape(routine['name'])}</b>", ""]
    if exercises:
        lines.append("Нажми на упражнение, чтобы убрать его из программы.")
    else:
        lines.append("В программе нет упражнений.")
    kb = keyboards.routine_edit_keyboard(
        routine_id, [(ex["id"], ex["display_name"]) for ex in exercises]
    )
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


@router.callback_query(F.data.startswith("rt:edit:"))
async def rt_edit(callback: CallbackQuery, state: FSMContext):
    routine_id = int(callback.data.split(":")[2])
    await _show_routine_editor(callback, state, routine_id)
    await callback.answer()


# Button text has a hard Telegram limit (64 chars) and gets cramped well before
# that, so the exercise summary is cut short rather than listing everything.
_SOURCE_PICKER_SUMMARY_MAX = 30


async def _workout_exercise_names(workout_id: int) -> list[str]:
    """Exercise names for a workout, in block order and de-duplicated — same
    source list create_routine_from_workout snapshots into the routine."""
    seen: set[int] = set()
    names: list[str] = []
    for block in await db.list_blocks_for_workout(workout_id):
        for be in await db.get_block_exercises(block["id"]):
            if be["exercise_id"] in seen:
                continue
            seen.add(be["exercise_id"])
            names.append(be["display_name"])
    return names


async def _workout_exercise_summary(workout_id: int) -> str:
    """Comma-joined, truncated version of _workout_exercise_names for a button label."""
    summary = ", ".join(await _workout_exercise_names(workout_id))
    if len(summary) > _SOURCE_PICKER_SUMMARY_MAX:
        summary = summary[:_SOURCE_PICKER_SUMMARY_MAX].rstrip() + "…"
    return summary


async def _show_routine_source_picker(callback: CallbackQuery, state: FSMContext, page: int) -> None:
    user_id = callback.from_user.id
    total = await db.count_workouts(user_id)
    workouts = await db.list_workouts(user_id, limit=ROUTINE_SOURCE_PAGE_SIZE, offset=page * ROUTINE_SOURCE_PAGE_SIZE)
    items = []
    for w in workouts:
        date_label = formatting.format_date_ru(dt.datetime.fromisoformat(w["started_at"]))
        summary = await _workout_exercise_summary(w["id"])
        label = f"{date_label} — {summary}" if summary else date_label
        items.append({"id": w["id"], "label": label})
    has_next = (page + 1) * ROUTINE_SOURCE_PAGE_SIZE < total
    kb = keyboards.routine_source_picker_keyboard(items, page, has_next)
    text = "Из какой тренировки создать программу?" if items else "Нет завершённых тренировок."
    await state.update_data(routine_source_page=page)
    await ui.safe_edit(callback, text, reply_markup=kb)


@router.callback_query(F.data.startswith("rt:pickw:page:"))
async def rt_pickw_page(callback: CallbackQuery, state: FSMContext):
    page = int(callback.data.split(":")[3])
    await _show_routine_source_picker(callback, state, page)
    await callback.answer()


async def _show_routine_source_preview(callback: CallbackQuery, workout_id: int) -> None:
    """Full exercise list of the tapped workout, with a confirm/back choice — the
    picker's button label is truncated, so this is where the user actually sees
    what they're about to base a program on."""
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != callback.from_user.id:
        await callback.answer("Тренировка не найдена", show_alert=True)
        return
    date_label = formatting.format_date_ru(dt.datetime.fromisoformat(workout["started_at"]))
    blocks = await view_builder.build_block_views(workout_id)
    lines = [f"📋 <b>{escape(date_label)}</b>", ""]
    if blocks:
        for i, b in enumerate(blocks, start=1):
            sets_str = ", ".join(
                formatting.format_set(w, r, rpe)
                for (w, r), rpe in zip(b.sets, b.set_rpes or [None] * len(b.sets), strict=True)
            )
            lines.append(f"{i}. <b>{escape(b.exercise_name)}</b>")
            if sets_str:
                lines.append(f"   {sets_str}")
    else:
        lines.append("В тренировке нет упражнений.")
    lines.append("")
    lines.append("Создать программу из этой тренировки?")
    kb = keyboards.routine_source_preview_keyboard(workout_id)
    await ui.safe_edit(callback, "\n".join(lines), reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("rt:pickw:item:"))
async def rt_pickw_item(callback: CallbackQuery, state: FSMContext):
    workout_id = int(callback.data.split(":")[3])
    await _show_routine_source_preview(callback, workout_id)
    await callback.answer()


@router.callback_query(F.data == "rt:pickw:back")
async def rt_pickw_back(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await _show_routine_source_picker(callback, state, data.get("routine_source_page", 0))
    await callback.answer()


@router.callback_query(F.data.startswith("rt:pickw:use:"))
async def rt_pickw_use(callback: CallbackQuery, state: FSMContext):
    workout_id = int(callback.data.split(":")[3])
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != callback.from_user.id:
        await callback.answer("Тренировка не найдена", show_alert=True)
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
    routine_id = int(callback.data.split(":")[2])
    routine = await _owned_routine(callback, routine_id)
    if routine is None:
        return

    active = await db.get_active_workout(callback.from_user.id)
    if active:
        if await db.list_exercise_ids_for_workout(active["id"]):
            # Dropping into the old session here would lose the program the user
            # just chose, and tapping "▶️ Начать" again would do the same thing —
            # a loop with no way forward. Offer the two real choices instead.
            started = dt.datetime.fromisoformat(active["started_at"])
            kb = keyboards.yes_no_keyboard(
                yes_cb=f"rt:finishprev:{routine_id}",
                no_cb=f"rt:resumeprev:{routine_id}",
                yes_text="🏁 Завершить и начать",
                no_text="↩️ Вернуться к ней",
            )
            await ui.safe_edit(
                callback,
                f"У тебя не закрыта тренировка от <b>{formatting.format_date_ru(started)}</b>.\n"
                f"Завершить её и начать по программе «{escape(routine['name'])}»?",
                reply_markup=kb,
                parse_mode="HTML",
            )
            await callback.answer()
            return
        await db.discard_workout(active["id"])

    await _begin_routine_workout(callback, state, routine)
    await callback.answer()


async def _begin_routine_workout(callback: CallbackQuery, state: FSMContext, routine) -> None:
    """Create the workout and load the routine's first block. Assumes any
    previously active workout has already been dealt with."""
    from handlers.workout import _delete_message as wk_delete
    from handlers.workout import _load_next_planned_block, _picker_screen_groups

    exercises = await db.list_routine_exercises(routine["id"])
    planned = [
        {"exercise_ids": [ex["exercise_id"]], "targets": {ex["exercise_id"]: ex["target"]}}
        for ex in exercises
    ]

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


@router.callback_query(F.data.startswith("rt:finishprev:"))
async def rt_finish_previous_and_start(callback: CallbackQuery, state: FSMContext):
    """"🏁 Завершить и начать" — close the stale session (keeping what's in it)
    and start the chosen program straight away."""
    routine_id = int(callback.data.split(":")[2])
    routine = await _owned_routine(callback, routine_id)
    if routine is None:
        return
    active = await db.get_active_workout(callback.from_user.id)
    if active:
        await db.delete_empty_blocks(active["id"])
        await db.finish_workout(active["id"])
    await _begin_routine_workout(callback, state, routine)
    await callback.answer("Прошлая тренировка завершена")


@router.callback_query(F.data.startswith("rt:resumeprev:"))
async def rt_resume_previous(callback: CallbackQuery, state: FSMContext):
    """"↩️ Вернуться к ней" — the other half of the same choice."""
    from handlers.workout import _enter_live

    active = await db.get_active_workout(callback.from_user.id)
    if active is None:
        await callback.answer("Активной тренировки уже нет")
        await _show_routine_detail(callback, state, int(callback.data.split(":")[2]))
        return
    await _enter_live(callback, state, active["id"])
    await callback.answer()


# ---------- editing a saved routine's exercise list ----------
#
# Adding reuses the same shape as the live-workout picker (groups → exercises,
# with search and template forking) — a different prefix ("rtadd") targeting
# db.append_routine_exercise instead of opening a live block. Removing is a
# single tap with no confirmation: unlike deleting the whole program, dropping
# one exercise is trivially undone with "➕ Добавить упражнение".

@router.callback_query(F.data.startswith("rt:rmex:"))
async def rt_remove_exercise(callback: CallbackQuery, state: FSMContext):
    _, _, routine_id_s, re_id_s = callback.data.split(":")
    routine_id, re_id = int(routine_id_s), int(re_id_s)
    routine = await _owned_routine(callback, routine_id)
    if routine is None:
        return
    entry = await db.get_routine_exercise(re_id)
    if entry is None or entry["routine_id"] != routine_id:
        await callback.answer("Упражнение уже убрано", show_alert=True)
        await _show_routine_editor(callback, state, routine_id)
        return
    await db.remove_routine_exercise(re_id)
    await callback.answer("Убрал из программы")
    await _show_routine_editor(callback, state, routine_id)


async def _rtadd_groups_screen(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(RoutineFlow.adding_exercise_group)
    groups = await db.list_muscle_groups(callback.from_user.id)
    kb = keyboards.groups_keyboard(
        groups, prefix="rtadd", extra_buttons=[("❌ Отмена", "rtadd:cancel")], show_all=True
    )
    await ui.safe_edit(
        callback, "Выбери группу мышц или найди упражнение по названию:", reply_markup=kb
    )


@router.callback_query(F.data.startswith("rt:addex:"))
async def rt_add_exercise_start(callback: CallbackQuery, state: FSMContext):
    routine_id = int(callback.data.split(":")[2])
    if await _owned_routine(callback, routine_id) is None:
        return
    await state.update_data(rtadd_routine_id=routine_id, rtadd_group_id=None, rtadd_page=0)
    await _rtadd_groups_screen(callback, state)
    await callback.answer()


@router.callback_query(
    StateFilter(RoutineFlow.adding_exercise_group, RoutineFlow.adding_exercise_pick),
    F.data == "rtadd:cancel",
)
async def rtadd_cancel(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    routine_id = data.get("rtadd_routine_id")
    await state.set_state(None)
    if routine_id is not None:
        await _show_routine_editor(callback, state, routine_id)
    await callback.answer()


async def _rtadd_exercise_list_screen(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(RoutineFlow.adding_exercise_pick)
    data = await state.get_data()
    group_id = data.get("rtadd_group_id")
    page = data.get("rtadd_page", 0)
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
        exercises, prefix="rtadd", show_new_button=False, back_cb="back", page=page, has_next=has_next,
    )
    text = "Выбери упражнение или напиши название для поиска:" if exercises else "Пусто здесь — напиши название для поиска."
    await ui.safe_edit(callback, text, reply_markup=kb)


@router.callback_query(StateFilter(RoutineFlow.adding_exercise_group), F.data.startswith("rtadd:grp:"))
async def rtadd_pick_group(callback: CallbackQuery, state: FSMContext):
    raw = callback.data.split(":")[2]
    group_id = None if raw == "all" else int(raw)
    await state.update_data(rtadd_group_id=group_id, rtadd_page=0)
    await _rtadd_exercise_list_screen(callback, state)
    await callback.answer()


@router.callback_query(StateFilter(RoutineFlow.adding_exercise_pick), F.data.startswith("rtadd:page:"))
async def rtadd_page(callback: CallbackQuery, state: FSMContext):
    page = int(callback.data.split(":")[2])
    await state.update_data(rtadd_page=page)
    await _rtadd_exercise_list_screen(callback, state)
    await callback.answer()


@router.callback_query(StateFilter(RoutineFlow.adding_exercise_pick), F.data == "rtadd:back")
async def rtadd_back_to_groups(callback: CallbackQuery, state: FSMContext):
    await _rtadd_groups_screen(callback, state)
    await callback.answer()


async def _rtadd_finish(event, state: FSMContext, exercise_id: int) -> None:
    data = await state.get_data()
    routine_id = data["rtadd_routine_id"]
    await db.append_routine_exercise(routine_id, exercise_id)
    await db.touch_exercise_last_used(exercise_id)
    await state.set_state(None)
    # Back to the composition editor the "➕" was tapped from, so several
    # exercises can be added in a row.
    await _show_routine_editor(event, state, routine_id)
    if isinstance(event, CallbackQuery):
        await event.answer("Добавил в программу")


@router.callback_query(StateFilter(RoutineFlow.adding_exercise_pick), F.data.startswith("rtadd:ex:"))
async def rtadd_pick_exercise(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    await _rtadd_finish(callback, state, ex_id)


@router.callback_query(
    StateFilter(RoutineFlow.adding_exercise_group, RoutineFlow.adding_exercise_pick),
    F.data.startswith("rtadd:tpladd:"),
)
async def rtadd_pick_template(callback: CallbackQuery, state: FSMContext):
    template_id = int(callback.data.split(":")[2])
    ex_id = await db.fork_exercise_from_template(callback.from_user.id, template_id)
    await _rtadd_finish(callback, state, ex_id)


@router.message(StateFilter(RoutineFlow.adding_exercise_group, RoutineFlow.adding_exercise_pick))
async def rtadd_search_text(message: Message, state: FSMContext):
    """Typing while picking what to add searches — own exercises plus catalog
    templates (forked on tap), same merge as the live-workout picker."""
    query = message.text.strip()
    if not query:
        return
    results = await db.search_exercises(message.from_user.id, query)
    templates = await db.search_exercise_templates(message.from_user.id, query)
    kb = keyboards.exercises_keyboard(results, prefix="rtadd", show_new_button=False, templates=templates)
    if results or templates:
        text = f"Результаты поиска «{escape(query)}»:"
    else:
        text = f"Ничего не нашлось по «{escape(query)}»."
    await state.set_state(RoutineFlow.adding_exercise_pick)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")
