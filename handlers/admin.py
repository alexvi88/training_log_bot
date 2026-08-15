"""Admin-only, read-only: чужая история тренировок, пуши, диалоги с AI-тренером
и сырая лента действий (/activity — что человек вводил и куда нажимал, см.
activity_log.py)."""

import asyncio
import datetime as dt
from contextlib import suppress
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import acquisition
import activity_log
import ai_limits
import announcements
import config
import db
import formatting
import keyboards
import push_texts
import ui
import view_builder
from fsm import AdminFlow
from handlers import sharing

router = Router(name="admin")

USERS_PAGE_SIZE = 10
HISTORY_PAGE_SIZE = 8
PUSHES_PAGE_SIZE = 10
AI_DIALOGS_TG_CHUNK = 4000


def _is_admin(telegram_id: int) -> bool:
    return config.ADMIN_ID is not None and telegram_id == config.ADMIN_ID


@router.callback_query(F.data.startswith("ail:ack:"))
async def limit_ack(callback: CallbackQuery):
    """«Понятно» на предупреждении о лимите — до конца суток он пропускает.

    Живёт здесь, а не рядом с самими лимитами: кнопку видят только свои
    аккаунты (config.limit_preview_ids), и роутер админки подключён раньше
    остальных — расписка не должна зависеть от того, на каком экране человек
    поймал предупреждение.
    """
    user_id = callback.from_user.id
    if user_id not in config.limit_preview_ids():
        await callback.answer()
        return
    kind = callback.data.split(":", 2)[2]
    await ai_limits.record_ack(user_id, kind)
    with suppress(TelegramAPIError):
        await callback.message.edit_text(
            f"{callback.message.text}\n\n👌 Понял, до конца суток пропускаю."
        )
    await callback.answer("Пропускаю до конца суток")


