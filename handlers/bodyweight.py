"""⚖️ Вес тела — a lightweight bodyweight log with a trend chart.

Only body weight is tracked (no other measurements). Entries are timestamped
and stored in the user's current unit; switching units rescales them (see
handlers/settings.py). The screen shows the latest value, change since the
previous/first entry, and — once there are two points — a line chart.
"""

import asyncio
import datetime as dt
from contextlib import suppress

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InputMediaPhoto, Message

import charts
import db
import formatting
import keyboards
import ui
from fsm import BodyweightFlow
from parser import ParseError, bodyweight_warning, parse_bodyweight

router = Router(name="bodyweight")


def _window(logs: list, weeks: int) -> list:
    """Logs within the last `weeks` weeks (0 = all), for the chart window."""
    if weeks <= 0:
        return logs
    cutoff = (dt.date.today() - dt.timedelta(weeks=weeks)).isoformat()
    return [r for r in logs if r["logged_at"][:10] >= cutoff]


def _daily_average_points(logs: list) -> list[tuple[dt.datetime, float]]:
    """One point per day (averaging same-day entries), so logging weight
    several times a day doesn't turn the trend line into noise."""
    by_date: dict[dt.date, list[float]] = {}
    for r in logs:
        d = dt.datetime.fromisoformat(r["logged_at"]).date()
        by_date.setdefault(d, []).append(float(r["weight"]))
    return [(dt.datetime.combine(d, dt.time()), sum(ws) / len(ws)) for d, ws in sorted(by_date.items())]


async def _refresh_screen(message: Message, state: FSMContext, text: str, kb, png: bytes | None) -> None:
    """Show the bodyweight screen after a typed weight, editing the previous
    screen in place rather than deleting and resending it — only the user's
    typed message goes (see bw_weight_entered)."""
    data = await state.get_data()
    screen_id = data.get("bw_screen_id")
    bot = message.bot
    chat_id = message.chat.id

    if screen_id is not None:
        try:
            if png is None:
                await bot.edit_message_text(
                    text, chat_id=chat_id, message_id=screen_id, reply_markup=kb, parse_mode="HTML"
                )
            else:
                await bot.edit_message_media(
                    InputMediaPhoto(
                        media=BufferedInputFile(png, filename="bodyweight.png"),
                        caption=text,
                        parse_mode="HTML",
                    ),
                    chat_id=chat_id,
                    message_id=screen_id,
                    reply_markup=kb,
                )
            return  # screen_id is still current
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                return
            # Can't be edited in place (e.g. text <-> photo, or it's gone) — fall
            # back to delete-and-resend so the chat isn't left with a stale screen.
            with suppress(TelegramBadRequest):
                await bot.delete_message(chat_id=chat_id, message_id=screen_id)

    if png is None:
        sent = await message.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        photo = BufferedInputFile(png, filename="bodyweight.png")
        sent = await message.answer_photo(photo, caption=text, reply_markup=kb, parse_mode="HTML")
    await state.update_data(bw_screen_id=sent.message_id)


async def _render(event, state: FSMContext, user_id: int | None = None) -> None:
    """Render (or re-render) the bodyweight screen for a Message or CallbackQuery.

    user_id overrides `event.from_user.id` — needed when `event` is a bot-sent
    message reused only for its chat/bot handle (see bw_weight_confirm_yes,
    where the real message.from_user would be the bot itself, not the user)."""
    if user_id is None:
        user_id = event.from_user.id
    user = await db.get_user(user_id)
    logs = await db.list_bodyweight_logs(user_id)
    data = await state.get_data()
    weeks = data.get("bw_weeks", keyboards.DEFAULT_BODYWEIGHT_WEEKS)
    await state.set_state(BodyweightFlow.viewing)
    await state.update_data(bw_weeks=weeks)
    chart_logs = _window(logs, weeks)
    text = formatting.build_bodyweight_screen(logs, user["unit"], period_logs=chart_logs)
    show_periods = len(logs) >= 2
    kb = keyboards.bodyweight_keyboard(has_logs=bool(logs), weeks=weeks, show_periods=show_periods)

    png = None
    points = _daily_average_points(chart_logs)
    if len(points) >= 2:
        unit_label = formatting.UNIT_LABELS.get(user["unit"], "кг")
        png = await asyncio.to_thread(
            charts.render_metric_over_sessions, points, f"Вес тела, {unit_label}", unit_label
        )

    if not isinstance(event, CallbackQuery):
        # Edits the previous screen in place instead of deleting and resending it.
        await _refresh_screen(event, state, text, kb, png)
        return

    if png is None:
        sent = await ui.safe_edit(event, text, reply_markup=kb, parse_mode="HTML")
    else:
        sent = await ui.safe_edit_photo(
            event, png, "bodyweight.png", text, reply_markup=kb, parse_mode="HTML"
        )
    # Remembered so the next typed weight can be edited in place instead of
    # stacking another screen under it (see _refresh_screen).
    if sent is not None:
        await state.update_data(bw_screen_id=getattr(sent, "message_id", None))


