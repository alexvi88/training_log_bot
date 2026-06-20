"""Workout lifecycle: start, add exercise/superset, log sets, finish."""

import datetime as dt

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
import view_builder
from fsm import WorkoutFlow
from parser import ParseError, parse_single_token, parse_superset_line

router = Router(name="workout")


# ---------- helpers ----------

async def _ensure_user(telegram_id: int, username: str | None):
    return await db.get_or_create_user(telegram_id, username)


async def _refresh_live(bot, chat_id: int, message_id: int, user, workout_id: int, hint, keyboard):
    blocks = await view_builder.build_block_views(workout_id, user["e1rm_formula"])
    workout = await db.get_workout(workout_id)
    started_at = dt.datetime.fromisoformat(workout["started_at"])
    text = formatting.build_live_session_text(
        started_at, blocks, hint, hide_warmups=bool(user["hide_warmups"])
    )
    try:
        await bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=text, reply_markup=keyboard
        )
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise


def _idle_keyboard(data: dict | None = None):
    has_planned = bool((data or {}).get("planned_blocks"))
    return keyboards.exercise_picker_entry_keyboard(has_planned=has_planned)


async def _react_ok(bot, message: Message):
    try:
        await bot.set_message_reaction(
            chat_id=message.chat.id, message_id=message.message_id,
            reaction=[ReactionTypeEmoji(emoji="✅")],
        )
    except TelegramBadRequest:
        pass


def _current_target_exercise(data: dict) -> int | None:
    if data.get("current_exercise_id") is not None:
        return data["current_exercise_id"]
    ids = data.get("superset_exercise_ids")
    idx = data.get("current_superset_idx", 0)
    if ids:
        return ids[idx]
    return None


async def _log_one(block_id: int, exercise_id: int, weight: float, reps: int, is_warmup: bool, order_in_round: int = 0):
    round_idx = await db.next_round_index(block_id, exercise_id)
    await db.add_set(block_id, exercise_id, round_idx, order_in_round, weight, reps, is_warmup)


def _effective_steps(ex, user) -> tuple[float, float]:
    step = ex["weight_step"] if ex["weight_step"] is not None else user["default_weight_step"]
    step_big = ex["weight_step_big"] if ex["weight_step_big"] is not None else step * 4
    return step, step_big


async def _advance_superset(state: FSMContext, data: dict) -> None:
    ids = data["superset_exercise_ids"]
    next_idx = (data.get("current_superset_idx", 0) + 1) % len(ids)
    await state.update_data(current_superset_idx=next_idx)


_LOGGING_HINT = (
    "Тапни число повторов — запишется сет с текущим весом.\n"
    "Текстом — fallback: 100 8 · 100x8x3 · +20 8 · 8 (свой вес)"
)


async def _render_logging_screen(bot, state: FSMContext, user):
    data = await state.get_data()
    in_superset = bool(data.get("superset_exercise_ids"))
    last_by = data.get("last_by_exercise") or {}
    target = _current_target_exercise(data)
    ex = await db.get_exercise(target)
    step, step_big = _effective_steps(ex, user)
    weight, _ = last_by.get(target, (0.0, 0))

    if in_superset:
        names = data.get("superset_exercise_names") or []
        idx = data.get("current_superset_idx", 0)
        hint = (
            f"🔗 Суперсет: {' ⇄ '.join(names)}\nСейчас: {names[idx]}.\n{_LOGGING_HINT}\n"
            f"Весь раунд одной строкой: 30 12 / 15 10"
        )
    else:
        hint = _LOGGING_HINT

    kb = keyboards.set_input_keyboard(weight, step, step_big, in_superset=in_superset)
    await _refresh_live(bot, data["live_chat_id"], data["live_message_id"], user, data["workout_id"], hint, kb)


