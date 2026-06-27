"""Workout lifecycle: start, add exercises, switch between them, log sets, finish."""

import datetime as dt
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReactionTypeEmoji

import analytics
import config
import db
import formatting
import keyboards
import ui
import view_builder
from fsm import WorkoutFlow
from parser import ParseError, parse_single_token

router = Router(name="workout")


# ---------- helpers ----------

async def _ensure_user(telegram_id: int, username: str | None):
    return await db.get_or_create_user(telegram_id, username)


def _move_open_exercises_last(
    blocks: list[formatting.BlockView], open_exercises: list[int], active_id: int | None
) -> list[formatting.BlockView]:
    """Push still-open exercises to the bottom, active one last, closest to the input hint."""
    open_set = set(open_exercises)
    closed = [
        b for b in blocks
        if not (isinstance(b, formatting.ExerciseBlockView) and b.exercise_id in open_set)
    ]
    open_map = {
        b.exercise_id: b for b in blocks
        if isinstance(b, formatting.ExerciseBlockView) and b.exercise_id in open_set
    }
    order = [eid for eid in open_exercises if eid != active_id]
    if active_id in open_map:
        order.append(active_id)
    return closed + [open_map[eid] for eid in order if eid in open_map]


async def _refresh_live(bot, state: FSMContext, user, workout_id: int, hint, keyboard):
    """Re-send the live tracker message so it always sits at the bottom of the chat.

    Telegram doesn't let a bot move an edited message down past newer messages
    (e.g. the weight/reps the user just typed), so we delete and resend instead
    of editing in place.
    """
    data = await state.get_data()
    chat_id = data["live_chat_id"]
    blocks = await view_builder.build_block_views(workout_id, user["e1rm_formula"], skip_empty=False)
    active = data.get("active_exercise_id")
    blocks = _move_open_exercises_last(blocks, data.get("open_exercises") or [], active)
    text = formatting.build_live_session_text(
        blocks, hint, hide_warmups=bool(user["hide_warmups"]), active_exercise_id=active,
    )
    if data.get("is_backfill") and data.get("bf_date"):
        date = dt.date.fromisoformat(data["bf_date"])
        text = f"📅 {formatting.format_date_ru(date)}\n\n{text}"
    try:
        await bot.delete_message(chat_id=chat_id, message_id=data["live_message_id"])
    except TelegramBadRequest:
        pass
    sent = await bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard, parse_mode="HTML")
    await state.update_data(live_message_id=sent.message_id)


def _idle_keyboard(data: dict | None = None):
    has_planned = bool((data or {}).get("planned_blocks"))
    return keyboards.exercise_picker_entry_keyboard(has_planned=has_planned)


async def _delete_message(message: Message):
    try:
        await message.delete()
    except TelegramBadRequest:
        pass


async def _react_ok(bot, message: Message):
    try:
        await bot.set_message_reaction(
            chat_id=message.chat.id, message_id=message.message_id,
            reaction=[ReactionTypeEmoji(emoji="✅")],
        )
    except TelegramBadRequest:
        pass


async def _log_one(block_id: int, exercise_id: int, weight: float, reps: int, is_warmup: bool):
    round_idx = await db.next_round_index(block_id, exercise_id)
    await db.add_set(block_id, exercise_id, round_idx, 0, weight, reps, is_warmup)


def _logging_hint(last_session: list[tuple[float, int]] | None, has_sets: bool) -> str:
    base = "Напиши вес и повторы через пробел, например «100 8»"
    if has_sets:
        base += " (можно только повторы — вес возьмётся с прошлого подхода)"
    if last_session:
        sets_str = ", ".join(formatting.format_set(w, r) for w, r in last_session)
        return f"В прошлый раз: {sets_str}\n{base}"
    return base


async def _last_session_sets(ex_id: int) -> list[tuple[float, int]]:
    """Working sets from this exercise's most recent finished workout, for the "last time" hint."""
    rows = await db.list_sets_for_exercise(ex_id)
    if not rows:
        return []
    last_workout_id = rows[-1]["workout_id"]
    return [(r["weight"], r["reps"]) for r in rows if r["workout_id"] == last_workout_id]