async def show_bodyweight(callback: CallbackQuery, state: FSMContext) -> None:
    await db.get_or_create_user(callback.from_user.id, callback.from_user.username)
    await _render(callback, state)
    await callback.answer()


@router.callback_query(F.data == "menu:bodyweight")
async def menu_bodyweight(callback: CallbackQuery, state: FSMContext):
    await show_bodyweight(callback, state)


@router.callback_query(F.data == "bw:menu")
async def bw_menu(callback: CallbackQuery, state: FSMContext):
    from handlers.workout import _show_main_menu
    await _show_main_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data.startswith("bw:period:"))
async def bw_period(callback: CallbackQuery, state: FSMContext):
    weeks = int(callback.data.split(":")[2])
    await state.update_data(bw_weeks=weeks)
    await _render(callback, state)
    await callback.answer()


@router.callback_query(F.data == "bw:undo")
async def bw_undo(callback: CallbackQuery, state: FSMContext):
    removed = await db.delete_last_bodyweight(callback.from_user.id)
    await callback.answer("Удалил последнюю запись" if removed else "Нет записей")
    await _render(callback, state)


@router.message(StateFilter(BodyweightFlow.viewing), F.text)
async def bw_weight_entered(message: Message, state: FSMContext):
    try:
        weight = parse_bodyweight(message.text)
    except ParseError as e:
        await message.reply(e.message)
        return
    user = await db.get_user(message.from_user.id)
    warning = bodyweight_warning(weight, user["unit"])
    if warning is not None:
        # A soft nudge, not a reject (see parser.bodyweight_warning) — hold the
        # value in state and ask, same pattern as the suspicious-set-weight
        # confirm. "Исправить" just leaves the screen for a retype.
        await state.update_data(bw_pending_weight=weight)
        u = formatting.UNIT_LABELS.get(user["unit"], "кг")
        await message.reply(
            f"⚠️ {formatting.format_weight(weight)}{u}? {warning}\nЗаписываем?",
            reply_markup=keyboards.bodyweight_confirm_keyboard(),
        )
        return
    await db.add_bodyweight_log(message.from_user.id, weight)
    # The typed number itself is cleaned up so it doesn't clutter the chat;
    # the screen underneath is edited in place rather than deleted (see _render).
    with suppress(TelegramBadRequest):
        await message.delete()
    await _render(message, state)


@router.callback_query(StateFilter(BodyweightFlow.viewing), F.data == "bw:wconf:yes")
async def bw_weight_confirm_yes(callback: CallbackQuery, state: FSMContext):
    """Confirms a bodyweight entry flagged by bodyweight_warning — logs it as
    typed, no second-guessing beyond the one nudge already given."""
    data = await state.get_data()
    weight = data.get("bw_pending_weight")
    if weight is None:
        await callback.answer()
        return
    await db.add_bodyweight_log(callback.from_user.id, weight)
    await state.update_data(bw_pending_weight=None)
    confirm_message = callback.message
    with suppress(TelegramBadRequest):
        await confirm_message.delete()
    # confirm_message is a separate reply, not the tracked bw_screen_id — pass
    # it as a plain Message so _render takes the edit-in-place path for the
    # actual bodyweight screen instead of trying to reuse this one; user_id is
    # explicit because confirm_message.from_user would be the bot, not the user.
    await _render(confirm_message, state, user_id=callback.from_user.id)
    await callback.answer("Записал")


@router.callback_query(StateFilter(BodyweightFlow.viewing), F.data == "bw:wconf:no")
async def bw_weight_confirm_no(callback: CallbackQuery, state: FSMContext):
    """Throws the flagged entry away so the weight can simply be retyped."""
    await state.update_data(bw_pending_weight=None)
    with suppress(TelegramBadRequest):
        await callback.message.delete()
    await callback.answer("Не записал — пришли число ещё раз")