# ---------- main menu ----------

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user = await _ensure_user(message.from_user.id, message.from_user.username)
    active = await db.get_active_workout(message.from_user.id)
    extra = ""
    if active:
        started = dt.datetime.fromisoformat(active["started_at"])
        if (dt.datetime.now() - started).total_seconds() > config.STALE_WORKOUT_HOURS * 3600:
            extra = (
                f"\n\n⚠️ У тебя висит тренировка с {formatting.format_date_ru(started)} — "
                f"забыл закрыть?"
            )
    await message.answer(
        f"Привет! Что делаем?{extra}", reply_markup=keyboards.main_menu(bool(active))
    )


async def _show_main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    active = await db.get_active_workout(callback.from_user.id)
    await callback.message.edit_text(
        "Привет! Что делаем?", reply_markup=keyboards.main_menu(bool(active))
    )


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
    user = await _ensure_user(callback.from_user.id, callback.from_user.username)
    active = await db.get_active_workout(callback.from_user.id)
    if active:
        await _enter_live(callback, state, active["id"])
        return
    workout_id = await db.create_workout(callback.from_user.id)
    await callback.message.delete()
    sent = await callback.message.answer("🏋️ Тренировка начата")
    await state.set_state(WorkoutFlow.idle)
    await state.update_data(
        workout_id=workout_id, live_chat_id=sent.chat.id, live_message_id=sent.message_id,
        last_by_exercise={},
    )
    await _refresh_live(callback.bot, sent.chat.id, sent.message_id, user, workout_id, None, _idle_keyboard())


@router.callback_query(F.data == "menu:resume_workout")
async def resume_workout(callback: CallbackQuery, state: FSMContext):
    active = await db.get_active_workout(callback.from_user.id)
    if not active:
        await callback.answer("Нет активной тренировки")
        await _show_main_menu(callback, state)
        return
    await _enter_live(callback, state, active["id"])


async def _enter_live(callback: CallbackQuery, state: FSMContext, workout_id: int):
    user = await _ensure_user(callback.from_user.id, callback.from_user.username)
    await callback.message.delete()
    sent = await callback.message.answer("🏋️ Тренировка")
    await state.set_state(WorkoutFlow.idle)
    await state.update_data(
        workout_id=workout_id, live_chat_id=sent.chat.id, live_message_id=sent.message_id,
        last_by_exercise={}, current_exercise_id=None, current_block_id=None,
        superset_exercise_ids=None,
    )
    data = await state.get_data()
    await _refresh_live(callback.bot, sent.chat.id, sent.message_id, user, workout_id, None, _idle_keyboard(data))


# ---------- picker: add single exercise / build superset ----------

async def _picker_screen_groups(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user = await db.get_user(callback.from_user.id)
    groups = await db.list_muscle_groups(callback.from_user.id)
    mode = data.get("picker_mode", "single")
    if mode == "superset":
        ids = data.get("superset_exercise_ids") or []
        names = data.get("superset_exercise_names") or []
        hint = ("🔗 Суперсет: " + ", ".join(names) + "\nВыбери группу для следующего упражнения:") if names \
            else "🔗 Суперсет — выбери группу для первого упражнения:"
        extra = [("❌ Отмена", "pick:cancel")]
        if len(ids) >= 2:
            extra.insert(0, ("✅ Готово", "pick:done"))
    else:
        hint = "Выбери группу мышц:"
        extra = [("❌ Отмена", "pick:cancel")]
    kb = keyboards.groups_keyboard(groups, prefix="pick", extra_buttons=extra)
    await state.update_data(picker_stage="groups")
    await _refresh_live(
        callback.bot, data["live_chat_id"], data["live_message_id"], user, data["workout_id"], hint, kb
    )


@router.callback_query(F.data == "live:add_exercise")
async def live_add_exercise(callback: CallbackQuery, state: FSMContext):
    await state.update_data(picker_mode="single", superset_exercise_ids=None, superset_exercise_names=None)
    await state.set_state(WorkoutFlow.picking_group)
    await _picker_screen_groups(callback, state)
    await callback.answer()


@router.callback_query(F.data == "live:add_superset")
async def live_add_superset(callback: CallbackQuery, state: FSMContext):
    await state.update_data(picker_mode="superset", superset_exercise_ids=[], superset_exercise_names=[])
    await state.set_state(WorkoutFlow.picking_group)
    await _picker_screen_groups(callback, state)
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.picking_group, WorkoutFlow.picking_exercise), F.data == "pick:cancel")
async def pick_cancel(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user = await db.get_user(callback.from_user.id)
    await state.set_state(WorkoutFlow.idle)
    await _refresh_live(
        callback.bot, data["live_chat_id"], data["live_message_id"], user, data["workout_id"], None, _idle_keyboard(data)
    )
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.picking_group), F.data.startswith("pick:grp:"))
async def pick_group(callback: CallbackQuery, state: FSMContext):
    group_id = int(callback.data.split(":")[2])
    await state.update_data(pending_group_id=group_id)
    await state.set_state(WorkoutFlow.picking_exercise)
    await _picker_screen_exercises(callback, state)
    await callback.answer()


