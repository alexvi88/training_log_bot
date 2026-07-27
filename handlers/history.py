"""History browsing (§8) and progress/analytics screens (§7)."""

import asyncio
import datetime as dt
from contextlib import suppress
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import ai_trainer
import analytics
import charts
import config
import db
import formatting
import keyboards
import ui
import view_builder
from fsm import HistoryFlow, ProgressFlow

router = Router(name="history")

HISTORY_PAGE_SIZE = 8


# ---------- history ----------

async def show_history_list(callback: CallbackQuery, state: FSMContext, page: int):
    await state.set_state(HistoryFlow.browsing)
    await state.update_data(history_page=page)
    user_id = callback.from_user.id
    total = await db.count_workouts(user_id)
    workouts = await db.list_workouts(user_id, limit=HISTORY_PAGE_SIZE, offset=page * HISTORY_PAGE_SIZE)
    contents = await db.list_workout_contents([w["id"] for w in workouts])
    items = []
    entries = []
    for w in workouts:
        started = dt.datetime.fromisoformat(w["started_at"])
        names, set_count = contents.get(w["id"], ([], 0))
        items.append({"id": w["id"], "label": formatting.format_date_ru(started)})
        entries.append((started, names, set_count))
    has_next = (page + 1) * HISTORY_PAGE_SIZE < total
    kb = keyboards.history_list_keyboard(items, page, has_next)
    await ui.safe_edit(
        callback, formatting.build_history_list(entries), reply_markup=kb, parse_mode="HTML"
    )
    await callback.answer()


@router.message(StateFilter(HistoryFlow.browsing))
async def hist_search(message: Message, state: FSMContext):
    """Typing while browsing history filters it by exercise name.

    The list itself can only show dates — exercise names are far too long for a
    button label — so search is the only way to answer "в какой тренировке был
    жим" without opening sessions one by one.
    """
    query = message.text.strip() if message.text else ""
    with suppress(TelegramBadRequest):
        await message.delete()
    if not query:
        return
    workouts = await db.search_workouts_by_exercise(message.from_user.id, query)
    contents = await db.list_workout_contents([w["id"] for w in workouts])
    items = []
    entries = []
    for w in workouts:
        started = dt.datetime.fromisoformat(w["started_at"])
        names, set_count = contents.get(w["id"], ([], 0))
        items.append({"id": w["id"], "label": formatting.format_date_ru(started)})
        entries.append((started, names, set_count))
    kb = keyboards.history_list_keyboard(items, page=0, has_next=False)
    text = formatting.build_history_list(
        entries,
        header=f"🔎 <b>Тренировки с «{escape(query)}»: {len(entries)}</b>",
        footer="",
        empty=f"🔎 Ничего не нашёл по «{escape(query)}».",
    )
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("hist:page:"))
async def hist_page(callback: CallbackQuery, state: FSMContext):
    page = int(callback.data.split(":")[2])
    await show_history_list(callback, state, page)