async def _render_logging_screen(bot, state: FSMContext, user):
    data = await state.get_data()
    open_ids: list[int] = data.get("open_exercises") or []
    active = data.get("active_exercise_id")
    last_session_sets = data.get("last_session_sets") or {}

    names: dict[int, str] = {}
    for ex_id in open_ids:
        ex = await db.get_exercise(ex_id)
        names[ex_id] = ex["display_name"]

    open_items = [(ex_id, names[ex_id]) for ex_id in open_ids]
    active_block_id = (data.get("open_blocks") or {}).get(active)
    has_sets = bool(active_block_id and await db.list_sets_for_block(active_block_id))
    hint = _logging_hint(None if has_sets else last_session_sets.get(active), has_sets)
    kb = keyboards.logging_keyboard(open_items, active, has_sets)
    await _refresh_live(bot, state, user, data["workout_id"], hint, kb)


async def _back_after_cancel(bot, state: FSMContext, user):
    data = await state.get_data()
    if data.get("open_exercises"):
        await state.set_state(WorkoutFlow.logging_set)
        await _render_logging_screen(bot, state, user)
    else:
        await state.set_state(WorkoutFlow.idle)
        await _refresh_live(bot, state, user, data["workout_id"], None, _idle_keyboard(data))


# ---------- main menu ----------

_GREETING = "💪 <b>Привет, АТЛЕТ!</b> Начнём тренировку?"


async def _menu_text(user_id: int, extra: str = "") -> str:
    """Greeting line. extra is appended after it (e.g. stale-workout warning)."""
    return _GREETING + extra


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await _ensure_user(message.from_user.id, message.from_user.username)
    active = await db.get_active_workout(message.from_user.id)
    extra = ""
    if active:
        started = dt.datetime.fromisoformat(active["started_at"])
        if (dt.datetime.now() - started).total_seconds() > config.STALE_WORKOUT_HOURS * 3600:
            extra = (
                f"\n\n⚠️ У тебя висит тренировка с {formatting.format_date_ru(started)} — "
                f"забыл закрыть?"
            )
    text = await _menu_text(message.from_user.id, extra)
    await message.answer(text, reply_markup=keyboards.main_menu(bool(active)), parse_mode="HTML")


async def _show_main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    active = await db.get_active_workout(callback.from_user.id)
    text = await _menu_text(callback.from_user.id)
    await ui.safe_edit(callback, text, reply_markup=keyboards.main_menu(bool(active)), parse_mode="HTML")


@router.callback_query(F.data == "menu:progress")
async def menu_progress(callback: CallbackQuery, state: FSMContext):
    from handlers.history import show_progress_entry
    await show_progress_entry(callback, state)


@router.callback_query(F.data == "menu:history")
async def menu_history(callback: CallbackQuery, state: FSMContext):
    from handlers.history import show_history_list
    await show_history_list(callback, state, page=0)


@router.callback_query(F.data == "menu:exercises")
async def menu_exercises(callback: CallbackQuery, state: FSMContext):
    from handlers.exercises import show_exercise_groups
    await show_exercise_groups(callback, state)


@router.callback_query(F.data == "menu:settings")
async def menu_settings(callback: CallbackQuery, state: FSMContext):
    from handlers.settings import show_settings
    await show_settings(callback, state)


# ---------- start / resume workout ----------

@router.callback_query(F.data == "menu:start_workout")
async def start_workout(callback: CallbackQuery, state: FSMContext):
    await _ensure_user(callback.from_user.id, callback.from_user.username)
    active = await db.get_active_workout(callback.from_user.id)
    if active:
        await _enter_live(callback, state, active["id"])
        return
    workout_id = await db.create_workout(callback.from_user.id)
    await callback.message.delete()
    sent = await callback.message.answer("🏋️ Тренировка начата")
    await state.update_data(
        workout_id=workout_id, live_chat_id=sent.chat.id, live_message_id=sent.message_id,
        last_by_exercise={},
    )
    await state.set_state(WorkoutFlow.picking_group)
    await _picker_screen_groups(callback, state)