async def _show_users_list(target: Message | CallbackQuery, state: FSMContext, page: int):
    await state.set_state(AdminFlow.browsing_users)
    await state.update_data(admin_users_page=page)
    total = await db.count_users()
    users = await db.list_users_with_workout_counts(limit=USERS_PAGE_SIZE, offset=page * USERS_PAGE_SIZE)
    has_next = (page + 1) * USERS_PAGE_SIZE < total
    kb = keyboards.admin_users_keyboard(users, page, has_next)
    text = "👥 Пользователи:" if users else "Пользователей пока нет."
    if isinstance(target, CallbackQuery):
        await ui.safe_edit(target, text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.message(Command("check_users"))
async def cmd_admin(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    await _show_users_list(message, state, page=0)


@router.callback_query(F.data.startswith("admin:up:"))
async def admin_users_page(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    page = int(callback.data.split(":")[2])
    await _show_users_list(callback, state, page)
    await callback.answer()


@router.callback_query(F.data == "admin:menu")
async def admin_to_menu(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    from handlers.workout import _show_main_menu
    await _show_main_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == "admin:back")
async def admin_back_to_users(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    data = await state.get_data()
    await _show_users_list(callback, state, data.get("admin_users_page", 0))
    await callback.answer()


async def _show_history_list(callback: CallbackQuery, state: FSMContext, target_user_id: int, page: int):
    await state.set_state(AdminFlow.browsing_history)
    await state.update_data(admin_target_user=target_user_id, admin_history_page=page)
    total = await db.count_workouts(target_user_id)
    workouts = await db.list_workouts(target_user_id, limit=HISTORY_PAGE_SIZE, offset=page * HISTORY_PAGE_SIZE)
    items = []
    for w in workouts:
        started = dt.datetime.fromisoformat(w["started_at"])
        items.append({"id": w["id"], "label": formatting.format_date_ru(started)})
    has_next = (page + 1) * HISTORY_PAGE_SIZE < total
    kb = keyboards.admin_history_list_keyboard(items, target_user_id, page, has_next)
    text = "📚 История тренировок:" if items else "У этого пользователя пока нет завершённых тренировок."
    await ui.safe_edit(callback, text, reply_markup=kb)


@router.callback_query(F.data.startswith("admin:u:"))
async def admin_pick_user(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    target_user_id = int(callback.data.split(":")[2])
    await _show_history_list(callback, state, target_user_id, page=0)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:hp:"))
async def admin_history_page(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    _, _, target_raw, page_raw = callback.data.split(":")
    await _show_history_list(callback, state, int(target_raw), int(page_raw))
    await callback.answer()


@router.callback_query(F.data.startswith("admin:hb:"))
async def admin_history_back(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    target_user_id = int(callback.data.split(":")[2])
    data = await state.get_data()
    await _show_history_list(callback, state, target_user_id, data.get("admin_history_page", 0))
    await callback.answer()


@router.callback_query(F.data.startswith("admin:hi:"))
async def admin_history_item(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    _, _, target_raw, workout_raw = callback.data.split(":")
    target_user_id, workout_id = int(target_raw), int(workout_raw)
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != target_user_id:
        await callback.answer("Тренировка не найдена", show_alert=True)
        return
    user = await db.get_user(target_user_id)
    blocks = await view_builder.build_block_views(
        workout_id, user["e1rm_formula"], previous_before=workout["started_at"],
        mark_records=True,
    )
    started = dt.datetime.fromisoformat(workout["started_at"])
    duration_seconds = await view_builder.workout_duration_seconds(workout)
    text = formatting.build_workout_summary(
        started, blocks, workout["note"], show_extra_stats=bool(user["show_extra_stats"]),
        duration_seconds=duration_seconds, unit=user["unit"],
    )
    await ui.safe_edit(
        callback, text, reply_markup=keyboards.admin_history_item_keyboard(target_user_id), parse_mode="HTML"
    )
    await callback.answer()


async def _show_pushes_list(target: Message | CallbackQuery, state: FSMContext, page: int):
    await state.set_state(AdminFlow.browsing_pushes)
    await state.update_data(admin_pushes_page=page)
    total = await db.count_pushes()
    pushes = await db.list_recent_pushes(limit=PUSHES_PAGE_SIZE, offset=page * PUSHES_PAGE_SIZE)
    has_next = (page + 1) * PUSHES_PAGE_SIZE < total

    if pushes:
        entries = []
        for p in pushes:
            sent = dt.datetime.fromisoformat(p["sent_at"])
            who = f"@{p['username']}" if p["username"] else str(p["telegram_id"])
            category = push_texts.CATEGORY_LABELS.get(p["category"], p["category"])
            # Пуши с AI-комментарием бывают на несколько абзацев — 10 таких
            # подряд валили Telegram-лимит в 4096 символов, и вместо списка
            # приходило TelegramBadRequest: message is too long, а команда
            # /pushes отвечала «Что-то пошло не так» вместо экрана.
            body = f"«{formatting.shorten(p['text'], 200)}»" if p["text"] else "(без текста — только стикер)"
            entries.append(f"{sent.strftime('%d.%m %H:%M')} · {who} · {category}\n{body}")
        text = f"📬 Пуши ({total}), последние сверху:\n\n" + "\n\n".join(entries)
    else:
        text = "Пушей пока не было."

    kb = keyboards.admin_pushes_keyboard(page, has_next)
    if isinstance(target, CallbackQuery):
        await ui.safe_edit(target, text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.message(Command("pushes"))
async def cmd_pushes(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    await _show_pushes_list(message, state, page=0)


@router.callback_query(F.data.startswith("admin:pp:"))
async def admin_pushes_page(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    page = int(callback.data.split(":")[2])
    await _show_pushes_list(callback, state, page)
    await callback.answer()


async def _show_ai_users_list(target: Message | CallbackQuery, state: FSMContext, page: int):
    await state.set_state(AdminFlow.browsing_ai_users)
    await state.update_data(admin_ai_users_page=page)
    total = await db.count_users()
    users = await db.list_users_with_ai_message_counts(limit=USERS_PAGE_SIZE, offset=page * USERS_PAGE_SIZE)
    has_next = (page + 1) * USERS_PAGE_SIZE < total
    kb = keyboards.admin_ai_users_keyboard(users, page, has_next)
    text = "🤖 Диалоги с AI-тренером — выбери пользователя:" if users else "Пользователей пока нет."
    if isinstance(target, CallbackQuery):
        await ui.safe_edit(target, text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.message(Command("ai_dialogs"))
async def cmd_ai_dialogs(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    await _show_ai_users_list(message, state, page=0)


@router.callback_query(F.data.startswith("admin:aip:"))
async def admin_ai_users_page(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    page = int(callback.data.split(":")[2])
    await _show_ai_users_list(callback, state, page)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:aib:"))
async def admin_ai_users_back(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    page = int(callback.data.split(":")[2])
    await _show_ai_users_list(callback, state, page)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:aiu:"))
async def admin_ai_dialogs_show(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    target_user_id = int(callback.data.split(":")[2])

    user = await db.get_user(target_user_id)
    rows = await db.get_ai_chat_history(target_user_id) if user else []
    if not rows:
        await callback.answer("У этого пользователя пока нет диалогов с AI-тренером.", show_alert=True)
        return

    data = await state.get_data()
    page = data.get("admin_ai_users_page", 0)

    who = f"@{user['username']}" if user["username"] else str(target_user_id)
    lines = [f"🤖 Диалоги с AI-тренером — {who} ({len(rows)} сообщ.):", ""]
    for row in rows:
        sent = dt.datetime.fromisoformat(row["created_at"])
        speaker = "👤 Юзер" if row["role"] == "user" else "🤖 AI"
        lines.append(f"{sent.strftime('%d.%m %H:%M')} · {speaker}:\n{row['content']}")
    text = "\n\n".join(lines)

    chunks = [text[i : i + AI_DIALOGS_TG_CHUNK] for i in range(0, len(text), AI_DIALOGS_TG_CHUNK)]
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        markup = keyboards.admin_ai_dialogs_back_keyboard(page) if is_last else None
        await callback.message.answer(chunk, reply_markup=markup)
    await callback.answer()


ACTIVITY_PAGE_SIZE = 25
# Общая лента смешивает события всех юзеров, поэтому на одно и то же число
# событий приходится куда больше времени наблюдения — короткая страница
# заставляла бы листать через каждые несколько минут. Держим её длиннее.
ACTIVITY_ALL_PAGE_SIZE = 80
# Экран — одно сообщение (4096 символов у Telegram), а в базе строка может быть
# длиной до activity_log.MAX_CONTENT_LEN. Показываем начало: для «что человек
# ввёл» его хватает, а целиком длинная простыня всё равно вытеснила бы с экрана
# соседние события — то есть контекст, ради которого лента и открыта.
ACTIVITY_LINE_LIMIT = 120


def _activity_line(row) -> str:
    at = dt.datetime.fromisoformat(row["created_at"])
    content = row["content"]
    if len(content) > ACTIVITY_LINE_LIMIT:
        content = content[: ACTIVITY_LINE_LIMIT - 1] + "…"
    content = content.replace("\n", " ⏎ ")
    marker = "👉" if row["kind"] == activity_log.KIND_CALLBACK else "💬"
    return f"{at.strftime('%d.%m %H:%M')} {marker} {content}"


def _activity_line_all(row) -> str:
    """Та же строка, что и в ленте одного пользователя, но с автором — общая
    лента иначе нечитаема — и в HTML, чтобы автора можно было выделить жирным."""
    at = dt.datetime.fromisoformat(row["created_at"])
    content = row["content"]
    if len(content) > ACTIVITY_LINE_LIMIT:
        content = content[: ACTIVITY_LINE_LIMIT - 1] + "…"
    content = content.replace("\n", " ⏎ ")
    marker = "👉" if row["kind"] == activity_log.KIND_CALLBACK else "💬"
    who = f"@{row['username']}" if row["username"] else str(row["telegram_id"])
    return f"{at.strftime('%d.%m %H:%M')} {marker} {escape(content)} — <b>{escape(who)}</b>"


async def _show_activity_users(target: Message | CallbackQuery, state: FSMContext, page: int):
    await state.set_state(AdminFlow.browsing_activity_users)
    await state.update_data(admin_activity_users_page=page)
    total = await db.count_users()
    users = await db.list_users_with_event_counts(limit=USERS_PAGE_SIZE, offset=page * USERS_PAGE_SIZE)
    has_next = (page + 1) * USERS_PAGE_SIZE < total
    kb = keyboards.admin_activity_users_keyboard(users, page, has_next)
    text = "👀 Что делают пользователи — выбери, чью ленту открыть:" if users else "Пользователей пока нет."
    if isinstance(target, CallbackQuery):
        await ui.safe_edit(target, text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


async def _show_activity_feed(callback: CallbackQuery, state: FSMContext, target_user_id: int, page: int):
    await state.set_state(AdminFlow.browsing_activity)
    await state.update_data(admin_activity_user=target_user_id, admin_activity_page=page)
    user = await db.get_user(target_user_id)
    total = await db.count_user_events(target_user_id)
    rows = await db.list_user_events(
        target_user_id, limit=ACTIVITY_PAGE_SIZE, offset=page * ACTIVITY_PAGE_SIZE
    )
    has_next = (page + 1) * ACTIVITY_PAGE_SIZE < total

    who = f"@{user['username']}" if user and user["username"] else str(target_user_id)
    if rows:
        header = f"👀 {who} — {total} действий, свежие сверху:"
        text = header + "\n\n" + "\n".join(_activity_line(row) for row in rows)
    else:
        text = f"👀 {who} — действий пока нет (лог хранится {config.ACTIVITY_RETENTION_DAYS} дн.)."

    kb = keyboards.admin_activity_feed_keyboard(target_user_id, page, has_next)
    await ui.safe_edit(callback, text, reply_markup=kb)


async def _show_activity_feed_all(callback: CallbackQuery, state: FSMContext, page: int):
    await state.set_state(AdminFlow.browsing_activity_all)
    await state.update_data(admin_activity_all_page=page)
    total = await db.count_all_events()
    rows = await db.list_all_events(limit=ACTIVITY_ALL_PAGE_SIZE, offset=page * ACTIVITY_ALL_PAGE_SIZE)
    has_next = (page + 1) * ACTIVITY_ALL_PAGE_SIZE < total

    if rows:
        header = f"👀 Все пользователи — {total} действий, свежие сверху:"
        text = header + "\n\n" + "\n".join(_activity_line_all(row) for row in rows)
    else:
        text = f"👀 Действий пока нет (лог хранится {config.ACTIVITY_RETENTION_DAYS} дн.)."

    kb = keyboards.admin_activity_all_keyboard(page, has_next)
    await ui.safe_edit(callback, text, reply_markup=kb, parse_mode="HTML")


GROWTH_WINDOW_DAYS = 30
GROWTH_REFERRERS = 10


@router.message(Command("growth"))
async def cmd_growth(message: Message, state: FSMContext):
    """Воронка по источникам: за что заплатили и что с этого пришло.

    Без аргументов — окно GROWTH_WINDOW_DAYS дней; `/growth 7` сужает его, чтобы
    смотреть свежий закуп, не утопая в накопленной истории.
    """
    if not _is_admin(message.from_user.id):
        return
    days = GROWTH_WINDOW_DAYS
    parts = (message.text or "").split()
    if len(parts) > 1 and parts[1].isdigit() and int(parts[1]) > 0:
        days = int(parts[1])
    funnel = await db.acquisition_funnel(days, alive_days=acquisition.ALIVE_WINDOW_DAYS)
    referrers = await db.top_referrers(GROWTH_REFERRERS)
    bot_username = await sharing.get_bot_username(message.bot)
    text = (
        f"{acquisition.format_funnel(funnel, days)}\n\n"
        f"{acquisition.format_referrers(referrers)}\n\n"
        f"Ссылка под новый канал: <code>{acquisition.channel_link(bot_username, 'имя')}</code> — "
        f"вместо «имя» латиница, цифры и «_»."
    )
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("activity"))
async def cmd_activity(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    await _show_activity_users(message, state, page=0)


@router.callback_query(F.data.startswith("admin:acp:"))
async def admin_activity_users_page(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    page = int(callback.data.split(":")[2])
    await _show_activity_users(callback, state, page)
    await callback.answer()


@router.callback_query(F.data == "admin:acb")
async def admin_activity_back(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    data = await state.get_data()
    await _show_activity_users(callback, state, data.get("admin_activity_users_page", 0))
    await callback.answer()


@router.callback_query(F.data.startswith("admin:acu:"))
async def admin_activity_pick_user(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    target_user_id = int(callback.data.split(":")[2])
    await _show_activity_feed(callback, state, target_user_id, page=0)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:acf:"))
async def admin_activity_feed_page(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    _, _, target_raw, page_raw = callback.data.split(":")
    await _show_activity_feed(callback, state, int(target_raw), int(page_raw))
    await callback.answer()


@router.callback_query(F.data.startswith("admin:aca:"))
async def admin_activity_all(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    page = int(callback.data.split(":")[2])
    await _show_activity_feed_all(callback, state, page)
    await callback.answer()


BROADCAST_SEND_DELAY = 0.05  # seconds between sends — stays under Telegram's rate limit


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    await state.set_state(AdminFlow.broadcast_awaiting_message)
    await message.answer(
        "📢 Пришли сообщение для рассылки всем пользователям — текст, фото, что угодно, "
        "уйдёт как есть. Любая другая команда отменит рассылку."
    )


@router.message(StateFilter(AdminFlow.broadcast_awaiting_message))
async def broadcast_receive(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    total = await db.count_users()
    await state.update_data(broadcast_chat_id=message.chat.id, broadcast_message_id=message.message_id)
    await state.set_state(AdminFlow.broadcast_confirming)
    await message.reply(
        f"Отправить это сообщение всем пользователям ({total})?",
        reply_markup=keyboards.yes_no_keyboard(
            "admin:bc:yes", "admin:bc:no", yes_text="📢 Отправить", no_text="Отмена",
        ),
    )


@router.callback_query(StateFilter(AdminFlow.broadcast_confirming), F.data == "admin:bc:no")
async def broadcast_cancel(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.clear()
    await ui.safe_edit(callback, "Рассылка отменена.")
    await callback.answer()


@router.callback_query(StateFilter(AdminFlow.broadcast_confirming), F.data == "admin:bc:yes")
async def broadcast_send(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    data = await state.get_data()
    src_chat_id = data.get("broadcast_chat_id")
    src_message_id = data.get("broadcast_message_id")
    await state.clear()
    if src_chat_id is None or src_message_id is None:
        await callback.answer("Сообщение потерялось, начни заново через /broadcast", show_alert=True)
        return

    await callback.answer("Рассылка запущена…")
    await ui.safe_edit(callback, "📢 Рассылка запущена, отчитаюсь по завершении…")

    user_ids = await db.list_all_telegram_ids()
    sent = blocked = failed = 0
    for telegram_id in user_ids:
        try:
            await callback.bot.copy_message(
                chat_id=telegram_id, from_chat_id=src_chat_id, message_id=src_message_id,
            )
            sent += 1
        except TelegramForbiddenError:
            blocked += 1
        except TelegramAPIError:
            failed += 1
        await asyncio.sleep(BROADCAST_SEND_DELAY)

    await callback.message.answer(
        f"✅ Рассылка завершена: {sent} доставлено, {blocked} заблокировали бота, {failed} ошибок."
    )




# ---------- разовые релизные рассылки (announcements.py) ----------
#
# Механизм сам присылает админу анонс после разворота и ждёт добра — здесь
# живут кнопки под этим превью и ручной вход в тот же экран.

# Ключи рассылок, которые прямо сейчас разносятся этим процессом. Статус в базе
# на это не годится: «approved» стоит и во время рассылки, и после
# перезапуска посреди неё, а различать надо именно «уже жму, не жми второй раз».
_sending: set[str] = set()


@router.message(Command("announce"))
async def cmd_announce(message: Message):
    """Показать анонсы, ждущие решения, и что с ними.

    Нужна, когда превью потерялось в чате (или ADMIN_ID выставили уже после
    разворота): экран с кнопками можно вызвать руками, а не ждать следующего
    рестарта.
    """
    if not _is_admin(message.from_user.id):
        return
    if not announcements.ANNOUNCEMENTS:
        await message.answer("Нерассланных релизов нет.")
        return
    for ann in announcements.ANNOUNCEMENTS:
        if ann.key in _sending:
            await message.answer(f"Релиз «{ann.key}» рассылаю прямо сейчас.")
            continue
        status = await db.get_announcement_status(ann.key)
        pending = await db.count_announcement_recipients(ann.key)
        if status == announcements.STATUS_APPROVED and not pending:
            await message.answer(f"Релиз «{ann.key}» разослан целиком.")
            continue
        if status == announcements.STATUS_APPROVED:
            await message.answer(
                f"Релиз «{ann.key}» одобрен, осталось разослать: {pending}. "
                "Добью на следующем старте."
            )
            continue
        if not ann.available():
            await message.answer(
                f"Релиз «{ann.key}» ждёт: фича, про которую он написан, в этом развороте выключена."
            )
            continue
        # И «ещё не показывал», и «показывал», и «ты его отклонил» ведут сюда:
        # раз позвали руками — показываем анонс и кнопки заново.
        await announcements.send_preview(message.bot, ann)


@router.callback_query(F.data.startswith("admin:ann:go:"))
async def announcement_approve(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    key = callback.data.split(":", 3)[3]
    ann = announcements.by_key(key)
    if ann is None:
        await callback.answer("Такого релиза больше нет в коде.", show_alert=True)
        return
    if key in _sending:
        await callback.answer("Уже рассылаю.")
        return
    _sending.add(key)
    await db.set_announcement_status(key, announcements.STATUS_APPROVED)
    await callback.answer("Рассылка запущена…")
    await ui.safe_edit(callback, f"📢 Релиз «{key}» пошёл по базе, отчитаюсь по завершении…")
    # Отдельной задачей: рассылка на всю базу идёт минутами, а хендлер
    # callback'а столько держать нельзя — Telegram ждёт ответа секунды.
    asyncio.create_task(_run_announcement(callback.bot, ann))


async def _run_announcement(bot, ann) -> None:
    try:
        await announcements.deliver_and_report(bot, ann)
    finally:
        _sending.discard(ann.key)


@router.callback_query(F.data.startswith("admin:ann:no:"))
async def announcement_decline(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    key = callback.data.split(":", 3)[3]
    await db.set_announcement_status(key, announcements.STATUS_DECLINED)
    await ui.safe_edit(
        callback,
        f"Релиз «{key}» не рассылаю. Передумаешь — /announce покажет его снова.",
    )
    await callback.answer()


# ---------- /admin_wipe: снести аккаунт для проверки онбординга ----------
#
# Нарочно не принимает id аргументом — только выбор из ADMIN_ID/TEST_USER_ID
# кнопкой. Это единственная защита от того, чтобы случайно снести живого
# пользователя опечаткой в id: выбирать можно только из двух заранее известных
# своих же аккаунтов (config.limit_preview_ids), настоящих атлетов в списке нет.


async def _wipe_candidates() -> list[tuple[int, str]]:
    out = []
    for uid in (config.ADMIN_ID, config.TEST_USER_ID):
        if uid is None:
            continue
        user = await db.get_user(uid)
        label = f"@{user['username']}" if user and user["username"] else str(uid)
        out.append((uid, label))
    return out


@router.message(Command("admin_wipe"))
async def cmd_admin_wipe(message: Message):
    if not _is_admin(message.from_user.id):
        return
    candidates = await _wipe_candidates()
    if not candidates:
        await message.answer("ADMIN_ID и TEST_USER_ID не настроены — сносить некого.")
        return
    b = InlineKeyboardBuilder()
    for uid, label in candidates:
        b.button(text=f"🧨 {label} ({uid})", callback_data=f"admin:wipe:ask:{uid}")
    b.adjust(1)
    await message.answer(
        "Кого сносим? Удалится ВСЁ — тренировки, вес, еда, программы, диалог с "
        "тренером, сам аккаунт. Следующий /start пройдёт как у нового.",
        reply_markup=b.as_markup(),
    )


@router.callback_query(F.data.startswith("admin:wipe:ask:"))
async def admin_wipe_ask(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = int(callback.data.split(":")[3])
    candidates = dict(await _wipe_candidates())
    label = candidates.get(uid, str(uid))
    await ui.safe_edit(
        callback,
        f"Точно снести {label} ({uid}) целиком? Это необратимо.",
        reply_markup=keyboards.yes_no_keyboard(
            yes_cb=f"admin:wipe:go:{uid}", no_cb="admin:wipe:cancel",
            yes_text="💣 Да, снести всё", no_text="Отмена",
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:wipe:cancel")
async def admin_wipe_cancel(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    await ui.safe_edit(callback, "Отменил, никого не тронул.")
    await callback.answer()


@router.callback_query(F.data.startswith("admin:wipe:go:"))
async def admin_wipe_go(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = int(callback.data.split(":")[3])
    await db.wipe_user_account(uid)
    await ui.safe_edit(callback, f"Снёс {uid}. /start там теперь пройдёт как у новичка.")
    await callback.answer("Готово")
