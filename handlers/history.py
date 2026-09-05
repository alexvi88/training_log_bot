"""History browsing (§8) and progress/analytics screens (§7)."""

import asyncio
import datetime as dt
from contextlib import suppress
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import achievement_sync
import acquisition
import ai_trainer
import analytics
import charts
import config
import db
import formatting
import i18n
import keyboards
import state_scaffold
import timeutil
import ui
import view_builder
from fsm import HistoryFlow, ProgressFlow
from handlers import sharing

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


HISTORY_SEARCH_PAGE_SIZE = 20


async def _render_search_page(user_id: int, query: str, page: int):
    """Одна страница поиска по истории — общий кусок для первого показа и
    перелистывания (hist:spage:N), чтобы старые тренировки частого упражнения
    оставались достижимы, а не терялись за первыми 20 совпадениями."""
    offset = page * HISTORY_SEARCH_PAGE_SIZE
    workouts = await db.search_workouts_by_exercise(
        user_id, query, limit=HISTORY_SEARCH_PAGE_SIZE, offset=offset
    )
    contents = await db.list_workout_contents([w["id"] for w in workouts])
    items = []
    entries = []
    for w in workouts:
        started = dt.datetime.fromisoformat(w["started_at"])
        names, set_count = contents.get(w["id"], ([], 0))
        items.append({"id": w["id"], "label": formatting.format_date_ru(started)})
        entries.append((started, names, set_count))
    total = await db.count_workouts_by_exercise(user_id, query)
    shown_so_far = offset + len(entries)
    has_next = shown_so_far < total
    count_label = str(total) if shown_so_far >= total else i18n.t("history.shown_of_total", shown=shown_so_far, total=total)
    kb = keyboards.history_search_keyboard(items, page, has_next)
    text = formatting.build_history_list(
        entries,
        header=i18n.t("history.search_header", query=escape(query), count=count_label),
        footer="",
        empty=i18n.t("history.search_empty", query=escape(query)),
    )
    return text, kb


@router.message(StateFilter(HistoryFlow.browsing), F.text)
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
    await state.update_data(history_search_query=query)
    text, kb = await _render_search_page(message.from_user.id, query, page=0)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(StateFilter(HistoryFlow.browsing), F.data.startswith("hist:spage:"))