@router.callback_query(F.data == "menu:resume_workout")
async def resume_workout(callback: CallbackQuery, state: FSMContext):
    active = await db.get_active_workout(callback.from_user.id)
    if not active:
        await callback.answer("Нет активной тренировки")
        await _show_main_menu(callback, state)
        return
    await _enter_live(callback, state, active["id"])


async def _reopen_exercises(
    workout_id: int,
) -> tuple[list[int], dict[int, int], dict[int, list], dict[int, tuple]]:
    """Rebuild which exercises are still "open" for a workout from the DB.

    The FSM is the only place that tracks "closed" vs "open" exercises, so when we
    re-enter a workout (resume, or bot restart) after losing that in-memory state,
    the best we can do is treat every exercise already logged in this workout as open
    again — that's what lets the user keep adding sets without re-picking it.
    """
    open_exercises: list[int] = []
    open_blocks: dict[int, int] = {}
    for block in await db.list_blocks_for_workout(workout_id):
        for be in await db.get_block_exercises(block["id"]):
            ex_id = be["exercise_id"]
            if ex_id not in open_exercises:
                open_exercises.append(ex_id)
            open_blocks[ex_id] = block["id"]
    last_session_sets = {ex_id: await _last_session_sets(ex_id) for ex_id in open_exercises}
    last_by_exercise: dict[int, tuple] = {}
    for ex_id in open_exercises:
        current_sets = await db.list_sets_for_workout_exercise(workout_id, ex_id)
        if current_sets:
            last = current_sets[-1]
            last_by_exercise[ex_id] = (last["weight"], last["reps"])
        else:
            history = await db.list_sets_for_exercise(ex_id)
            if history:
                last = history[-1]
                last_by_exercise[ex_id] = (last["weight"], last["reps"])
    return open_exercises, open_blocks, last_session_sets, last_by_exercise


async def _enter_live(callback: CallbackQuery, state: FSMContext, workout_id: int):
    user = await _ensure_user(callback.from_user.id, callback.from_user.username)
    await callback.message.delete()
    sent = await callback.message.answer("🏋️ Тренировка")
    open_exercises, open_blocks, last_session_sets, last_by_exercise = await _reopen_exercises(workout_id)
    active_exercise_id = open_exercises[-1] if open_exercises else None
    await state.set_state(WorkoutFlow.logging_set if open_exercises else WorkoutFlow.idle)
    await state.update_data(
        workout_id=workout_id, live_chat_id=sent.chat.id, live_message_id=sent.message_id,
        last_by_exercise=last_by_exercise, open_exercises=open_exercises, open_blocks=open_blocks,
        active_exercise_id=active_exercise_id, last_session_sets=last_session_sets,
    )
    if open_exercises:
        await _render_logging_screen(callback.bot, state, user)
    else:
        await _refresh_live(callback.bot, state, user, workout_id, None, _idle_keyboard(await state.get_data()))


# ---------- picker: add an exercise (either to start, or alongside what's already open) ----------