@router.callback_query(F.data == "hist:back")
async def hist_back(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await show_history_list(callback, state, data.get("history_page", 0))


@router.callback_query(F.data == "hist:menu")
async def hist_to_menu(callback: CallbackQuery, state: FSMContext):
    from handlers.workout import _show_main_menu
    await _show_main_menu(callback, state)
    await callback.answer()


async def _top_lifts(user_id: int, formula: str) -> list[tuple[str, float, int, float]]:
    """Best working set per exercise, strongest first — for the Hall of Fame.

    Every exercise the user has ever logged gets a line, including bodyweight
    ones: those have no load to rank by, so their record is the best set of reps
    and they follow the weighted lifts (weight 0 marks them for the formatter).
    The list isn't capped here — build_hall_of_fame folds it and trims whatever
    doesn't fit the message.
    """
    weighted: list[tuple[str, float, int, float]] = []
    bodyweight: list[tuple[str, float, int, float]] = []

    # One query for every set the user owns, then grouped here — the per-exercise
    # version cost a round-trip per exercise ever created (see list_all_sets_by_exercise).
    by_exercise: dict[int, tuple[str, list[analytics.SetRow]]] = {}
    for r in await db.list_all_sets_by_exercise(user_id):
        entry = by_exercise.get(r["exercise_id"])
        if entry is None:
            entry = by_exercise[r["exercise_id"]] = (r["display_name"], [])
        entry[1].append(analytics.SetRow(r["weight"], r["reps"], r["workout_id"], r["started_at"]))

    for display_name, set_rows in by_exercise.values():
        sessions = analytics.group_sets_by_session(set_rows)
        for s in sessions:
            s.formula = formula
        pr = analytics.compute_personal_records(sessions)
        if pr.max_e1rm > 0 and pr.best_e1rm_weight > 0:
            weighted.append((display_name, pr.best_e1rm_weight, pr.best_e1rm_reps, pr.max_e1rm))
        elif pr.max_reps_at_weight:
            best_reps = max(pr.max_reps_at_weight.values())
            bodyweight.append((display_name, 0.0, best_reps, 0.0))
    weighted.sort(key=lambda t: t[3], reverse=True)
    bodyweight.sort(key=lambda t: t[2], reverse=True)
    return weighted + bodyweight


async def build_hall_of_fame_text(user_id: int, max_chars: int | None = None) -> str:
    user = await db.get_user(user_id)
    formula = user["e1rm_formula"] if user else config.DEFAULT_E1RM_FORMULA
    unit = user["unit"] if user else "kg"
    total_workouts = await db.count_workouts(user_id)
    agg = await db.hall_of_fame_aggregates(user_id)
    dates = [dt.date.fromisoformat(d) for d in await db.list_finished_workout_dates(user_id)]
    best_streak = analytics.max_week_streak(dates)
    top = await _top_lifts(user_id, formula)
    equivalent = formatting.format_tonnage_equivalent(agg["tonnage"], seed=user_id)
    return formatting.build_hall_of_fame(
        total_workouts=total_workouts,
        tonnage_kg=agg["tonnage"],
        tonnage_equivalent=equivalent,
        best_week_streak=best_streak,
        longest_workout_seconds=agg["longest_workout_seconds"],
        top_lifts=top,
        unit=unit,
        max_chars=max_chars,
    )


@router.callback_query(F.data == "menu:achievements")
async def menu_achievements(callback: CallbackQuery, state: FSMContext):
    """'🏆 Достижения' — reached from the Progress entry screen. Combines the
    old standalone Hall of Fame screen (lifetime totals, personal records)
    with the badge grid into one screen."""
    await state.clear()
    # Acknowledged up front: assembling this screen reads the user's whole set
    # history, and Telegram spins the tapped button until the callback is answered
    # (and gives up entirely after ~10s).
    await callback.answer()
    earned = await db.list_achievement_codes(callback.from_user.id)
    ach_text = formatting.build_achievements_screen(earned)
    # Records and badges share one message, and the record list is the open-ended
    # half (one line per exercise ever logged), so it gets whatever room the fixed
    # badge grid leaves — otherwise a long-time user's screen overflows the 4096
    # cap and ui.safe_edit deletes it without putting anything back.
    budget = formatting.MESSAGE_LIMIT - formatting.telegram_length(ach_text) - 2
    hof_text = await build_hall_of_fame_text(callback.from_user.id, max_chars=budget)
    text = hof_text + "\n\n" + ach_text
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="prog:groups")
    kb.adjust(1)
    await ui.safe_edit(callback, text, reply_markup=kb.as_markup(), parse_mode="HTML")


async def show_history_item(callback: CallbackQuery, workout_id: int) -> bool:
    """The history detail screen — same body as the just-finished completion
    card (sets, e1RM deltas, tonnage-equivalent, PR highlights, AI comment),
    so a past workout doesn't read as a stripped-down version of the one you
    just logged. See handlers.workout._finished_workout_card_text.
    """
    from handlers.workout import _finished_workout_card_text

    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != callback.from_user.id:
        await callback.answer("Тренировка не найдена", show_alert=True)
        return False
    user = await db.get_user(callback.from_user.id)
    comment = await ai_trainer.ensure_workout_comment(user, workout_id)
    text = await _finished_workout_card_text(workout, user, workout["note"], comment=comment)
    kb = keyboards.history_item_keyboard(
        workout_id, show_ai_button=comment is None and ai_trainer.is_configured()
    )
    await ui.safe_edit(callback, text, reply_markup=kb, parse_mode="HTML")
    return True