async def hist_search_page(callback: CallbackQuery, state: FSMContext):
    page = int(callback.data.split(":")[2])
    query = (await state.get_data()).get("history_search_query", "")
    if not query:
        await callback.answer()
        return
    text, kb = await _render_search_page(callback.from_user.id, query, page)
    await ui.safe_edit(callback, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


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
        entry[1].append(analytics.SetRow(db.load_of(r), r["reps"], r["workout_id"], r["started_at"]))

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
    equivalent = formatting.format_tonnage_equivalent(agg["tonnage"], seed=user_id, unit=unit)
    tonnage_kg = formatting.to_kg(agg["tonnage"], unit)
    per_week = analytics.workouts_per_week(dates, timeutil.user_today(user))
    rank = analytics.rank_for(total_workouts, tonnage_kg, per_week)
    return formatting.build_hall_of_fame(
        total_workouts=total_workouts,
        tonnage_kg=agg["tonnage"],
        tonnage_equivalent=equivalent,
        best_week_streak=best_streak,
        longest_workout_seconds=await view_builder.longest_workout_seconds(user_id),
        top_lifts=top,
        unit=unit,
        rank=rank,
        rank_gap=analytics.rank_gap(rank, total_workouts, tonnage_kg, per_week),
        max_chars=max_chars,
    )


@router.callback_query(F.data == "prog:week")
async def prog_week(callback: CallbackQuery, state: FSMContext):
    """Недельная сводка: настоящей таблицей там, где Telegram её умеет.

    Rich-сообщения появились в Bot API 10.1, поэтому таблица — попытка, а не
    гарантия: если сервер или клиент её не знает, тот же самый набор чисел
    уходит обычным текстом. Текстовая версия не «урезанная», она полная —
    таблица лишь читается с одного взгляда.
    """
    user_id = callback.from_user.id
    user = await db.get_user(user_id)
    today = timeutil.user_today(user)
    monday = today - dt.timedelta(days=today.weekday())
    since = f"{monday.isoformat()}T00:00:00"

    rows = [
        formatting.WeeklyRow(
            name=r["name"], top_weight=r["top_weight"],
            tonnage=r["tonnage"], sets_count=r["sets_count"],
        )
        for r in await db.weekly_exercise_rollup(user_id, since)
    ]
    dates = [dt.date.fromisoformat(d) for d in await db.list_finished_workout_dates(user_id)]
    workouts = sum(1 for d in dates if d >= monday)
    # По всем упражнениям недели, а не по показанным: сводка сама режет список
    # до formatting.WEEKLY_ROWS_LIMIT строк, и итог, посчитанный по обрезку, был
    # меньше правды и расходился с плиткой тоннажа на дашборде.
    total = sum(r.tonnage for r in rows)
    period = f"{monday.strftime('%d.%m')}–{(monday + dt.timedelta(days=6)).strftime('%d.%m')}"
    text = formatting.build_weekly_summary(
        rows, workouts, total, period, unit=user["unit"]
    )
    kb = keyboards.back_keyboard("menu:progress")

    table = formatting.build_weekly_table(rows, unit=user["unit"])
    if table is not None and await _send_rich_weekly(callback, text, table, kb):
        await callback.answer()
        return
    await ui.safe_edit(callback, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


async def _send_rich_weekly(callback: CallbackQuery, text: str, table, kb) -> bool:
    """Попробовать отправить сводку rich-сообщением. False — значит не вышло, и
    вызывающая сторона шлёт текст."""
    from aiogram.types import InputRichBlockParagraph, InputRichMessage

    # Блоки несут обычный текст, не HTML-разметку: заголовок для них чистится
    # от тегов, которые нужны только текстовому фолбэку.
    heading = formatting.strip_tags(text.partition("\n\n")[0])
    try:
        await callback.message.answer_rich(
            rich_message=InputRichMessage(
                blocks=[InputRichBlockParagraph(text=heading), table]
            ),
            reply_markup=kb,
        )
    except (TelegramAPIError, AttributeError, TypeError):
        # Сервер/клиент ниже 10.1, старый aiogram — что угодно: молча уходим в текст.
        return False
    with suppress(TelegramBadRequest):
        await callback.message.delete()
    return True


@router.callback_query(F.data.startswith("menu:achievements"))
async def menu_achievements(callback: CallbackQuery, state: FSMContext):
    """'🏆 Достижения' — экран зала славы и значков одним куском.

    Входов два: главное меню и экран прогресса, — и «назад» обязано вести туда,
    откуда пришли. Раньше оно всегда уводило в прогресс, и человек, открывший
    достижения из меню, оказывался на экране, которого не открывал. Откуда
    пришли, помним хвостом callback_data, а не состоянием: кнопка живёт в чате
    вечно, и состояние к моменту тапа может быть уже любым.
    """
    # Экран смотрят и посреди тренировки («сколько мне до значка»), поэтому
    # снимается только поток: каркас открытых упражнений должен уцелеть.
    await state_scaffold.clear_state_keep_workout(state)
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
    from_progress = callback.data.endswith(":prog")
    # С карточки законченной тренировки экран приходит ОТДЕЛЬНЫМ сообщением:
    # карточка — единственное место, где тренировка показана целиком, и удалять
    # её под достижениями значит терять итог навсегда. Из меню и из прогресса
    # экран по-прежнему заменяет собой предыдущий.
    from_card = callback.data.endswith(":card")
    kb = InlineKeyboardBuilder()
    kb.button(text=i18n.t("history.ranks_button"), callback_data="rank:ladder" + (":prog" if from_progress else ""))
    # hist:menu — существующая ручка «в главное меню» (hist_to_menu ниже зовёт
    # _show_main_menu); отдельной заводить незачем, а свежая строка вроде
    # «menu:back» была бы кнопкой, которую никто не слушает.
    kb.button(text=i18n.t("btn.back"), callback_data="prog:groups" if from_progress else "hist:menu")
    kb.adjust(1)
    await ui.safe_edit(
        callback, text, reply_markup=kb.as_markup(), parse_mode="HTML", delete=not from_card
    )


@router.callback_query(F.data.startswith("rank:ladder"))
async def rank_ladder(callback: CallbackQuery, state: FSMContext):
    """«🎖 Звания» — вся лестница с порогами и правилом, по которому она считается.

    Звание до этого показывалось в трёх местах и нигде не объяснялось: плашка на
    сводке, строка в зале славы и разовое объявление на карточке. Из этого видно,
    что система есть, и не видно, какая она и докуда идёт, — а непонятная система
    мотивирует хуже отсутствующей.
    """
    await callback.answer()
    user = await db.get_user(callback.from_user.id)
    dates = [dt.date.fromisoformat(d) for d in await db.list_finished_workout_dates(callback.from_user.id)]
    agg = await db.hall_of_fame_aggregates(callback.from_user.id)
    tonnage_kg = formatting.to_kg(agg["tonnage"], user["unit"])
    per_week = analytics.workouts_per_week(dates, timeutil.user_today(user))
    rank = analytics.rank_for(len(dates), tonnage_kg, per_week)
    text = formatting.build_rank_ladder(
        analytics.RANKS, rank,
        analytics.rank_gap(rank, len(dates), tonnage_kg, per_week),
        total_workouts=len(dates),
        tonnage_kg=tonnage_kg,
        per_week=per_week,
    )
    kb = InlineKeyboardBuilder()
    kb.button(
        text=i18n.t("btn.back"),
        callback_data="menu:achievements" + (":prog" if callback.data.endswith(":prog") else ""),
    )
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
        await ui.alert_workout_not_found(callback)
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
        await ui.alert_workout_not_found(callback)
        return
    user = await db.get_user(callback.from_user.id)
    blocks = await view_builder.build_block_views(
        workout_id, user["e1rm_formula"], mark_records=True
    )
    started = dt.datetime.fromisoformat(workout["started_at"])
    title, body, footer, note = formatting.build_workout_card(
        started, blocks, workout["note"], unit=user["unit"]
    )
    png = await asyncio.to_thread(charts.render_workout_card, title, body, footer, note)
    kb = InlineKeyboardBuilder()
    # URL-кнопка стоит первой и переживает пересылку — в отличие от callback'ов
    # (тот же приём, что у визиток, см. handlers/sharing.py). Картинку уносят в
    # чат с друзьями, и там она перестаёт быть просто картинкой: по ссылке в ней
    # видно, кто привёл человека (acquisition.SOURCE_REFERRAL).
    link = acquisition.referral_link(await sharing.get_bot_username(callback.bot), callback.from_user.id)
    kb.button(text=i18n.t("history.share_card_cta"), url=link)
    # Вторая кнопка — та же ссылка, но копией: картинку пересылают куда угодно,
    # не только тапом по URL-кнопке — в чат, где ссылки превью не разворачивают,
    # или в заметки себе, — и там её нужно вставить самому.
    kb.button(text=i18n.t("history.share_card_copy"), copy_text=CopyTextButton(text=link))
    kb.button(text=i18n.t("history.back_to_workout_button"), callback_data=f"hist:item:{workout_id}")
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
        await ui.alert_workout_not_found(callback)
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

    lines = [f"{i18n.t('history.delete_confirm_title')}\n<b>{escape(header)}</b>"]
    if summary:
        # TONE_OF_VOICE.md: «подход», не «сет» — запрещённое слово словаря
        # (в английском наоборот — "set" законное слово, см. English voice).
        lines.append(f"<i>{escape(summary)} — {i18n.t('history.sets_count', n=set_count)}</i>")
    lines.append(f"\n{i18n.t('history.delete_confirm_warning')}")
    return "\n".join(lines)


@router.callback_query(F.data.startswith("hist:del:"))
async def hist_delete_confirm(callback: CallbackQuery, state: FSMContext):
    workout_id = int(callback.data.split(":")[2])
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != callback.from_user.id:
        await ui.alert_workout_not_found(callback)
        return
    kb = keyboards.yes_no_keyboard(
        yes_cb=f"hist:delyes:{workout_id}",
        no_cb=f"hist:item:{workout_id}",
        yes_text=i18n.t("btn.delete"),
        no_text=i18n.t("btn.cancel"),
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
        await ui.alert_workout_not_found(callback)
        return
    await db.discard_workout(workout_id)
    # Deleting the workout has to delete what it earned too: a set typed as 500кг
    # unlocks the weight clubs, and leaving those badges behind would keep the
    # mistake visible in the grid forever, with no workout left to fix or remove.
    await achievement_sync.resync(callback.from_user.id)
    data = await state.get_data()
    await show_history_list(callback, state, data.get("history_page", 0))
    await callback.answer(i18n.t("history.deleted_toast"))


# ---------- progress ----------

async def show_progress_entry(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ProgressFlow.picking_group)
    if await db.count_workouts(callback.from_user.id) == 0:
        # Without this, a new user picks a group, sees "пусто", and backs out —
        # the picker itself can't tell them that until they've already drilled in.
        kb = InlineKeyboardBuilder()
        kb.button(text=i18n.t("history.start_workout_button"), callback_data="menu:start_workout")
        kb.button(text=i18n.t("btn.back"), callback_data="prog:back")
        kb.adjust(1)
        await ui.safe_edit(
            callback,
            i18n.t("history.progress_empty"),
            reply_markup=kb.as_markup(),
        )
        await callback.answer()
        return
    groups = await db.list_muscle_groups(callback.from_user.id)
    kb = keyboards.groups_keyboard(
        groups, prefix="prog",
        extra_buttons=[
            (i18n.t("history.week_button"), "prog:week"),
            (i18n.t("btn.achievements"), "menu:achievements:prog"),
            (i18n.t("btn.back"), "prog:back"),
        ],
        show_all=True,
    )
    text = i18n.t("history.progress_intro")
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
    b.row(InlineKeyboardButton(text=i18n.t("btn.back"), callback_data="prog:groups"))
    # Тот же приём, что у экранов групп и списков упражнений в других
    # разделах — «или просто напиши название, например «жим»» вместо
    # суховатого «для поиска».
    text = (
        i18n.t("history.progress_pick_exercise")
        if exercises
        else i18n.t("history.progress_no_history_in_group")
    )
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


@router.message(StateFilter(ProgressFlow.picking_group, ProgressFlow.picking_exercise), F.text)
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
    b.row(InlineKeyboardButton(text=i18n.t("btn.back"), callback_data="prog:groups"))
    text = (
        i18n.t("history.search_results_for", query=escape(query))
        if results
        else i18n.t("history.search_no_results_for", query=escape(query))
    )
    await state.set_state(ProgressFlow.picking_exercise)
    await message.answer(text, reply_markup=b.as_markup(), parse_mode="HTML")


async def _load_sessions(exercise_id: int, formula: str) -> list[analytics.SessionStats]:
    rows = await db.list_sets_for_exercise(exercise_id)
    set_rows = [
        analytics.SetRow(db.load_of(r), r["reps"], r["workout_id"], r["started_at"], r["rpe"])
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
    session_notes = await db.list_workout_notes_for_exercise(ex_id)
    fingerprint = (
        tuple(
            (s.started_at, tuple((r.weight, r.reps, r.rpe) for r in s.sets))
            for s in sessions
        ),
        user["e1rm_formula"], user["unit"], tuple(sorted(session_notes.items())),
    )
    cached_user = _progress_view_cache.get(user["telegram_id"])
    by_limit = (
        cached_user[2] if cached_user and cached_user[:2] == (ex_id, fingerprint) else {}
    )

    cached = by_limit.get(limit)
    if cached is not None:
        text, png = cached
    else:
        # У упражнения, сменившего режим (подтягивания с весом → своим весом),
        # в истории живут две несопоставимые величины: килограммы e1RM и голые
        # повторы. На одной оси они читаются как обвал силы — 110 и 12 рядом.
        # Поэтому график остаётся про одну величину, а сессии другого режима в
        # него просто не попадают: рекорды обоих режимов всё равно показаны
        # текстом выше (formatting.format_progress_screen).
        chart_is_bw = sessions[-1].is_bodyweight_mode if sessions else False
        plotted = [s for s in sessions if s.is_bodyweight_mode == chart_is_bw]
        points: list[tuple[dt.datetime, float]] = [
            (
                dt.datetime.fromisoformat(s.started_at),
                float(s.max_reps_in_set if chart_is_bw else s.top_e1rm),
            )
            for s in plotted
        ]
        comparison = analytics.compare_to_previous_session(sessions)
        records = analytics.compute_personal_records(sessions)

        text = formatting.format_progress_screen(
            ex["display_name"], sessions, comparison, records, limit=limit, unit=user["unit"],
            session_notes=session_notes,
            golds=analytics.gold_book(sessions, user["e1rm_formula"]),
        )

        png = None
        if points:
            # Уезжает в пиксели графика (charts.render_metric_over_sessions рисует
            # заголовок/ось matplotlib'ом, никакой текстовый тест кириллицу там не
            # увидит) — переводим явно, а не полагаемся на formatting.plural_ru.
            metric = i18n.t("history.chart_metric_reps") if chart_is_bw else "e1RM"
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
    # Кнопка живёт в чате вечно, а упражнение за это время могли удалить: тап по
    # старой карточке падал с TypeError внутри рендера, и человек видел только
    # крутящуюся кнопку, которая ничем не кончается.
    if await db.get_exercise(ex_id) is None:
        await ui.alert_exercise_not_found(callback)
        return
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