async def _picker_screen_groups(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user = await db.get_user(callback.from_user.id)
    groups = await db.list_muscle_groups(callback.from_user.id)
    hint = "Выбери группу мышц:"
    open_ids = data.get("open_exercises") or []
    if open_ids:
        names = [escape((await db.get_exercise(eid))["display_name"]) for eid in open_ids]
        hint = "Открыто сейчас: " + ", ".join(names) + "\n" + hint
    extra = [("❌ Отмена", "pick:cancel")]
    kb = keyboards.groups_keyboard(groups, prefix="pick", extra_buttons=extra, show_all=True)
    await state.update_data(picker_stage="groups")
    await _refresh_live(callback.bot, state, user, data["workout_id"], hint, kb)


@router.callback_query(StateFilter(WorkoutFlow.idle, WorkoutFlow.logging_set), F.data == "live:add_exercise")
async def live_add_exercise(callback: CallbackQuery, state: FSMContext):
    await state.set_state(WorkoutFlow.picking_group)
    await _picker_screen_groups(callback, state)
    await callback.answer()


@router.callback_query(
    StateFilter(WorkoutFlow.picking_group, WorkoutFlow.picking_exercise, WorkoutFlow.creating_exercise_name),
    F.data == "pick:cancel",
)
async def pick_cancel(callback: CallbackQuery, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    await _back_after_cancel(callback.bot, state, user)
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.picking_group), F.data.startswith("pick:grp:"))
async def pick_group(callback: CallbackQuery, state: FSMContext):
    raw = callback.data.split(":")[2]
    group_id = None if raw == "all" else int(raw)
    await state.update_data(pending_group_id=group_id, pick_page=0)
    await state.set_state(WorkoutFlow.picking_exercise)
    await _picker_screen_exercises(callback, state)
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.picking_exercise), F.data.startswith("pick:page:"))
async def pick_page(callback: CallbackQuery, state: FSMContext):
    page = int(callback.data.split(":")[2])
    await state.update_data(pick_page=page)
    await _picker_screen_exercises(callback, state)
    await callback.answer()


async def _picker_screen_exercises(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user = await db.get_user(callback.from_user.id)
    group_id = data["pending_group_id"]
    page = data.get("pick_page", 0)
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
        exercises, prefix="pick", back_cb="back", show_new_button=group_id is not None,
        page=page, has_next=has_next,
    )
    if exercises:
        names = [escape(ex["display_name"]) for ex in exercises]
        hint = "Выбери упражнение из своих:\n" + keyboards.numbered_list(names)
    else:
        hint = "У тебя пока нет своих упражнений здесь — добавь новое:"
    await state.update_data(picker_stage="exercises")
    await _refresh_live(callback.bot, state, user, data["workout_id"], hint, kb)


@router.callback_query(StateFilter(WorkoutFlow.picking_exercise), F.data == "pick:back")
async def pick_back_to_groups(callback: CallbackQuery, state: FSMContext):
    await state.set_state(WorkoutFlow.picking_group)
    await _picker_screen_groups(callback, state)
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.picking_exercise), F.data.startswith("pick:ex:"))
async def pick_existing_exercise(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    await _on_exercise_chosen(callback, state, ex_id)


async def _new_exercise_entry_screen(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user = await db.get_user(callback.from_user.id)
    await _refresh_live(
        callback.bot, state, user, data["workout_id"],
        "Напиши название нового упражнения, или выбери из шаблонов:",
        keyboards.new_exercise_entry_keyboard("pick"),
    )


@router.callback_query(StateFilter(WorkoutFlow.picking_exercise), F.data == "pick:new")
async def pick_new_exercise(callback: CallbackQuery, state: FSMContext):
    await state.set_state(WorkoutFlow.creating_exercise_name)
    await _new_exercise_entry_screen(callback, state)
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.creating_exercise_name), F.data == "pick:templates")
async def pick_templates(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user = await db.get_user(callback.from_user.id)
    templates = await db.list_templates_in_group(data["pending_group_id"])
    kb = keyboards.templates_keyboard(templates, prefix="pick", back_cb="newback")
    hint = "Шаблоны — выбери подходящий:"
    if templates:
        names = [escape(t["display_name"]) for t in templates]
        hint += "\n" + keyboards.numbered_list(names)
    await _refresh_live(callback.bot, state, user, data["workout_id"], hint, kb)
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.creating_exercise_name), F.data == "pick:newback")
async def pick_back_from_templates(callback: CallbackQuery, state: FSMContext):
    await _new_exercise_entry_screen(callback, state)
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.creating_exercise_name), F.data.startswith("pick:tpl:"))
async def pick_template(callback: CallbackQuery, state: FSMContext):
    template_id = int(callback.data.split(":")[2])
    ex_id = await db.fork_exercise_from_template(callback.from_user.id, template_id)
    await _on_exercise_chosen(callback, state, ex_id)