async def _picker_screen_exercises(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user = await db.get_user(callback.from_user.id)
    group_id = data["pending_group_id"]
    exercises = await db.list_user_exercises_in_group(
        callback.from_user.id, group_id, limit=config.RECENT_EXERCISES_LIMIT
    )
    kb = keyboards.exercises_keyboard(exercises, prefix="pick", back_cb="back")
    mode = data.get("picker_mode", "single")
    hint = "Выбери упражнение (можешь просто написать название для поиска):"
    if mode == "superset":
        names = data.get("superset_exercise_names") or []
        if names:
            hint = "🔗 Суперсет: " + ", ".join(names) + "\n" + hint
    await state.update_data(picker_stage="exercises")
    await _refresh_live(
        callback.bot, data["live_chat_id"], data["live_message_id"], user, data["workout_id"], hint, kb
    )


@router.callback_query(StateFilter(WorkoutFlow.picking_exercise), F.data == "pick:back")
async def pick_back_to_groups(callback: CallbackQuery, state: FSMContext):
    await state.set_state(WorkoutFlow.picking_group)
    await _picker_screen_groups(callback, state)
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.picking_exercise), F.data == "pick:templates")
async def pick_templates(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user = await db.get_user(callback.from_user.id)
    templates = await db.list_templates_in_group(data["pending_group_id"])
    kb = keyboards.templates_keyboard(templates, prefix="pick")
    await _refresh_live(
        callback.bot, data["live_chat_id"], data["live_message_id"], user, data["workout_id"],
        "Шаблоны — выбери, потом можно уточнить вариант:", kb,
    )
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.picking_exercise), F.data == "pick:back2")
async def pick_back_from_templates(callback: CallbackQuery, state: FSMContext):
    await _picker_screen_exercises(callback, state)
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.picking_exercise), F.data.startswith("pick:ex:"))
async def pick_existing_exercise(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    await _on_exercise_chosen(callback, state, ex_id)


@router.callback_query(StateFilter(WorkoutFlow.picking_exercise), F.data.startswith("pick:tpl:"))
async def pick_template(callback: CallbackQuery, state: FSMContext):
    template_id = int(callback.data.split(":")[2])
    template = await db.get_exercise(template_id)
    await state.update_data(
        new_ex_template_id=template_id, new_ex_name=template["name"],
        new_ex_equipment=None, new_ex_unilateral=False,
    )
    await state.set_state(WorkoutFlow.creating_exercise_attrs)
    await _attrs_screen(callback, state)
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.picking_exercise), F.data == "pick:new")
async def pick_new_exercise(callback: CallbackQuery, state: FSMContext):
    await state.set_state(WorkoutFlow.creating_exercise_name)
    data = await state.get_data()
    user = await db.get_user(callback.from_user.id)
    await _refresh_live(
        callback.bot, data["live_chat_id"], data["live_message_id"], user, data["workout_id"],
        "Напиши название упражнения:", keyboards.cancel_keyboard("pick:cancel"),
    )
    await callback.answer()


