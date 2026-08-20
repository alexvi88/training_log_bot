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
import i18n
import keyboards
import timeutil
import ui
from fsm import BodyweightFlow
from parser import ParseError, bodyweight_warning, parse_bodyweight_entry

router = Router(name="bodyweight")


# Guards "bw:wconf:yes" against a double tap: bw_pending_weight is only cleared
# after db.add_bodyweight_log, and two callbacks from the same user can run
# concurrently, each reading its own snapshot of state with the pending weight
# still set — so the second tap would log the same weight a second time. Same
# shape as handlers.workout._confirming / _try_claim_weight_confirm.
_confirming: set[int] = set()


def _try_claim_confirming(user_id: int) -> bool:
    """Atomically check-and-reserve `_confirming` for this user — no `await`
    between the membership check and the `.add()`, same reasoning as
    ai_trainer._try_claim_busy."""
    if user_id in _confirming:
        return False
    _confirming.add(user_id)
    return True


def _window(logs: list, weeks: int, today: dt.date) -> list:
    """Logs within the last `weeks` weeks (0 = all), for the chart window.

    `today` is the caller's user-local "today" (timeutil.user_today) — the
    server's own UTC date would cut the window at the wrong hour for anyone
    with a non-zero tz_offset, same as backfill/calendars.
    """
    if weeks <= 0:
        return logs
    cutoff = (today - dt.timedelta(weeks=weeks)).isoformat()
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
    chart_logs = _window(logs, weeks, timeutil.user_today(user))
    text = formatting.build_bodyweight_screen(logs, user["unit"], period_logs=chart_logs)
    show_periods = len(logs) >= 2
    kb = keyboards.bodyweight_keyboard(has_logs=bool(logs), weeks=weeks, show_periods=show_periods)

    png = None
    points = _daily_average_points(chart_logs)
    if len(points) >= 2:
        unit_label = formatting.unit_label(user["unit"])
        # Заголовок и подпись оси уходят в картинку (charts.render_metric_over_sessions
        # рисует их matplotlib'ом), а не текстом на экране — поэтому оба берутся
        # из каталога, а не собираются f-строкой с русским текстом внутри: иначе
        # англоязычный с любой единицей всё равно видел бы кириллицу на графике.
        png = await asyncio.to_thread(
            charts.render_metric_over_sessions,
            points,
            i18n.t("bodyweight.chart_title", u=unit_label),
            unit_label,
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
    await db.get_or_create_user(
        callback.from_user.id, callback.from_user.username, callback.from_user.language_code
    )
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


async def _render_list(callback: CallbackQuery, state: FSMContext, page: int) -> None:
    """Отрисовать (или перерисовать) страницу «✏️ Записи»."""
    user_id = callback.from_user.id
    user = await db.get_user(user_id)
    size = keyboards.BODYWEIGHT_LIST_PAGE_SIZE
    total = await db.count_bodyweight_logs(user_id)
    rows = await db.list_bodyweight_logs_page(user_id, limit=size, offset=page * size)
    text = formatting.build_bodyweight_list_screen(rows, user["unit"], page, size, total)
    kb = keyboards.bodyweight_list_keyboard(
        [r["id"] for r in rows], page, has_next=(page + 1) * size < total
    )
    await state.set_state(BodyweightFlow.browsing)
    await ui.safe_edit(callback, text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("bw:list:"))
async def bw_list(callback: CallbackQuery, state: FSMContext):
    page = int(callback.data.split(":")[2])
    await _render_list(callback, state, page)
    await callback.answer()


@router.callback_query(F.data.startswith("bw:delrec:"))
async def bw_delete_record(callback: CallbackQuery, state: FSMContext):
    """Удалить любую запись из списка — не только последнюю (см. модуль).

    Без StateFilter нарочно: экран «✏️ Записи» может пролежать в чате сколько
    угодно, пока человек заглянет ещё куда-то и вернётся — тот же довод, что у
    fd:delask в handlers/food_diary.py.
    """
    _, _, log_id_s, page_s = callback.data.split(":")
    log_id, page = int(log_id_s), int(page_s)
    removed = await db.delete_bodyweight_log(log_id, callback.from_user.id)
    await callback.answer(i18n.t("bodyweight.entry_deleted") if removed else i18n.t("bodyweight.entry_not_found"))
    # Если это была последняя запись на странице — а страница не первая,
    # съезжаем на предыдущую, чтобы не остаться на пустом экране.
    size = keyboards.BODYWEIGHT_LIST_PAGE_SIZE
    total = await db.count_bodyweight_logs(callback.from_user.id)
    if page > 0 and page * size >= total:
        page -= 1
    await _render_list(callback, state, page)


def _logged_at_for(date: dt.date | None) -> str | None:
    """Метка времени для взвешивания задним числом. Полдень, а не полночь: экран
    и график режут записи по дате, и середина дня не свалится в соседние сутки
    ни при каком часовом поясе."""
    if date is None:
        return None
    return dt.datetime.combine(date, dt.time(12, 0)).isoformat()


@router.message(StateFilter(BodyweightFlow.viewing), F.text)
async def bw_weight_entered(message: Message, state: FSMContext):
    user = await db.get_user(message.from_user.id)
    try:
        weight, date = parse_bodyweight_entry(
            message.text, today=timeutil.user_today(user), unit=user["unit"]
        )
    except ParseError as e:
        await ui.reply_transient(message, e.message)
        return
    warning = bodyweight_warning(weight, user["unit"])
    if warning is not None:
        # A soft nudge, not a reject (see parser.bodyweight_warning) — hold the
        # value in state and ask, same pattern as the suspicious-set-weight
        # confirm. "Исправить" just leaves the screen for a retype.
        await state.update_data(
            bw_pending_weight=weight, bw_pending_date=date.isoformat() if date else None
        )
        u = formatting.unit_label(user["unit"])
        await message.reply(
            i18n.t(
                "bodyweight.confirm_prompt",
                weight=formatting.format_weight(weight),
                u=u,
                warning=warning,
            ),
            reply_markup=keyboards.bodyweight_confirm_keyboard(),
        )
        return
    await db.add_bodyweight_log(message.from_user.id, weight, _logged_at_for(date))
    # The typed number itself is cleaned up so it doesn't clutter the chat;
    # the screen underneath is edited in place rather than deleted (see _render).
    with suppress(TelegramBadRequest):
        await message.delete()
    await _render(message, state)


@router.callback_query(StateFilter(BodyweightFlow.viewing), F.data == "bw:wconf:yes")
async def bw_weight_confirm_yes(callback: CallbackQuery, state: FSMContext):
    """Confirms a bodyweight entry flagged by bodyweight_warning — logs it as
    typed, no second-guessing beyond the one nudge already given.

    Claims `_confirming` before the first `await`: two fast taps on "Да" both
    read `bw_pending_weight` while it's still set (each holds its own
    snapshot), so without this guard both would call db.add_bodyweight_log —
    clearing the pending value only stops a *later* tap, not a second one
    racing the first.
    """
    user_id = callback.from_user.id
    if not _try_claim_confirming(user_id):
        await callback.answer()
        return
    try:
        data = await state.get_data()
        weight = data.get("bw_pending_weight")
        if weight is None:
            await callback.answer()
            return
        raw_date = data.get("bw_pending_date")
        date = dt.date.fromisoformat(raw_date) if raw_date else None
        await db.add_bodyweight_log(user_id, weight, _logged_at_for(date))
        await state.update_data(bw_pending_weight=None, bw_pending_date=None)
        confirm_message = callback.message
        with suppress(TelegramBadRequest):
            await confirm_message.delete()
        # confirm_message is a separate reply, not the tracked bw_screen_id — pass
        # it as a plain Message so _render takes the edit-in-place path for the
        # actual bodyweight screen instead of trying to reuse this one; user_id is
        # explicit because confirm_message.from_user would be the bot, not the user.
        await _render(confirm_message, state, user_id=user_id)
        await callback.answer(i18n.t("bodyweight.confirmed_logged"))
    finally:
        _confirming.discard(user_id)


@router.callback_query(StateFilter(BodyweightFlow.viewing), F.data == "bw:wconf:no")
async def bw_weight_confirm_no(callback: CallbackQuery, state: FSMContext):
    """Throws the flagged entry away so the weight can simply be retyped."""
    await state.update_data(bw_pending_weight=None, bw_pending_date=None)
    with suppress(TelegramBadRequest):
        await callback.message.delete()
    await callback.answer(i18n.t("bodyweight.confirm_declined"))