@router.message(StateFilter(WorkoutFlow.creating_exercise_name))
async def new_exercise_name_entered(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.reply("Название не может быть пустым")
        return
    await _delete_message(message)
    data = await state.get_data()
    ex_id = await db.create_exercise(message.from_user.id, name, data["pending_group_id"])
    await _on_exercise_chosen(message, state, ex_id)


async def _seed_last_value(data: dict, ex_id: int) -> dict:
    history = await db.list_sets_for_exercise(ex_id)
    last_by = dict(data.get("last_by_exercise") or {})
    if history:
        last = history[-1]
        last_by[ex_id] = (last["weight"], last["reps"])
    return last_by


async def _on_exercise_chosen(event, state: FSMContext, ex_id: int):
    data = await state.get_data()
    await db.touch_exercise_last_used(ex_id)

    open_exercises = list(data.get("open_exercises") or [])
    open_blocks = dict(data.get("open_blocks") or {})

    if ex_id not in open_exercises:
        block_id = await db.create_block(data["workout_id"], "single")
        await db.add_block_exercise(block_id, ex_id, 0)
        open_exercises.append(ex_id)
        open_blocks[ex_id] = block_id
    last_by = await _seed_last_value(data, ex_id)
    last_session_sets = dict(data.get("last_session_sets") or {})
    last_session_sets[ex_id] = await _last_session_sets(ex_id)

    await state.update_data(
        open_exercises=open_exercises, open_blocks=open_blocks,
        active_exercise_id=ex_id, last_by_exercise=last_by, last_session_sets=last_session_sets,
    )
    await state.set_state(WorkoutFlow.logging_set)
    user = await db.get_user(event.from_user.id)
    await _render_logging_screen(event.bot, state, user)
    if isinstance(event, CallbackQuery):
        await event.answer()


# ---------- logging sets: type "weight reps", switch between open exercises freely ----------

@router.callback_query(StateFilter(WorkoutFlow.logging_set), F.data.startswith("live:switch:"))
async def live_switch_exercise(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    data = await state.get_data()
    if ex_id not in (data.get("open_exercises") or []):
        await callback.answer()
        return
    await state.update_data(active_exercise_id=ex_id)
    await callback.answer()
    user = await db.get_user(callback.from_user.id)
    await _render_logging_screen(callback.bot, state, user)


@router.message(StateFilter(WorkoutFlow.logging_set))
async def log_set_text(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        parsed = parse_single_token(message.text)
    except ParseError as e:
        await message.reply(e.message)
        return
    active = data.get("active_exercise_id")
    block_id = (data.get("open_blocks") or {}).get(active)
    last_by = dict(data.get("last_by_exercise") or {})
    prev_weight, _ = last_by.get(active) or (0.0, 0)
    for ps in parsed:
        weight = prev_weight if (ps.weight_omitted and prev_weight) else ps.weight
        await _log_one(block_id, active, weight, ps.reps, ps.is_warmup)
        prev_weight = weight
    last_by[active] = (prev_weight, parsed[-1].reps)
    await state.update_data(last_by_exercise=last_by)
    await _react_ok(message.bot, message)
    await _delete_message(message)
    user = await db.get_user(message.from_user.id)
    await _render_logging_screen(message.bot, state, user)


@router.callback_query(StateFilter(WorkoutFlow.logging_set), F.data == "live:undo")
async def live_undo(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    active = data.get("active_exercise_id")
    block_id = (data.get("open_blocks") or {}).get(active)
    row = await db.delete_last_set_in_block(block_id)
    if row is None:
        await callback.answer("Нет сетов для удаления")
        return
    await callback.answer(f"Удалил {formatting.format_set(row['weight'], row['reps'], bool(row['is_warmup']))}")
    user = await db.get_user(callback.from_user.id)

    remaining = await db.list_sets_for_block(block_id)
    if not remaining:
        await db.delete_block(block_id)
        open_exercises = [eid for eid in (data.get("open_exercises") or []) if eid != active]
        open_blocks = dict(data.get("open_blocks") or {})
        open_blocks.pop(active, None)
        if open_exercises:
            await state.update_data(
                open_exercises=open_exercises, open_blocks=open_blocks, active_exercise_id=open_exercises[0],
            )
        else:
            await state.update_data(open_exercises=[], open_blocks={}, active_exercise_id=None)
            await state.set_state(WorkoutFlow.idle)
            await _refresh_live(callback.bot, state, user, data["workout_id"], None, _idle_keyboard(data))
            return

    await _render_logging_screen(callback.bot, state, user)


@router.callback_query(StateFilter(WorkoutFlow.logging_set), F.data == "live:finish_exercise")
async def live_finish_exercise(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    active = data.get("active_exercise_id")
    open_exercises = [eid for eid in (data.get("open_exercises") or []) if eid != active]
    open_blocks = dict(data.get("open_blocks") or {})
    open_blocks.pop(active, None)
    user = await db.get_user(callback.from_user.id)

    if open_exercises:
        await state.update_data(
            open_exercises=open_exercises, open_blocks=open_blocks, active_exercise_id=open_exercises[0],
        )
        await _render_logging_screen(callback.bot, state, user)
    else:
        await state.update_data(open_exercises=[], open_blocks={}, active_exercise_id=None)
        await state.set_state(WorkoutFlow.idle)
        await _refresh_live(callback.bot, state, user, data["workout_id"], None, _idle_keyboard(data))
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.idle), F.data == "live:next_planned")
async def live_next_planned(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    planned = list(data.get("planned_blocks") or [])
    if not planned:
        await callback.answer("Шаблон закончился")
        return
    block_plan = planned.pop(0)
    await state.update_data(planned_blocks=planned)
    workout_id = data["workout_id"]

    open_exercises: list[int] = []
    open_blocks: dict[int, int] = {}
    last_by = dict(data.get("last_by_exercise") or {})
    last_session_sets = dict(data.get("last_session_sets") or {})
    for ex_id in block_plan["exercise_ids"]:
        block_id = await db.create_block(workout_id, "single")
        await db.add_block_exercise(block_id, ex_id, 0)
        await db.touch_exercise_last_used(ex_id)
        last_by = await _seed_last_value({"last_by_exercise": last_by}, ex_id)
        last_session_sets[ex_id] = await _last_session_sets(ex_id)
        open_exercises.append(ex_id)
        open_blocks[ex_id] = block_id

    await state.update_data(
        open_exercises=open_exercises, open_blocks=open_blocks,
        active_exercise_id=open_exercises[0], last_by_exercise=last_by, last_session_sets=last_session_sets,
    )
    await state.set_state(WorkoutFlow.logging_set)
    user = await db.get_user(callback.from_user.id)
    await _render_logging_screen(callback.bot, state, user)
    await callback.answer()


# ---------- finishing the workout ----------

@router.callback_query(StateFilter(WorkoutFlow.idle), F.data == "live:finish_workout")
async def live_finish_workout(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    workout_id = data["workout_id"]
    exercise_ids = await db.list_exercise_ids_for_workout(workout_id)
    if not exercise_ids:
        await db.discard_workout(workout_id)
        await state.clear()
        await _show_main_menu(callback, state)
        await callback.answer("Тренировка была пустая — удалил её.")
        return
    await ui.safe_edit(
        callback,
        "Завершаем? Можно добавить заметку (сон/самочувствие):",
        reply_markup=keyboards.finish_workout_keyboard(),
    )
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.idle), F.data == "live:cancel_finish")
async def cancel_finish(callback: CallbackQuery, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    await _back_after_cancel(callback.bot, state, user)
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.idle), F.data == "finish:note")
async def finish_ask_note(callback: CallbackQuery, state: FSMContext):
    await state.set_state(WorkoutFlow.finishing_note)
    await ui.safe_edit(
        callback,
        "Напиши заметку (сон, самочувствие, что угодно):",
        reply_markup=keyboards.cancel_keyboard("live:cancel_finish"),
    )
    await callback.answer()


@router.message(StateFilter(WorkoutFlow.finishing_note))
async def finish_note_entered(message: Message, state: FSMContext):
    await _finalize_workout(message, state, note=message.text.strip())


@router.callback_query(StateFilter(WorkoutFlow.idle), F.data == "finish:skip_note")
async def finish_skip_note(callback: CallbackQuery, state: FSMContext):
    await _finalize_workout(callback, state, note=None)


async def _finalize_workout(event, state: FSMContext, note: str | None):
    data = await state.get_data()
    workout_id = data["workout_id"]
    user_id = event.from_user.id
    bot = event.bot
    user = await db.get_user(user_id)
    formula = user["e1rm_formula"]

    exercise_ids = await db.list_exercise_ids_for_workout(workout_id)
    highlight_groups: list[tuple[str, list[str], str | None]] = []

    workout = await db.get_workout(workout_id)
    started_at = dt.datetime.fromisoformat(workout["started_at"])

    for ex_id in exercise_ids:
        ex = await db.get_exercise(ex_id)
        history_rows = await db.list_sets_for_exercise(ex_id, exclude_workout_id=workout_id)
        history_set_rows = [
            analytics.SetRow(r["weight"], r["reps"], bool(r["is_warmup"]), r["workout_id"], r["started_at"])
            for r in history_rows
        ]
        prior_sessions = analytics.group_sets_by_session(history_set_rows)
        for s in prior_sessions:
            s.formula = formula

        this_rows = await db.list_sets_for_workout_exercise(workout_id, ex_id)
        this_set_rows = [
            analytics.SetRow(r["weight"], r["reps"], bool(r["is_warmup"]), workout_id, workout["started_at"])
            for r in this_rows
        ]
        new_session = analytics.SessionStats(
            workout_id=workout_id, started_at=workout["started_at"], sets=this_set_rows, formula=formula
        )
        if not new_session.working_sets:
            continue

        records = analytics.detect_new_records(prior_sessions, new_session)
        pr_details = [
            formatting.format_pr_detail(r.kind, r.value, r.extra, unit=user["unit"])
            for r in records
            if r.kind != "e1rm"
        ]

        comparison_line = None
        if prior_sessions:
            comparison = analytics.compare_to_previous_session(prior_sessions + [new_session])
            if comparison and not new_session.is_bodyweight_mode:
                comparison_line = formatting.format_comparison_line(comparison.e1rm_delta, unit=user["unit"])

        if pr_details or comparison_line:
            highlight_groups.append((ex["display_name"], pr_details, comparison_line))

    is_backfill = bool(data.get("is_backfill"))
    finished_at = f"{data['bf_date']}T12:00:00" if is_backfill else None
    await db.finish_workout(workout_id, note, finished_at=finished_at)

    blocks = await view_builder.build_block_views(workout_id, formula)
    summary = formatting.build_workout_summary(
        started_at, blocks, note,
        hide_warmups=bool(user["hide_warmups"]), show_extra_stats=bool(user["show_extra_stats"]),
    )
    highlights = formatting.build_exercise_highlights(highlight_groups)
    full_text = summary + (f"\n\n{formatting.DIVIDER}\n\n{highlights}" if highlights else "")
    if is_backfill:
        full_text = "✅ Сохранено как прошлая тренировка\n\n" + full_text

    card_kb = keyboards.workout_card_keyboard(workout_id)
    try:
        await bot.edit_message_text(
            chat_id=data["live_chat_id"], message_id=data["live_message_id"], text=full_text,
            parse_mode="HTML", reply_markup=card_kb,
        )
    except TelegramBadRequest:
        await bot.send_message(
            chat_id=data["live_chat_id"], text=full_text, parse_mode="HTML", reply_markup=card_kb
        )

    await state.clear()
    active = await db.get_active_workout(user_id)
    menu_text = await _menu_text(user_id)
    await bot.send_message(
        chat_id=data["live_chat_id"], text=menu_text, reply_markup=keyboards.main_menu(bool(active)),
        parse_mode="HTML",
    )