@router.message(StateFilter(WorkoutFlow.creating_exercise_name))
async def new_exercise_name_entered(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.reply("Название не может быть пустым")
        return
    await state.update_data(
        new_ex_template_id=None, new_ex_name=name, new_ex_equipment=None, new_ex_unilateral=False
    )
    await state.set_state(WorkoutFlow.creating_exercise_attrs)
    await message.delete()
    data = await state.get_data()
    user = await db.get_user(message.from_user.id)
    await _refresh_live(
        message.bot, data["live_chat_id"], data["live_message_id"], user, data["workout_id"],
        f"«{name}» — можешь уточнить оснастку/хват текстом или нажать Готово.",
        keyboards.new_exercise_attrs_keyboard(),
    )


async def _attrs_screen(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user = await db.get_user(callback.from_user.id)
    name = data.get("new_ex_name", "")
    equipment = data.get("new_ex_equipment")
    unilateral = data.get("new_ex_unilateral", False)
    parts = [name]
    if unilateral:
        parts.append("одной рукой")
    if equipment:
        parts.append(equipment)
    hint = "Вариант: " + " · ".join(parts) + "\nНапиши уточнение текстом или нажми Готово."
    await _refresh_live(
        callback.bot, data["live_chat_id"], data["live_message_id"], user, data["workout_id"],
        hint, keyboards.new_exercise_attrs_keyboard(),
    )


@router.callback_query(StateFilter(WorkoutFlow.creating_exercise_attrs), F.data == "attr:unilateral")
async def attr_toggle_unilateral(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(new_ex_unilateral=not data.get("new_ex_unilateral", False))
    await _attrs_screen(callback, state)
    await callback.answer()


@router.message(StateFilter(WorkoutFlow.creating_exercise_attrs))
async def attr_text_entered(message: Message, state: FSMContext):
    await state.update_data(new_ex_equipment=message.text.strip())
    await message.delete()
    data = await state.get_data()
    user = await db.get_user(message.from_user.id)
    name = data.get("new_ex_name", "")
    equipment = data.get("new_ex_equipment")
    unilateral = data.get("new_ex_unilateral", False)
    parts = [name]
    if unilateral:
        parts.append("одной рукой")
    if equipment:
        parts.append(equipment)
    hint = "Вариант: " + " · ".join(parts) + "\nНапиши ещё уточнение или нажми Готово."
    await _refresh_live(
        message.bot, data["live_chat_id"], data["live_message_id"], user, data["workout_id"],
        hint, keyboards.new_exercise_attrs_keyboard(),
    )


@router.callback_query(StateFilter(WorkoutFlow.creating_exercise_attrs), F.data == "attr:done")
async def attr_done(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    template_id = data.get("new_ex_template_id")
    if template_id:
        ex_id = await db.fork_exercise_from_template(
            callback.from_user.id, template_id,
            equipment=data.get("new_ex_equipment"), unilateral=data.get("new_ex_unilateral"),
        )
    else:
        ex_id = await db.create_exercise(
            callback.from_user.id, data["new_ex_name"], data["pending_group_id"],
            equipment=data.get("new_ex_equipment"), unilateral=data.get("new_ex_unilateral", False),
        )
    await _on_exercise_chosen(callback, state, ex_id)


async def _seed_last_value(data: dict, ex_id: int) -> dict:
    history = await db.list_sets_for_exercise(ex_id)
    last_by = dict(data.get("last_by_exercise") or {})
    if history:
        last = history[-1]
        last_by[ex_id] = (last["weight"], last["reps"])
    return last_by


async def _on_exercise_chosen(callback: CallbackQuery, state: FSMContext, ex_id: int):
    data = await state.get_data()
    mode = data.get("picker_mode", "single")
    await db.touch_exercise_last_used(ex_id)
    ex = await db.get_exercise(ex_id)

    if mode == "superset":
        ids = list(data.get("superset_exercise_ids") or [])
        names = list(data.get("superset_exercise_names") or [])
        ids.append(ex_id)
        names.append(ex["display_name"])
        await state.update_data(superset_exercise_ids=ids, superset_exercise_names=names)
        await state.set_state(WorkoutFlow.picking_group)
        await _picker_screen_groups(callback, state)
        await callback.answer()
        return

    block_id = await db.create_block(data["workout_id"], "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    last_by = await _seed_last_value(data, ex_id)
    await state.update_data(
        current_block_id=block_id, current_exercise_id=ex_id, last_by_exercise=last_by
    )
    await state.set_state(WorkoutFlow.logging_set)
    user = await db.get_user(callback.from_user.id)
    await _render_logging_screen(callback.bot, state, user)
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.picking_group), F.data == "pick:done")
async def pick_superset_done(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    ids = data.get("superset_exercise_ids") or []
    if len(ids) < 2:
        await callback.answer("Нужно минимум 2 упражнения", show_alert=True)
        return
    block_id = await db.create_block(data["workout_id"], "superset")
    last_by = dict(data.get("last_by_exercise") or {})
    for idx, ex_id in enumerate(ids):
        await db.add_block_exercise(block_id, ex_id, idx)
        last_by = await _seed_last_value({"last_by_exercise": last_by}, ex_id)
    await state.update_data(
        current_block_id=block_id, current_exercise_id=None,
        superset_exercise_ids=ids, current_superset_idx=0, last_by_exercise=last_by,
    )
    await state.set_state(WorkoutFlow.logging_superset)
    user = await db.get_user(callback.from_user.id)
    await _render_logging_screen(callback.bot, state, user)
    await callback.answer()


# ---------- search by typing while picking exercise ----------

@router.message(StateFilter(WorkoutFlow.picking_exercise))
async def search_exercise_text(message: Message, state: FSMContext):
    query = message.text.strip()
    await message.delete()
    if not query:
        return
    data = await state.get_data()
    user = await db.get_user(message.from_user.id)
    results = await db.search_exercises(message.from_user.id, query)
    kb = keyboards.exercises_keyboard(
        results, prefix="pick", show_templates_button=False, show_new_button=True, back_cb="back"
    )
    hint = f"Результаты поиска «{query}»:" if results else f"Ничего не нашлось по «{query}». Можно создать новое."
    await _refresh_live(
        message.bot, data["live_chat_id"], data["live_message_id"], user, data["workout_id"], hint, kb
    )


# ---------- logging sets: buttons are the primary path, text is fallback ----------

@router.callback_query(StateFilter(WorkoutFlow.logging_set, WorkoutFlow.logging_superset), F.data.startswith("live:reps:"))
async def live_tap_reps(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    suffix = callback.data.split(":")[2]

    if suffix == "other":
        await state.update_data(return_state=await state.get_state())
        await state.set_state(WorkoutFlow.entering_reps)
        user = await db.get_user(callback.from_user.id)
        await _refresh_live(
            callback.bot, data["live_chat_id"], data["live_message_id"], user, data["workout_id"],
            "Напиши число повторов:", keyboards.cancel_keyboard("live:cancel_input"),
        )
        await callback.answer()
        return

    reps = int(suffix)
    target = _current_target_exercise(data)
    last_by = dict(data.get("last_by_exercise") or {})
    weight, _ = last_by.get(target, (0.0, 0))
    block_id = data["current_block_id"]
    in_superset = bool(data.get("superset_exercise_ids"))
    order_in_round = data.get("current_superset_idx", 0) if in_superset else 0
    await _log_one(block_id, target, weight, reps, False, order_in_round)
    last_by[target] = (weight, reps)
    await state.update_data(last_by_exercise=last_by)
    if in_superset:
        await _advance_superset(state, data)
    await callback.answer(f"Записал {formatting.format_set(weight, reps)}")
    user = await db.get_user(callback.from_user.id)
    await _render_logging_screen(callback.bot, state, user)


@router.callback_query(
    StateFilter(WorkoutFlow.logging_set, WorkoutFlow.logging_superset),
    F.data.in_({"live:w:plus", "live:w:minus", "live:w:bigplus", "live:w:bigminus"}),
)
async def live_adjust_weight(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    target = _current_target_exercise(data)
    user = await db.get_user(callback.from_user.id)
    ex = await db.get_exercise(target)
    step, step_big = _effective_steps(ex, user)
    deltas = {
        "live:w:plus": step, "live:w:minus": -step,
        "live:w:bigplus": step_big, "live:w:bigminus": -step_big,
    }
    last_by = dict(data.get("last_by_exercise") or {})
    weight, reps = last_by.get(target, (0.0, 0))
    weight = max(0.0, weight + deltas[callback.data])
    last_by[target] = (weight, reps)
    await state.update_data(last_by_exercise=last_by)
    await callback.answer(f"{formatting.format_weight(weight)} кг")
    await _render_logging_screen(callback.bot, state, user)


@router.callback_query(StateFilter(WorkoutFlow.logging_set, WorkoutFlow.logging_superset), F.data == "live:w:exact")
async def live_enter_exact_weight(callback: CallbackQuery, state: FSMContext):
    await state.update_data(return_state=await state.get_state())
    await state.set_state(WorkoutFlow.entering_weight)
    data = await state.get_data()
    user = await db.get_user(callback.from_user.id)
    await _refresh_live(
        callback.bot, data["live_chat_id"], data["live_message_id"], user, data["workout_id"],
        "Напиши точный вес в кг (например 102.5):", keyboards.cancel_keyboard("live:cancel_input"),
    )
    await callback.answer()


@router.message(StateFilter(WorkoutFlow.entering_weight))
async def exact_weight_entered(message: Message, state: FSMContext):
    try:
        weight = float(message.text.strip().replace(",", "."))
        if weight < 0:
            raise ValueError
    except ValueError:
        await message.reply("Нужно неотрицательное число, например 102.5")
        return
    data = await state.get_data()
    target = _current_target_exercise(data)
    last_by = dict(data.get("last_by_exercise") or {})
    _, reps = last_by.get(target, (0.0, 0))
    last_by[target] = (weight, reps)
    await state.update_data(last_by_exercise=last_by)
    await message.delete()
    await state.set_state(data.get("return_state") or WorkoutFlow.logging_set)
    user = await db.get_user(message.from_user.id)
    await _render_logging_screen(message.bot, state, user)


@router.message(StateFilter(WorkoutFlow.entering_reps))
async def other_reps_entered(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        await message.reply("Нужно целое число повторов больше 0")
        return
    reps = int(text)
    data = await state.get_data()
    target = _current_target_exercise(data)
    last_by = dict(data.get("last_by_exercise") or {})
    weight, _ = last_by.get(target, (0.0, 0))
    block_id = data["current_block_id"]
    in_superset = bool(data.get("superset_exercise_ids"))
    order_in_round = data.get("current_superset_idx", 0) if in_superset else 0
    await _log_one(block_id, target, weight, reps, False, order_in_round)
    last_by[target] = (weight, reps)
    await state.update_data(last_by_exercise=last_by)
    if in_superset:
        await _advance_superset(state, data)
    await message.delete()
    await state.set_state(data.get("return_state") or WorkoutFlow.logging_set)
    await _react_ok(message.bot, message)
    user = await db.get_user(message.from_user.id)
    await _render_logging_screen(message.bot, state, user)


@router.callback_query(F.data == "live:cancel_input")
async def cancel_input(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.set_state(data.get("return_state") or WorkoutFlow.logging_set)
    user = await db.get_user(callback.from_user.id)
    await _render_logging_screen(callback.bot, state, user)
    await callback.answer()


@router.message(StateFilter(WorkoutFlow.logging_set))
async def log_set_text(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        parsed = parse_single_token(message.text)
    except ParseError as e:
        await message.reply(e.message)
        return
    block_id = data["current_block_id"]
    ex_id = data["current_exercise_id"]
    for ps in parsed:
        await _log_one(block_id, ex_id, ps.weight, ps.reps, ps.is_warmup)
    last_by = dict(data.get("last_by_exercise") or {})
    last_by[ex_id] = (parsed[-1].weight, parsed[-1].reps)
    await state.update_data(last_by_exercise=last_by)
    await _react_ok(message.bot, message)
    user = await db.get_user(message.from_user.id)
    await _render_logging_screen(message.bot, state, user)


@router.message(StateFilter(WorkoutFlow.logging_superset))
async def log_superset_text(message: Message, state: FSMContext):
    data = await state.get_data()
    ids = data["superset_exercise_ids"]
    block_id = data["current_block_id"]
    last_by = dict(data.get("last_by_exercise") or {})

    if "/" in message.text:
        try:
            parsed_rounds = parse_superset_line(message.text, len(ids))
        except ParseError as e:
            await message.reply(e.message)
            return
        round_idx = await db.next_round_index(block_id, ids[0])
        for idx, ex_id in enumerate(ids):
            ps = parsed_rounds[idx][0]
            await db.add_set(block_id, ex_id, round_idx, idx, ps.weight, ps.reps, ps.is_warmup)
            last_by[ex_id] = (ps.weight, ps.reps)
        next_idx = 0
    else:
        try:
            parsed = parse_single_token(message.text)
        except ParseError as e:
            await message.reply(e.message)
            return
        idx = data.get("current_superset_idx", 0)
        ex_id = ids[idx]
        for ps in parsed:
            await _log_one(block_id, ex_id, ps.weight, ps.reps, ps.is_warmup, order_in_round=idx)
        last_by[ex_id] = (parsed[-1].weight, parsed[-1].reps)
        next_idx = (idx + 1) % len(ids)

    await state.update_data(last_by_exercise=last_by, current_superset_idx=next_idx)
    await _react_ok(message.bot, message)
    user = await db.get_user(message.from_user.id)
    await _render_logging_screen(message.bot, state, user)


@router.callback_query(StateFilter(WorkoutFlow.logging_set, WorkoutFlow.logging_superset), F.data == "live:undo")
async def live_undo(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    row = await db.delete_last_set_in_block(data["current_block_id"])
    if row is None:
        await callback.answer("Нет сетов для удаления")
        return
    await callback.answer(f"Удалил {formatting.format_set(row['weight'], row['reps'], bool(row['is_warmup']))}")
    user = await db.get_user(callback.from_user.id)
    await _render_logging_screen(callback.bot, state, user)


@router.callback_query(StateFilter(WorkoutFlow.logging_set, WorkoutFlow.logging_superset), F.data == "live:finish_exercise")
async def live_finish_exercise(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(
        current_block_id=None, current_exercise_id=None,
        superset_exercise_ids=None, superset_exercise_names=None, current_superset_idx=0,
    )
    await state.set_state(WorkoutFlow.idle)
    user = await db.get_user(callback.from_user.id)
    await _refresh_live(
        callback.bot, data["live_chat_id"], data["live_message_id"], user, data["workout_id"],
        None, _idle_keyboard(data),
    )
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
    block_id = await db.create_block(workout_id, block_plan["type"])
    last_by = dict(data.get("last_by_exercise") or {})
    ex_ids = block_plan["exercise_ids"]
    for idx, ex_id in enumerate(ex_ids):
        await db.add_block_exercise(block_id, ex_id, idx)
        await db.touch_exercise_last_used(ex_id)
        last_by = await _seed_last_value({"last_by_exercise": last_by}, ex_id)
    user = await db.get_user(callback.from_user.id)

    if block_plan["type"] == "single":
        await state.update_data(
            current_block_id=block_id, current_exercise_id=ex_ids[0], last_by_exercise=last_by
        )
        await state.set_state(WorkoutFlow.logging_set)
    else:
        names = [(await db.get_exercise(eid))["display_name"] for eid in ex_ids]
        await state.update_data(
            current_block_id=block_id, current_exercise_id=None,
            superset_exercise_ids=ex_ids, superset_exercise_names=names,
            current_superset_idx=0, last_by_exercise=last_by,
        )
        await state.set_state(WorkoutFlow.logging_superset)

    await _render_logging_screen(callback.bot, state, user)
    await callback.answer()


# ---------- finishing the workout ----------

@router.callback_query(F.data == "live:finish_workout")
async def live_finish_workout(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    workout_id = data["workout_id"]
    exercise_ids = await db.list_exercise_ids_for_workout(workout_id)
    if not exercise_ids:
        await callback.message.edit_text(
            "Тренировка пустая — удалить её?",
            reply_markup=keyboards.yes_no_keyboard("finish:discard_empty", "live:cancel_finish"),
        )
        await callback.answer()
        return
    await callback.message.edit_text(
        "Завершаем? Можно добавить заметку (сон/самочувствие):",
        reply_markup=keyboards.finish_workout_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "finish:discard_empty")
async def finish_discard_empty(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await db.discard_workout(data["workout_id"])
    await state.clear()
    await callback.message.edit_text("Удалил пустую тренировку.")
    await _show_main_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == "live:cancel_finish")
async def cancel_finish(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user = await db.get_user(callback.from_user.id)
    in_progress = data.get("current_block_id") is not None
    if in_progress:
        await _render_logging_screen(callback.bot, state, user)
    else:
        await _refresh_live(
            callback.bot, data["live_chat_id"], data["live_message_id"], user, data["workout_id"],
            None, _idle_keyboard(data),
        )
    await callback.answer()


@router.callback_query(F.data == "finish:note")
async def finish_ask_note(callback: CallbackQuery, state: FSMContext):
    await state.set_state(WorkoutFlow.finishing_note)
    await callback.message.edit_text("Напиши заметку (сон, самочувствие, что угодно):")
    await callback.answer()


@router.message(StateFilter(WorkoutFlow.finishing_note))
async def finish_note_entered(message: Message, state: FSMContext):
    await _finalize_workout(message, state, note=message.text.strip())


@router.callback_query(F.data == "finish:skip_note")
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
    pr_lines: list[str] = []
    comparison_lines: list[str] = []

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
        for r in records:
            pr_lines.append(formatting.format_pr_line(ex["display_name"], r.kind, r.value, r.extra))

        if prior_sessions:
            comparison = analytics.compare_to_previous_session(prior_sessions + [new_session])
            if comparison and not new_session.is_bodyweight_mode:
                comparison_lines.append(
                    f"{ex['display_name']}: " + formatting.format_comparison_line(
                        comparison.e1rm_delta, comparison.tonnage_delta
                    )
                )

    await db.finish_workout(workout_id, note)

    blocks = await view_builder.build_block_views(workout_id, formula)
    finished_at = dt.datetime.now()
    summary = formatting.build_workout_summary(
        started_at, finished_at, blocks, note,
        hide_warmups=bool(user["hide_warmups"]), show_extra_stats=bool(user["show_extra_stats"]),
    )
    extra_parts = []
    if pr_lines:
        extra_parts.append("\n".join(pr_lines))
    if comparison_lines:
        extra_parts.append("\n".join(comparison_lines))
    full_text = summary + ("\n\n" + "\n\n".join(extra_parts) if extra_parts else "")

    try:
        await bot.edit_message_text(
            chat_id=data["live_chat_id"], message_id=data["live_message_id"], text=full_text
        )
    except TelegramBadRequest:
        await bot.send_message(chat_id=data["live_chat_id"], text=full_text)

    await state.clear()
    active = await db.get_active_workout(user_id)
    await bot.send_message(
        chat_id=data["live_chat_id"], text="Что дальше?", reply_markup=keyboards.main_menu(bool(active))
    )