@router.callback_query(F.data.startswith("hist:item:"))
async def hist_item(callback: CallbackQuery, state: FSMContext):
    workout_id = int(callback.data.split(":")[2])
    if await show_history_item(callback, workout_id):
        await callback.answer()


@router.callback_query(F.data.startswith("hist:card:"))
async def hist_card(callback: CallbackQuery, state: FSMContext):
    """Shares the workout as a picture. This necessarily leaves the picture as
    the new bottom-of-chat message (a photo can't carry the text card's
    keyboard), so it gets its own caption and a way back to the text card,
    rather than landing as a bare, dead-end image."""
    workout_id = int(callback.data.split(":")[2])
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != callback.from_user.id:
        await callback.answer("Тренировка не найдена", show_alert=True)
        return
    user = await db.get_user(callback.from_user.id)
    blocks = await view_builder.build_block_views(workout_id, user["e1rm_formula"])
    started = dt.datetime.fromisoformat(workout["started_at"])
    title, body, footer, note = formatting.build_workout_card(
        started, blocks, workout["note"], unit=user["unit"]
    )
    png = await asyncio.to_thread(charts.render_workout_card, title, body, footer, note)
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад к тренировке", callback_data=f"hist:item:{workout_id}")
    kb.adjust(1)
    await callback.message.answer_photo(
        BufferedInputFile(png, filename="workout.png"),
        caption=f"{title} · {footer}",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("hist:edit:"))
async def hist_edit(callback: CallbackQuery, state: FSMContext):
    workout_id = int(callback.data.split(":")[2])
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != callback.from_user.id:
        await callback.answer("Тренировка не найдена", show_alert=True)
        return
    from handlers.edit_workout import show_edit_screen
    await show_edit_screen(callback, state, workout_id)
    await callback.answer()


_DELETE_CONFIRM_SUMMARY_MAX = 120


async def _delete_confirm_text(workout) -> str:
    """Names the workout about to be destroyed: date, duration and what was in it."""
    started = dt.datetime.fromisoformat(workout["started_at"])
    header = formatting.format_date_ru(started)
    duration = await view_builder.workout_duration_seconds(workout)
    if duration is not None:
        header += f" · {formatting.format_duration(duration)}"

    names: list[str] = []
    seen: set[int] = set()
    set_count = 0
    for block in await db.list_blocks_for_workout(workout["id"]):
        set_count += len(await db.list_sets_for_block(block["id"]))
        for be in await db.get_block_exercises(block["id"]):
            if be["exercise_id"] not in seen:
                seen.add(be["exercise_id"])
                names.append(be["display_name"])
    summary = ", ".join(names)
    if len(summary) > _DELETE_CONFIRM_SUMMARY_MAX:
        summary = summary[:_DELETE_CONFIRM_SUMMARY_MAX].rstrip(" ,") + "…"

    lines = [f"Удалить тренировку\n<b>{escape(header)}</b>"]
    if summary:
        set_word = formatting.plural_ru(set_count, ("сет", "сета", "сетов"))
        lines.append(f"<i>{escape(summary)} — {set_count} {set_word}</i>")
    lines.append("\nЭто действие нельзя отменить.")
    return "\n".join(lines)


@router.callback_query(F.data.startswith("hist:del:"))
async def hist_delete_confirm(callback: CallbackQuery, state: FSMContext):
    workout_id = int(callback.data.split(":")[2])
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != callback.from_user.id:
        await callback.answer("Тренировка не найдена", show_alert=True)
        return
    kb = keyboards.yes_no_keyboard(
        yes_cb=f"hist:delyes:{workout_id}",
        no_cb=f"hist:item:{workout_id}",
        yes_text="🗑 Удалить",
        no_text="❌ Отмена",
    )
    # safe_edit replaces the card being deleted, so the question has to carry the
    # date and contents itself — otherwise the one screen that identifies the
    # workout disappears exactly when the user is deciding whether to destroy it.
    await ui.safe_edit(callback, await _delete_confirm_text(workout), reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("hist:delyes:"))
async def hist_delete(callback: CallbackQuery, state: FSMContext):
    workout_id = int(callback.data.split(":")[2])
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != callback.from_user.id:
        await callback.answer("Тренировка не найдена", show_alert=True)
        return
    await db.discard_workout(workout_id)
    data = await state.get_data()
    await show_history_list(callback, state, data.get("history_page", 0))
    await callback.answer("Тренировка удалена.")


# ---------- progress ----------

async def show_progress_entry(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ProgressFlow.picking_group)
    groups = await db.list_muscle_groups(callback.from_user.id)
    kb = keyboards.groups_keyboard(
        groups, prefix="prog",
        extra_buttons=[("🏆 Достижения", "menu:achievements"), ("⬅️ Назад", "prog:back")],
        show_all=True,
    )
    text = "📈 Прогресс — выбери группу мышц или найди упражнение по названию:"
    await ui.safe_edit(callback, text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "prog:back")
async def prog_back(callback: CallbackQuery, state: FSMContext):
    from handlers.workout import _show_main_menu
    await _show_main_menu(callback, state)


@router.callback_query(F.data == "prog:groups")
async def prog_back_to_groups(callback: CallbackQuery, state: FSMContext):
    await show_progress_entry(callback, state)


async def _render_progress_exercise_list(callback: CallbackQuery, state: FSMContext, raw: str, page: int) -> None:
    await state.set_state(ProgressFlow.picking_exercise)
    group_id = None if raw == "all" else int(raw)
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

    b = InlineKeyboardBuilder()
    for ex in exercises:
        b.row(InlineKeyboardButton(text=ex["display_name"], callback_data=f"prog:ex:{ex['id']}:{raw}"))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"prog:gpage:{raw}:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"prog:gpage:{raw}:{page + 1}"))
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="prog:groups"))
    if exercises:
        text = "📈 Прогресс — выбери упражнение или напиши название для поиска:"
    else:
        text = "Пока нет своих упражнений с историей в этой группе. Можно написать название для поиска."
    await ui.safe_edit(callback, text, reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("prog:grp:"))
async def prog_pick_group(callback: CallbackQuery, state: FSMContext):
    raw = callback.data.split(":")[2]
    await _render_progress_exercise_list(callback, state, raw, page=0)
    await callback.answer()


@router.callback_query(F.data.startswith("prog:gpage:"))
async def prog_group_page(callback: CallbackQuery, state: FSMContext):
    _, _, raw, page_str = callback.data.split(":")
    await _render_progress_exercise_list(callback, state, raw, page=int(page_str))
    await callback.answer()


@router.message(StateFilter(ProgressFlow.picking_group, ProgressFlow.picking_exercise))
async def prog_search_text(message: Message, state: FSMContext):
    """Typing while browsing Progress searches the user's own exercises instead
    of falling through to the fallback router's "Не понял" — same pattern as
    the workout picker and ⚙️ Упражнения, minus template suggestions: Progress
    is about history you already have, and a never-trained template would just
    show "Пока нет завершённых тренировок с этим упражнением" if picked."""
    query = message.text.strip()
    if not query:
        return
    results = await db.search_exercises(message.from_user.id, query)
    b = InlineKeyboardBuilder()
    for ex in results:
        b.row(InlineKeyboardButton(text=ex["display_name"], callback_data=f"prog:ex:{ex['id']}:all"))
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="prog:groups"))
    text = f"Результаты поиска «{escape(query)}»:" if results else f"Ничего не нашлось по «{escape(query)}»."
    await state.set_state(ProgressFlow.picking_exercise)
    await message.answer(text, reply_markup=b.as_markup(), parse_mode="HTML")


async def _load_sessions(exercise_id: int, formula: str) -> list[analytics.SessionStats]:
    rows = await db.list_sets_for_exercise(exercise_id)
    set_rows = [
        analytics.SetRow(r["weight"], r["reps"], r["workout_id"], r["started_at"], r["rpe"])
        for r in rows
    ]
    sessions = analytics.group_sets_by_session(set_rows)
    for s in sessions:
        s.formula = formula
    return sessions


# Cached (text, png) per period for whatever exercise a user is currently
# looking at — one entry per user, not one per (user, exercise), so rapidly
# flipping 8 -> 20 -> 8 back and forth skips the matplotlib re-render and
# re-encode (the expensive part of this screen) instead of growing unbounded
# over every exercise ever viewed. The fingerprint hashes every set of every
# session (not just the latest one), so editing or deleting a set anywhere in
# the exercise's history — not only appending a new one — invalidates it; this
# is cheap since `sessions` is already loaded for the text/analytics below,
# same pattern as _menu_view's heatmap cache in handlers/workout.py.
_progress_view_cache: dict[int, tuple[int, tuple, dict[int, tuple[str, bytes | None]]]] = {}


async def _render_progress_view(ex_id: int, user, limit: int, origin: str = "all"):
    """Build the text/chart/keyboard for an exercise's progress screen.

    PRs always look at the full history, so switching periods doesn't change
    what counts as a record. The headline delta and the chart both scope to
    the selected `limit` instead, so they stay consistent with each other and
    with what's actually plotted.
    """
    ex = await db.get_exercise(ex_id)
    sessions = await _load_sessions(ex_id, user["e1rm_formula"])
    fingerprint = (
        tuple(
            (s.started_at, tuple((r.weight, r.reps, r.rpe) for r in s.sets))
            for s in sessions
        ),
        user["e1rm_formula"], user["unit"],
    )
    cached_user = _progress_view_cache.get(user["telegram_id"])
    by_limit = (
        cached_user[2] if cached_user and cached_user[:2] == (ex_id, fingerprint) else {}
    )

    cached = by_limit.get(limit)
    if cached is not None:
        text, png = cached
    else:
        points: list[tuple[dt.datetime, float]] = []
        if sessions:
            is_bw = sessions[-1].is_bodyweight_mode
            points = [
                (dt.datetime.fromisoformat(s.started_at), float(s.max_reps_in_set if is_bw else s.top_e1rm))
                for s in sessions
            ]
        comparison = analytics.compare_to_previous_session(sessions)
        records = analytics.compute_personal_records(sessions)

        text = formatting.format_progress_screen(
            ex["display_name"], sessions, comparison, records, limit=limit, unit=user["unit"]
        )

        png = None
        if sessions:
            metric = "повторы" if sessions[-1].is_bodyweight_mode else "e1RM"
            png = await asyncio.to_thread(
                charts.render_metric_over_sessions,
                points[-limit:],
                f"{ex['display_name']} — {metric}",
                metric,
                show_weekly_rate=False,
            )
        by_limit[limit] = (text, png)
        _progress_view_cache[user["telegram_id"]] = (ex_id, fingerprint, by_limit)

    kb = (
        keyboards.progress_chart_keyboard(ex_id, limit, origin)
        if sessions
        else keyboards.progress_back_keyboard(ex_id, origin)
    )
    return text, png, kb


@router.callback_query(F.data.startswith("prog:ex:"))
async def prog_show_exercise(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    ex_id = int(parts[2])
    origin = parts[3] if len(parts) > 3 else "all"
    await state.update_data(prog_exercise_id=ex_id, prog_origin=origin)
    user = await db.get_user(callback.from_user.id)
    text, png, kb = await _render_progress_view(ex_id, user, keyboards.DEFAULT_PROGRESS_LIMIT, origin)

    if png:
        await ui.safe_edit_photo(callback, png, "chart.png", text, reply_markup=kb, parse_mode="HTML")
    else:
        await ui.safe_edit(callback, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("prog:per:"))
async def prog_change_period(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    ex_id, limit = int(parts[2]), int(parts[3])
    origin = parts[4] if len(parts) > 4 else "all"
    user = await db.get_user(callback.from_user.id)
    text, png, kb = await _render_progress_view(ex_id, user, limit, origin)

    # Same path as prog_show_exercise: edit_media directly would strand the chart
    # above anything that landed under it (a push, the user's own message), since
    # Telegram can't move an edited message back down — that's what safe_edit_photo
    # exists to decide.
    if png:
        await ui.safe_edit_photo(callback, png, "chart.png", text, reply_markup=kb, parse_mode="HTML")
    else:
        await ui.safe_edit(callback, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()
