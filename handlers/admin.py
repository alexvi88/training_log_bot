"""Admin-only, read-only: чужая история тренировок, пуши, диалоги с AI-тренером
и сырая лента действий (/activity — что человек вводил и куда нажимал, см.
activity_log.py)."""

import asyncio
import datetime as dt
import json
import logging
from contextlib import suppress
from html import escape
from typing import Optional

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InaccessibleMessage,
    InlineKeyboardButton,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import account_deletion
import acquisition
import activity_log
import ai_limits
import announcements
import api_v1_activity
import api_v1_feedback
import api_v1_support
import apns
import config
import db
import formatting
import i18n
import inbox
import keyboards
import product_metrics
import push_texts
import seo_pages
import site_stats
import timeutil
import ui
import view_builder
from fsm import AdminFlow
from handlers import sharing

logger = logging.getLogger(__name__)

router = Router(name="admin")

USERS_PAGE_SIZE = 10
HISTORY_PAGE_SIZE = 8
PUSHES_PAGE_SIZE = 10
AI_DIALOGS_TG_CHUNK = 4000


def _is_admin(telegram_id: int) -> bool:
    return config.ADMIN_ID is not None and telegram_id == config.ADMIN_ID


def admin_time(raw: str) -> dt.datetime:
    """Время события на часах админа (по умолчанию Москва, config.ADMIN_TZ_OFFSET).

    В базе всё лежит по часам сервера, а сервер живёт по UTC (см. timeutil) —
    и админские экраны показывали именно UTC. Разница с Москвой в три часа
    незаметна ровно до того момента, когда по логу надо понять, что было
    «утром» или «вчера вечером»: в UTC вечерний всплеск съезжает на день назад.
    """
    return dt.datetime.fromisoformat(raw) + dt.timedelta(hours=config.ADMIN_TZ_OFFSET)


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
            f"{callback.message.text}\n\n{i18n.t('limit.preview.ack_suffix')}"
        )
    await callback.answer(i18n.t("limit.preview.ack_toast"))


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
            sent = admin_time(p["sent_at"])
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


@router.message(Command("testpush"))
async def cmd_testpush(message: Message, state: FSMContext):
    """Проверочный пуш на iOS прямо сейчас: `/testpush` — себе, `/testpush
    <user_id>` — на телефон этого пользователя. Нужен, чтобы убедиться, что
    вся цепочка живая — секреты APNs, HTTP/2, токен устройства, — не дожидаясь
    настоящего повода для пуша. Отвечает, на каком шаге оборвалось."""
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    parts = (message.text or "").split()
    target = message.from_user.id
    if len(parts) > 1:
        try:
            target = int(parts[1])
        except ValueError:
            await message.answer("Формат: /testpush или /testpush <user_id>")
            return
    # Через inbox.deliver, как настоящие пуши: строка ложится в
    # «Уведомления» приложения даже без токена и без APNs — так проверяется
    # и раздел, а не только баннер. Текст — на языке того, кому шлём.
    user = await db.get_user(target)
    lang = user["lang"] if user and user["lang"] else i18n.DEFAULT_LANG
    with i18n.use_lang(lang):
        title, body = i18n.t("push.test.title"), i18n.t("push.test.body")
    delivery = await inbox.deliver(target, "admin_test", title, body, lang=lang)
    shelf = (
        "строка в «Уведомлениях» есть" if delivery.inbox_id is not None
        else "в «Уведомления» не легло — человек ни разу не входил в приложение"
    )
    if not apns.is_configured():
        await message.answer(
            "❌ APNs не настроен: нужны APNS_KEY_P8, APNS_KEY_ID, APNS_TEAM_ID и APNS_BUNDLE_ID. "
            f"Баннера не будет, {shelf}."
        )
        return
    tokens = await db.get_push_tokens(target)
    if not tokens:
        total = await db.count_push_tokens()
        await message.answer(
            f"❌ У пользователя {target} нет iOS-токена. Открой приложение, разреши "
            f"уведомления и войди — токен придёт сам. iOS-токенов в базе всего: {total}. "
            f"Баннера не будет, {shelf}."
        )
        return
    if delivery.accepted:
        await message.answer(
            f"✅ Apple принял пуш для {target} ({config.APNS_ENV}) на "
            f"{delivery.accepted} из {len(tokens)} устройств, {shelf}. Смотри телефон."
        )
    else:
        await message.answer(
            f"❌ Apple пуш не принял ({shelf}). Причина — в логах: "
            "fly logs -a training-log-bot | grep -i apns"
        )


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
        sent = admin_time(row["created_at"])
        speaker = "👤 Юзер" if row["role"] == "user" else "🤖 AI"
        lines.append(f"{sent.strftime('%d.%m %H:%M')} · {speaker}:\n{row['content']}")
    text = "\n\n".join(lines)

    chunks = [text[i : i + AI_DIALOGS_TG_CHUNK] for i in range(0, len(text), AI_DIALOGS_TG_CHUNK)]
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        markup = keyboards.admin_ai_dialogs_back_keyboard(page) if is_last else None
        await callback.message.answer(chunk, reply_markup=markup)
    await callback.answer()


# ---------- /aichats: журнал ходов тренера для проверки качества ----------
#
# Читает db.ai_turn_log (см. ai_chat_log.py) — бот и приложение вместе, с
# моделью, инструментами и ценой каждого хода. /ai_dialogs выше — старый
# вечный лог бота без этих полей; этот экран — для оценки ответов.

AICHATS_USERS_PAGE_SIZE = 8
AICHATS_TURNS_PAGE_SIZE = 4
AICHATS_RECENT_DAYS = 7
AICHATS_EXPORT_TURNS = 200
# Запас до 4096 у Telegram: подписи и навигация не должны упираться в лимит.
AICHATS_TEXT_LIMIT = 3900
AICHATS_QUESTION_LIMIT = 600
_AICHATS_SOURCE = {"bot": "бот", "app": "приложение"}


def _aichats_since() -> str:
    """Начало окна «за неделю» в той же ISO-форме, что created_at в базе (UTC)."""
    now = dt.datetime.fromisoformat(db.now_iso())
    return (now - dt.timedelta(days=AICHATS_RECENT_DAYS)).isoformat(timespec="seconds")


def _trim(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _turn_tools(row) -> list[str]:
    try:
        tools = json.loads(row["tools"] or "[]")
    except (TypeError, ValueError):
        return []
    return [str(name) for name in tools] if isinstance(tools, list) else []


def _turn_meta(row) -> str:
    parts = [
        admin_time(row["created_at"]).strftime("%d.%m %H:%M"),
        _AICHATS_SOURCE.get(row["source"], row["source"]),
        row["model"] or "модель неизвестна",
    ]
    tools = _turn_tools(row)
    parts.append("🛠 " + ", ".join(tools) if tools else "без инструментов")
    if row["cost_usd"] is not None:
        parts.append(f"${row['cost_usd']:.4f}")
    return " · ".join(parts)


def _aichats_label(row) -> str:
    return keyboards.admin_user_label(
        row["username"], keyboards.row_value(row, "email"), row["telegram_id"]
    )


def format_aichats_users(rows, page: int, total: int) -> str:
    if not rows:
        return "🤖 Журнал тренера пуст: ни одного хода за последние полгода."
    pages = max(1, -(-total // AICHATS_USERS_PAGE_SIZE))
    lines = [f"🤖 Разговоры с тренером — стр. {page + 1}/{pages} (всего атлетов: {total})", ""]
    for row in rows:
        sources = "+".join(
            _AICHATS_SOURCE.get(s, s) for s in sorted(set((row["sources"] or "").split(","))) if s
        )
        lines.append(
            f"{_aichats_label(row)} — {row['turns_recent']} за {AICHATS_RECENT_DAYS} дн. "
            f"(всего {row['turns_total']}), последний {admin_time(row['last_at']).strftime('%d.%m %H:%M')}, "
            f"{sources}, {row['model'] or 'модель неизвестна'}"
        )
    return "\n".join(lines)


def format_aichats_turns(label: str, rows, total: int, page: int) -> str:
    """Страница ходов: rows из базы новыми первыми, на экране — по порядку
    разговора. Каждому ходу — равная доля лимита сообщения; вопрос короче
    ответа, ответ режется хвостом с «…» (целиком — в выгрузке)."""
    pages = max(1, -(-total // AICHATS_TURNS_PAGE_SIZE))
    header = f"🤖 {label} — ходы тренера, стр. {page + 1}/{pages} (всего {total}, новые — на стр. 1)"
    if not rows:
        return header + "\n\nХодов нет."
    budget = (AICHATS_TEXT_LIMIT - len(header)) // len(rows)
    blocks = [header]
    for row in reversed(rows):
        meta = _turn_meta(row)
        room = max(200, budget - len(meta) - 12)
        question = _trim(row["question"], min(AICHATS_QUESTION_LIMIT, room // 3))
        answer = _trim(row["answer"], max(100, room - len(question)))
        blocks.append(f"— {meta}\n👤 {question}\n🤖 {answer}")
    return _trim("\n\n".join(blocks), AICHATS_TEXT_LIMIT)


def format_aichats_export(label: str, telegram_id: int, rows) -> str:
    """Выгрузка для чтения целиком: Markdown, по порядку разговора, без обрезки."""
    lines = [
        f"# Разговоры с AI-тренером: {label} (id {telegram_id})",
        "",
        f"Ходов: {len(rows)} (последние, не больше {AICHATS_EXPORT_TURNS}). "
        f"Время — по часам админа (UTC{config.ADMIN_TZ_OFFSET:+d}).",
    ]
    for row in reversed(rows):
        lines += [
            "",
            f"## {_turn_meta(row)}",
            "",
            "**Вопрос:**",
            "",
            (row["question"] or "").strip(),
            "",
            "**Ответ:**",
            "",
            (row["answer"] or "").strip(),
        ]
    return "\n".join(lines) + "\n"


def _aichats_users_keyboard(rows, page: int, has_next: bool):
    b = InlineKeyboardBuilder()
    for row in rows:
        b.button(
            text=f"{_aichats_label(row)} ({row['turns_recent']}/{AICHATS_RECENT_DAYS}д)",
            callback_data=f"admin:aic:u:{row['telegram_id']}:0:{page}",
        )
    b.adjust(1)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin:aic:p:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Дальше ▶️", callback_data=f"admin:aic:p:{page + 1}"))
    if nav:
        b.row(*nav)
    return b.as_markup()


def _aichats_turns_keyboard(telegram_id: int, page: int, has_older: bool, back: int):
    b = InlineKeyboardBuilder()
    nav = []
    if has_older:
        nav.append(InlineKeyboardButton(
            text="◀️ Старше", callback_data=f"admin:aic:u:{telegram_id}:{page + 1}:{back}"
        ))
    if page > 0:
        nav.append(InlineKeyboardButton(
            text="Новее ▶️", callback_data=f"admin:aic:u:{telegram_id}:{page - 1}:{back}"
        ))
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(
        text=f"📄 Выгрузить {AICHATS_EXPORT_TURNS}", callback_data=f"admin:aic:x:{telegram_id}"
    ))
    b.row(InlineKeyboardButton(text="↩️ К списку", callback_data=f"admin:aic:p:{back}"))
    return b.as_markup()


async def _show_aichats_users(target: Message | CallbackQuery, page: int, *, edit: bool) -> None:
    total = await db.count_ai_turn_log_users()
    rows = await db.list_ai_turn_log_users(
        _aichats_since(), limit=AICHATS_USERS_PAGE_SIZE, offset=page * AICHATS_USERS_PAGE_SIZE
    )
    has_next = (page + 1) * AICHATS_USERS_PAGE_SIZE < total
    text = format_aichats_users(rows, page, total)
    kb = _aichats_users_keyboard(rows, page, has_next)
    if edit:
        await ui.safe_edit(target, text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.message(Command("aichats"))
async def cmd_aichats(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await _show_aichats_users(message, page=0, edit=False)


@router.callback_query(F.data.startswith("admin:aic:p:"))
async def admin_aichats_page(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    page = max(0, int(callback.data.split(":")[3]))
    await _show_aichats_users(callback, page, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:aic:u:"))
async def admin_aichats_user(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    _, _, _, raw_id, raw_page, raw_back = callback.data.split(":")
    telegram_id, page, back = int(raw_id), max(0, int(raw_page)), max(0, int(raw_back))
    total = await db.count_ai_turn_log(telegram_id)
    rows = await db.get_ai_turn_log(
        telegram_id, limit=AICHATS_TURNS_PAGE_SIZE, offset=page * AICHATS_TURNS_PAGE_SIZE
    )
    user = await db.get_user(telegram_id)
    label = keyboards.admin_user_label(
        user["username"] if user else None, await db.get_user_email(telegram_id), telegram_id
    )
    has_older = (page + 1) * AICHATS_TURNS_PAGE_SIZE < total
    await ui.safe_edit(
        callback,
        format_aichats_turns(label, rows, total, page),
        reply_markup=_aichats_turns_keyboard(telegram_id, page, has_older, back),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:aic:x:"))
async def admin_aichats_export(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    telegram_id = int(callback.data.split(":")[3])
    rows = await db.get_ai_turn_log(telegram_id, limit=AICHATS_EXPORT_TURNS)
    if not rows:
        await callback.answer("Ходов нет.", show_alert=True)
        return
    user = await db.get_user(telegram_id)
    label = keyboards.admin_user_label(
        user["username"] if user else None, await db.get_user_email(telegram_id), telegram_id
    )
    body = format_aichats_export(label, telegram_id, rows).encode("utf-8")
    stamp = admin_time(db.now_iso()).strftime("%Y%m%d")
    await callback.message.answer_document(
        BufferedInputFile(body, filename=f"aichats_{telegram_id}_{stamp}.md")
    )
    await callback.answer()


ACTIVITY_PAGE_SIZE = 25
# Общая лента смешивает события всех юзеров, поэтому на одно и то же число
# событий приходится куда больше времени наблюдения — короткая страница
# заставляла бы листать через каждые несколько минут. Держим её длиннее.
ACTIVITY_ALL_PAGE_SIZE = 80
# Тексты в ленте — целиком: и что человек написал (до activity_log.MAX_CONTENT_LEN),
# и расшифровка голосового, и ответ тренера. Раньше строка резалась до 120
# символов, и владелец видел «Я бы **не переходил сразу** … Дли…» — ни вопроса,
# ни ответа целиком. Страница, которая не влезает в одно сообщение Telegram,
# уходит несколькими (ui.split_html), кнопки — под последним.
ACTIVITY_CHUNK_LIMIT = 4000
# Ответ тренера — markdown модели; в ленте он рендерится так же, как атлету
# (formatting.ai_markdown_to_html), а не сырыми звёздочками.
_ACTIVITY_MARKDOWN_KINDS = frozenset({activity_log.KIND_AI_REPLY})


# Кто написал строку: человек, его тап или тренер в ответ.
_ACTIVITY_MARKERS = {
    activity_log.KIND_CALLBACK: "👉",
    activity_log.KIND_CALLBACK_UNHANDLED: "👉",
    activity_log.KIND_AI_REPLY: "🤖",
    # Не 💀: тренировку вернули, и в ленте это должно читаться как спасённый
    # тап, а не как мёртвая кнопка.
    activity_log.KIND_CALLBACK_RECOVERED: "🔁",
    # Нажатие нижней клавиатуры — такой же тап, как инлайновый, и читаться в
    # ленте должно тапом, а не набранным словом.
    activity_log.KIND_REPLY_BUTTON: "👉",
    activity_log.KIND_AI_FAILED: "⚠️",
    activity_log.KIND_AI_PROGRAM_OFFERED: "🗂",
    # Действие из iOS-приложения (api_v1_activity). Маркер тот же, что у тапа:
    # это ровно тот же «человек что-то сделал», а чем именно сделал — уже
    # сказано пометкой источника слева.
    api_v1_activity.KIND_API_ACTION: "👉",
}


def _activity_marker(kind: str) -> str:
    return _ACTIVITY_MARKERS.get(kind, "💬")


def _activity_source(row) -> str:
    """Пометка источника: `[tg]` или `[ios]`.

    Читаем колонку защитно, а не `row["source"]`: строки такой формы собирают не
    только выборки ленты (aiosqlite.Row на незнакомый ключ бросает IndexError,
    dict — KeyError), и падать на экране из-за отсутствующей пометки нечестно.
    Нет колонки — значит Telegram: до появления источников оттуда приходило всё.
    """
    try:
        source = row["source"] or activity_log.SOURCE_TG
    except (KeyError, IndexError):
        source = activity_log.SOURCE_TG
    return f"[{source}]"


def _activity_content(row) -> str:
    """Содержимое строки ленты в HTML — целиком, без обрезки.

    Переводы строк — «⏎»: одно событие остаётся одной строкой ленты, и граница
    между событиями всегда видна (по ней же ui.split_html режет страницу)."""
    content = (row["content"] or "").strip()
    markdown = row["kind"] in _ACTIVITY_MARKDOWN_KINDS
    content = formatting.ai_markdown_to_html(content) if markdown else escape(content)
    return content.replace("\n", " ⏎ ")


def _activity_line(row) -> str:
    at = admin_time(row["created_at"])
    return (
        f"{at.strftime('%d.%m %H:%M')} {_activity_source(row)} "
        f"{_activity_marker(row['kind'])} {_activity_content(row)}"
    )


def _activity_line_all(row) -> str:
    """Та же строка, что и в ленте одного пользователя, но с автором — общая
    лента иначе нечитаема — и автор выделен жирным."""
    at = admin_time(row["created_at"])
    who = keyboards.admin_user_label(
        row["username"], keyboards.row_value(row, "email"), row["telegram_id"]
    )
    return (
        f"{at.strftime('%d.%m %H:%M')} {_activity_source(row)} {_activity_marker(row['kind'])} "
        f"{_activity_content(row)} — <b>{escape(who)}</b>"
    )


async def _drop_activity_extras(callback: CallbackQuery, state: FSMContext) -> None:
    """Убрать куски прошлой страницы ленты, кроме того, что под кнопками."""
    data = await state.get_data()
    extra = data.get("admin_activity_extra_ids") or []
    if not extra:
        return
    await state.update_data(admin_activity_extra_ids=[])
    chat_id = callback.message.chat.id
    for message_id in extra:
        with suppress(TelegramAPIError):
            await callback.bot.delete_message(chat_id, message_id)


async def _show_activity_text(
    callback: CallbackQuery, state: FSMContext, text: str, reply_markup
) -> None:
    """Страница ленты — одним сообщением, а не влезла — несколькими подряд.

    Кнопки — под последним куском: листают оттуда, где страница кончилась.
    Куски прошлой страницы при перелистывании убираются, чтобы чат не копил
    ленту дважды.
    """
    await _drop_activity_extras(callback, state)
    chunks = ui.split_html(text, ACTIVITY_CHUNK_LIMIT)
    if len(chunks) == 1:
        await ui.safe_edit(callback, text, reply_markup=reply_markup, parse_mode="HTML")
        return
    message = callback.message
    chat_id = message.chat.id
    if not isinstance(message, InaccessibleMessage):
        with suppress(TelegramAPIError):
            await message.delete()
    extra: list[int] = []
    for chunk in chunks[:-1]:
        sent = await callback.bot.send_message(chat_id, chunk, parse_mode="HTML")
        extra.append(sent.message_id)
    await callback.bot.send_message(chat_id, chunks[-1], reply_markup=reply_markup, parse_mode="HTML")
    await state.update_data(admin_activity_extra_ids=extra)


async def _show_activity_users(target: Message | CallbackQuery, state: FSMContext, page: int):
    await state.set_state(AdminFlow.browsing_activity_users)
    await state.update_data(admin_activity_users_page=page)
    total = await db.count_users()
    users = await db.list_users_with_event_counts(limit=USERS_PAGE_SIZE, offset=page * USERS_PAGE_SIZE)
    has_next = (page + 1) * USERS_PAGE_SIZE < total
    kb = keyboards.admin_activity_users_keyboard(users, page, has_next)
    if isinstance(target, CallbackQuery):
        await _drop_activity_extras(target, state)
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

    who = keyboards.admin_user_label(
        user["username"] if user else None, await db.get_user_email(target_user_id), target_user_id
    )
    if rows:
        header = f"👀 {escape(who)} — {total} действий, свежие сверху:"
        text = header + "\n\n" + "\n".join(_activity_line(row) for row in rows)
    else:
        text = f"👀 {escape(who)} — действий пока нет (лог хранится {config.ACTIVITY_RETENTION_DAYS} дн.)."

    kb = keyboards.admin_activity_feed_keyboard(target_user_id, page, has_next)
    await _show_activity_text(callback, state, text, kb)


SITE_FEED_PAGE_SIZE = 30


async def _show_site_feed(callback: CallbackQuery, page: int):
    """Посещения сайта людьми, сложенные по 10 минутам (site_stats.format_feed_line):
    роботы и повторные обновления одной страницы ленту не забивают."""
    total = await db.count_site_visit_buckets()
    rows = await db.site_visits_feed(limit=SITE_FEED_PAGE_SIZE, offset=page * SITE_FEED_PAGE_SIZE)
    has_next = (page + 1) * SITE_FEED_PAGE_SIZE < total
    if rows:
        text = "🌐 Сайт — просмотры людьми, по 10 минут, время UTC, свежие сверху:\n\n" + "\n".join(
            escape(site_stats.format_feed_line(row)) for row in rows
        )
    else:
        text = f"🌐 Посещений сайта пока нет (хранятся {config.ANALYTICS_RETENTION_DAYS} дн.)."
    kb = keyboards.admin_site_feed_keyboard(page, has_next)
    await ui.safe_edit(callback, text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("admin:acs:"))
async def admin_activity_site(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    await _show_site_feed(callback, int(callback.data.split(":")[2]))
    await callback.answer()


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
    await _show_activity_text(callback, state, text, kb)


GROWTH_WINDOW_DAYS = 30
GROWTH_REFERRERS = 10


def _growth_day(raw: str) -> Optional[str]:
    """Аргумент `/growth` как конкретный день — ISO-дата или None, если это не день.

    Понимает «сегодня»/«today», «вчера»/«yesterday», «18.08», «18.08.2026» и
    «2026-08-18». День отдельно от «/growth 1» нарочно: скользящие сутки от
    «сейчас» — это вчерашний вечер плюс сегодняшнее утро, а на вопрос «сколько
    пришло сегодня» так отвечать нельзя.
    """
    word = raw.strip().lower()
    today = dt.date.today()
    if word in ("сегодня", "today"):
        return today.isoformat()
    if word in ("вчера", "yesterday"):
        return (today - dt.timedelta(days=1)).isoformat()
    for fmt, needs_year in (("%Y-%m-%d", True), ("%d.%m.%Y", True), ("%d.%m", False)):
        try:
            parsed = dt.datetime.strptime(word, fmt).date()
        except ValueError:
            continue
        # «18.08» без года — это год текущий, а если такой день ещё не наступил,
        # то прошлый: в августе «31.12» спрашивают про прошлый декабрь.
        if not needs_year:
            parsed = parsed.replace(year=today.year)
            if parsed > today:
                parsed = parsed.replace(year=today.year - 1)
        return parsed.isoformat()
    return None


@router.message(Command("growth"))
async def cmd_growth(message: Message, state: FSMContext):
    """Воронка по источникам: за что заплатили и что с этого пришло.

    Без аргументов — окно GROWTH_WINDOW_DAYS дней; `/growth 7` сужает его, чтобы
    смотреть свежий закуп, не утопая в накопленной истории. `/growth сегодня`
    (а также «вчера» и любая дата вида 18.08 / 2026-08-18) — ровно один день, от
    полуночи до полуночи.
    """
    if not _is_admin(message.from_user.id):
        return
    days = GROWTH_WINDOW_DAYS
    day: Optional[str] = None
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1:
        arg = parts[1].strip()
        if arg.isdigit() and int(arg) > 0:
            days = int(arg)
        else:
            day = _growth_day(arg)
            if day is None:
                await message.answer(
                    "Не понял период. Пиши «/growth 7» — за столько дней, "
                    "«/growth сегодня» или «/growth 18.08» — за один день."
                )
                return
    funnel = await db.acquisition_funnel(
        days, alive_days=acquisition.ALIVE_WINDOW_DAYS, day=day
    )
    onboarding = await db.onboarding_funnel(days, day=day)
    app_funnel = await db.app_funnel(days, day=day)
    referrers = await db.top_referrers(GROWTH_REFERRERS)
    donation_stars, donation_people = await db.donation_totals(days, day=day)
    period = acquisition.period_label(days, day)
    bot_username = await sharing.get_bot_username(message.bot)
    site = await db.site_stats(days, day=day)
    seo_signups = [r for r in funnel if str(r["source"]).startswith(seo_pages.BOT_TAG_PREFIX)]
    text = (
        f"{acquisition.format_funnel(funnel, days, day)}\n\n"
        f"{acquisition.format_onboarding_funnel(onboarding, days, day)}\n\n"
        f"{acquisition.format_app_funnel(app_funnel, days, day)}\n\n"
        f"{acquisition.format_referrers(referrers)}\n\n"
        # Сайт (seo_pages.py, site_stats.py): просмотры без cookies и клики по
        # кнопкам; последняя строка закрывает воронку — сколько из этого
        # дошло до /start с меткой src_seo_*.
        f"{site_stats.format_growth_block(site, period, seo_signups)}\n\n"
        # Донат («Поддержать проект», handlers/donate.py) — не часть воронки
        # источников: это разовый жест, не шаг в онбординге, и число просто
        # рядом, а не в теле format_funnel/format_onboarding_funnel.
        f"❤️ Донаты за {period}: {donation_stars} ⭐ от {donation_people} человек.\n\n"
        f"Ссылка под новый канал: <code>{acquisition.channel_link(bot_username, 'имя')}</code> — "
        f"вместо «имя» латиница, цифры и «_»."
    )
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)


CRASHES_WINDOW_DAYS = 7
CRASHES_RECENT_LIMIT = 5

_KIND_LABELS = {
    "crash": "падения",
    "hang": "зависания",
    "cpu_exception": "перерасход CPU",
    "disk_write_exception": "запись на диск",
    "keychain_save_failed": "Keychain не сохранил вход",
    "storage_failure": "сбой файла очереди на телефоне",
}


def format_crash_report(summary, recent, days: int) -> str:
    """Сводка `/crashes`: сколько сбоев каждого вида по версиям и сборкам и
    несколько последних падений с причиной из diagnosticMetaData. Сами стеки —
    в базе (db.diagnostics.payload), в чат они не влезают и без символикации
    всё равно читаются адресами."""
    if not summary:
        return f"🧯 Сбоев iOS-приложения за {days} дн. не пришло."
    lines = [f"🧯 <b>Сбои iOS-приложения за {days} дн.</b>", ""]
    current = None
    for row in summary:
        version = f"{row['app_version']} ({row['build']})"
        if version != current:
            current = version
            lines.append(f"<b>{escape(version)}</b>")
        label = _KIND_LABELS.get(row["kind"], row["kind"])
        lines.append(f"  {escape(label)}: {row['n']} (людей с входом: {row['users']})")
    if recent:
        lines += ["", "<b>Последние падения:</b>"]
        for row in recent:
            reason = []
            if row["exception_type"] is not None:
                reason.append(f"exc {row['exception_type']}")
            if row["signal"] is not None:
                reason.append(f"sig {row['signal']}")
            if row["termination_reason"]:
                reason.append(str(row["termination_reason"])[:80])
            who = f"id {row['user_id']}" if row["user_id"] is not None else "без входа"
            lines.append(
                f"#{row['id']} {admin_time(row['created_at']).strftime('%d.%m %H:%M')} · "
                f"{escape(str(row['app_version']))} ({escape(str(row['build']))}) · "
                f"{escape(str(row['device']))} iOS {escape(str(row['os_version']))} · {who}"
                + (f"\n  {escape(', '.join(reason))}" if reason else "")
            )
    lines += [
        "",
        "Стек целиком: <code>/crash N</code>",
    ]
    return "\n".join(lines)


# Сколько кадров главного потока показать текстом в `/crash N`.
CRASH_FRAMES_SHOWN = 15
# Сколько последних экранов перед сбоем показать в `/crash`.
CRASH_SCREENS_SHOWN = 8


def _attributed_frames(payload: dict) -> list[dict]:
    """Кадры потока, на котором случился сбой или зависание (MetricKit
    `callStackTree`, поток с `threadAttributed`), сверху вниз — первая цепочка
    `subFrames`. Нет такого потока — первый попавшийся."""
    tree = payload.get("callStackTree") or {}
    stacks = tree.get("callStacks") or []
    if not stacks:
        return []
    stack = next((s for s in stacks if s.get("threadAttributed")), stacks[0])
    frames: list[dict] = []
    level = stack.get("callStackRootFrames") or []
    while level and len(frames) < 200:
        frame = level[0]
        frames.append(frame)
        level = frame.get("subFrames") or []
    return frames


def format_crash_detail(row, payload: dict) -> str:
    """Текст `/crash N`: что за сбой, на какой сборке и верх стека — свои
    кадры (`TrainingLog`) жирным, чтобы сразу было видно, наш ли код."""
    meta = payload.get("diagnosticMetaData") or {}
    who = f"id {row['user_id']}" if row["user_id"] is not None else "без входа"
    lines = [
        f"🧯 <b>#{row['id']} · {escape(_KIND_LABELS.get(row['kind'], row['kind']))}</b>",
        f"{escape(str(row['app_version']))} ({escape(str(row['build']))}) · "
        f"{escape(str(row['device']))} iOS {escape(str(row['os_version']))} · {who}",
    ]
    for key, label in (
        ("hangDuration", "зависание"),
        ("exceptionType", "exc"),
        ("signal", "sig"),
        ("terminationReason", "причина"),
    ):
        if meta.get(key) is not None:
            lines.append(f"{label}: {escape(str(meta[key])[:200])}")
    # Экраны из окна отчёта (приложение кладёт их в payload.appScreens): в
    # стеке зависания часто только SwiftUI, и без них не понять, где висло.
    screens = [
        str(item.get("screen"))[:40]
        for item in (payload.get("appScreens") or [])
        if isinstance(item, dict) and item.get("screen")
    ]
    if screens:
        lines.append("экраны: " + escape(" → ".join(screens[-CRASH_SCREENS_SHOWN:])))
    frames = _attributed_frames(payload)
    if frames:
        lines += ["", "<b>Верх стека:</b>"]
        for frame in frames[:CRASH_FRAMES_SHOWN]:
            name = str(frame.get("binaryName") or "?")
            text = escape(f"{name} +{frame.get('offsetIntoBinaryTextSegment', '?')}")
            lines.append(f"<b>{text}</b>" if name == "TrainingLog" else text)
        if len(frames) > CRASH_FRAMES_SHOWN:
            lines.append(f"… ещё {len(frames) - CRASH_FRAMES_SHOWN}")
    lines += ["", "Полный отчёт — файлом ниже; адреса символизируются по dSYM этой сборки."]
    return "\n".join(lines)


@router.message(Command("crash"))
async def cmd_crash(message: Message, state: FSMContext):
    """Один отчёт MetricKit по номеру из алерта или `/crashes`: сводка с верхом
    стека и сам JSON файлом."""
    if not _is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1].strip().lstrip("#") if len(parts) > 1 else ""
    if not arg.isdigit():
        await message.answer("Номер отчёта: <code>/crash 4</code>", parse_mode="HTML")
        return
    row = await db.get_diagnostic(int(arg))
    if row is None:
        await message.answer(f"Отчёта #{escape(arg)} нет — возможно, уже вычищен по сроку.", parse_mode="HTML")
        return
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        payload = {}
    await message.answer(format_crash_detail(row, payload if isinstance(payload, dict) else {}), parse_mode="HTML")
    await message.answer_document(
        BufferedInputFile(str(row["payload"]).encode(), filename=f"diagnostic-{row['id']}.json"),
    )


@router.message(Command("crashes"))
async def cmd_crashes(message: Message, state: FSMContext):
    """Сбои iOS-приложения из MetricKit (api_v1_diagnostics.py) — по версиям.

    Без аргументов — CRASHES_WINDOW_DAYS дней, `/crashes 30` — за столько.
    """
    if not _is_admin(message.from_user.id):
        return
    days = CRASHES_WINDOW_DAYS
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip().isdigit() and int(parts[1].strip()) > 0:
        days = int(parts[1].strip())
    summary = await db.diagnostics_summary(days)
    recent = await db.recent_crashes(days, CRASHES_RECENT_LIMIT)
    await message.answer(format_crash_report(summary, recent, days), parse_mode="HTML")


# Колонки таблицы /metrics — самое нужное, что влезает в экран телефона.
# Остальные метрики — в CSV под таблицей.
METRICS_TABLE_COLUMNS = (
    ("active_users", "DAU"),
    ("wau", "WAU"),
    ("mau", "MAU"),
    ("new_users", "нов"),
    ("trained_users", "трен"),
    ("active_ios", "iOS"),
)
METRICS_TABLE_DAYS = 14


def _metric_cell(value) -> str:
    if value is None:
        return "·"
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def format_metrics_table(rows: list[dict]) -> str:
    """Последние сутки сводки (product_metrics) моноширинной таблицей и
    удержание D1/D7/D30 по последним дозревшим когортам."""
    if not rows:
        return "📈 Сводка пока пустая — первые сутки лягут в первый час после полуночи UTC."
    head = "день  " + " ".join(f"{title:>4}" for _, title in METRICS_TABLE_COLUMNS)
    lines = [head]
    for row in rows[-METRICS_TABLE_DAYS:]:
        cells = " ".join(f"{_metric_cell(row.get(key)):>4}" for key, _ in METRICS_TABLE_COLUMNS)
        lines.append(f"{row['day'][5:]} {cells}")
    retention = []
    for n in (1, 7, 30):
        cohort = retained = 0
        # Четыре недели дозревших когорт — одна-две регистрации в день дают
        # шум, а не долю.
        matured = [r for r in rows if r.get(f"cohort_d{n}")][-28:]
        for r in matured:
            cohort += r[f"cohort_d{n}"]
            retained += r.get(f"retained_d{n}", 0)
        if cohort:
            retention.append(f"D{n} {retained / cohort:.0%} ({int(retained)}/{int(cohort)})")
    text = "📈 <b>Метрики по дням</b> (без своих аккаунтов)\n<pre>" + escape("\n".join(lines)) + "</pre>"
    if retention:
        text += "\nУдержание, 4 нед. когорт: " + " · ".join(retention)
    pushes = _metrics_push_line(rows)
    if pushes:
        text += "\n" + pushes
    return text


def _metrics_push_line(rows: list[dict]) -> str:
    """Итоги по пушам за последние сутки сводки одной строкой. Разбивка по
    категориям (`pushes_sent:<кат>`) — только в CSV: в таблицу телефона
    два десятка колонок не влезут."""
    last = rows[-1]
    tg = last.get("pushes_sent_tg") or 0
    ios = last.get("pushes_sent_ios") or 0
    failed = last.get("push_failed_ios") or 0
    taps_ios = int(last.get("push_opens_ios") or 0)
    taps_tg = int(last.get("push_opens_tg") or 0)
    # Тапы — в сутки тапа: в день без отправок по вчерашним пушам они бывают.
    if not (tg or ios or failed or taps_ios or taps_tg):
        return ""
    parts = [f"{int(tg + ios)} (TG {int(tg)} · iOS {int(ios)})"]
    if last.get("push_users"):
        parts.append(f"{int(last['push_users'])} чел.")
    parts.append(f"тапов {taps_ios + taps_tg} (TG {taps_tg} · iOS {taps_ios})")
    if last.get("push_ctr_ios") is not None:
        parts.append(f"CTR iOS {last['push_ctr_ios']:.0%}")
    if last.get("push_ctr_tg") is not None:
        parts.append(f"CTR TG {last['push_ctr_tg']:.0%}")
    if failed:
        parts.append(f"не доставлено iOS {int(failed)}")
    workout = next((r for r in reversed(rows) if r.get("push_to_workout_24h") is not None), None)
    if workout:
        parts.append(f"трен. за 24 ч {workout['push_to_workout_24h']:.0%} ({workout['day'][5:]})")
    return f"📣 Пуши {last['day'][5:]}: " + ", ".join(parts)


def metrics_csv(names: list[str], rows: list[dict]) -> bytes:
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["day", *names])
    for row in rows:
        writer.writerow([row["day"], *(row.get(name, "") for name in names)])
    return buf.getvalue().encode("utf-8")


@router.message(Command("metrics"))
async def cmd_metrics(message: Message, state: FSMContext):
    """Суточная сводка метрик (product_metrics): таблица за две недели и CSV
    всей истории — для графиков где угодно."""
    if not _is_admin(message.from_user.id):
        return
    await product_metrics.catch_up()
    names, rows = await product_metrics.pivot()
    await message.answer(format_metrics_table(rows), parse_mode="HTML")
    if rows:
        await message.answer_document(
            BufferedInputFile(metrics_csv(names, rows), filename=f"metrics_{rows[-1]['day']}.csv")
        )


# --- /events_csv: сырые продуктовые события для разбора где угодно ----------------

EVENTS_CSV_DEFAULT_DAYS = 30
# Потолок строк одной выгрузки: файл пишется на диск кусками (память не
# растёт), но Telegram принимает от бота документ до 50 МБ, а строка — около
# 120 байт. Больше — выгрузка обрезается, и подпись это говорит.
EVENTS_CSV_MAX_ROWS = 200_000

EVENTS_CSV_COLUMNS = ("ts", "user_id", "platform", "event", "props", "app_version", "own")
EVENT_USERS_CSV_COLUMNS = (
    "user_id", "signup_date", "source", "lang", "signup_platform", "has_app", "own",
)


def parse_events_csv_days(args: Optional[str]) -> Optional[int]:
    """`/events_csv [дней]`: без числа — 30; больше срока хранения событий
    смысла нет. None — не число."""
    raw = (args or "").strip()
    if not raw:
        return EVENTS_CSV_DEFAULT_DAYS
    if not raw.isdigit():
        return None
    return max(1, min(int(raw), config.ANALYTICS_RETENTION_DAYS))


async def write_events_csv(path: str, since: str, limit: int, own: set[int]) -> int:
    """Сырые события с `since` в CSV-файл — построчно, без всей выгрузки в
    памяти. `own` — свои аккаунты (config.limit_preview_ids): строки не
    выкидываются, а помечаются, отфильтровать — дело разбора. Вернёт число строк."""
    import csv

    count = 0
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(EVENTS_CSV_COLUMNS)
        async for created_at, user_id, platform, event, props, app_version in db.iter_analytics_events(
            since, limit
        ):
            writer.writerow(
                [created_at, user_id, platform, event, props or "", app_version or "",
                 1 if user_id in own else 0]
            )
            count += 1
    return count


async def write_event_users_csv(path: str, since: str, own: set[int]) -> int:
    """Признаки атлетов из выгрузки для когорт: дата заведения, источник,
    язык, где завёлся (app-only аккаунт — ios), есть ли вход в приложение."""
    import csv

    rows = await db.analytics_event_users(since)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(EVENT_USERS_CSV_COLUMNS)
        for row in rows:
            writer.writerow([
                row["user_id"],
                (row["created_at"] or "")[:10],
                row["source"] or "",
                row["lang"] or "",
                "tg" if row["telegram_linked"] else "ios",
                1 if row["has_app"] else 0,
                1 if row["user_id"] in own else 0,
            ])
    return len(rows)


@router.message(Command("events_csv"))
async def cmd_events_csv(message: Message, state: FSMContext, command: CommandObject | None = None):
    """Сырые продуктовые события (analytics_events) за N дней — для любой
    воронки, которой нет в /metrics: два CSV, события и атлеты (джойн по
    user_id). Без текста людей — его в событиях нет by design."""
    if not _is_admin(message.from_user.id):
        return
    days = parse_events_csv_days(command.args if command else None)
    if days is None:
        await message.answer("Дней — числом: /events_csv 30")
        return
    import os
    import tempfile

    since = (timeutil.utc_now() - dt.timedelta(days=days)).isoformat(timespec="seconds")
    total = await db.count_analytics_events_since(since)
    if not total:
        await message.answer(f"За {days} дн. событий нет.")
        return
    own = config.limit_preview_ids()
    stamp = dt.date.today().isoformat()
    with tempfile.TemporaryDirectory() as tmp:
        events_path = os.path.join(tmp, f"events_{days}d_{stamp}.csv")
        users_path = os.path.join(tmp, f"event_users_{days}d_{stamp}.csv")
        written = await write_events_csv(events_path, since, EVENTS_CSV_MAX_ROWS, own)
        users = await write_event_users_csv(users_path, since, own)
        caption = f"События за {days} дн.: {written} строк, атлетов {users}."
        if written < total:
            caption += f" Обрезано: всего {total}, выгружены первые {written} — возьми меньше дней."
        await message.answer_document(FSInputFile(events_path), caption=caption)
        await message.answer_document(FSInputFile(users_path))


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


def _broadcast_lang_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="🇷🇺 Только русским", callback_data="admin:bc:lang:ru")
    b.button(text="🇬🇧 Только англоязычным", callback_data="admin:bc:lang:en")
    b.button(text="🌍 Обоим, два текста", callback_data="admin:bc:lang:both")
    b.button(text="Отмена", callback_data="admin:bc:no")
    b.adjust(1)
    return b.as_markup()


async def _broadcast_audience(mode: str) -> dict[str, list[int]]:
    """Кому уйдёт рассылка — по языку интерфейса (`users.lang`).

    Русский текст не должен приезжать тому, у кого бот говорит по-английски, и
    наоборот: поэтому получатели всегда режутся по языку, а не берутся списком
    всех. В режиме «both» это два непересекающихся списка и два разных
    сообщения — по одному на язык.
    """
    langs = i18n.SUPPORTED if mode == "both" else (mode,)
    return {lang: await db.list_telegram_ids_by_lang(lang) for lang in langs}


async def _broadcast_confirm(message: Message, state: FSMContext, mode: str) -> None:
    audience = await _broadcast_audience(mode)
    who = ", ".join(f"{lang.upper()} — {len(ids)}" for lang, ids in audience.items())
    await state.set_state(AdminFlow.broadcast_confirming)
    await message.reply(
        f"Отправить рассылку? Получатели по языку: {who}.",
        reply_markup=keyboards.yes_no_keyboard(
            "admin:bc:yes", "admin:bc:no", yes_text="📢 Отправить", no_text="Отмена",
        ),
    )


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    await state.set_state(AdminFlow.broadcast_choosing_lang)
    await message.answer(
        "📢 Кому шлём? Рассылка режется по языку интерфейса, "
        "так что русский текст не уедет англоязычным.",
        reply_markup=_broadcast_lang_keyboard(),
    )


@router.callback_query(
    StateFilter(AdminFlow.broadcast_choosing_lang), F.data.startswith("admin:bc:lang:")
)
async def broadcast_choose_lang(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    mode = callback.data.split(":")[3]
    if mode not in (*i18n.SUPPORTED, "both"):
        await callback.answer("Не понял язык, начни заново через /broadcast", show_alert=True)
        return
    await state.update_data(broadcast_mode=mode)
    await state.set_state(AdminFlow.broadcast_awaiting_message)
    what = "русское сообщение" if mode in ("ru", "both") else "английское сообщение"
    tail = " Следом попрошу английское." if mode == "both" else ""
    await ui.safe_edit(
        callback,
        f"📢 Пришли {what} — текст, фото, что угодно, уйдёт как есть.{tail} "
        "Любая другая команда отменит рассылку.",
    )
    await callback.answer()


@router.message(StateFilter(AdminFlow.broadcast_awaiting_message))
async def broadcast_receive(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    mode = data.get("broadcast_mode", "ru")
    # В режиме «both» первым приходит русский текст, английский — следующим
    # сообщением; в одноязычных режимах пришедшее и есть текст этого языка.
    lang = "en" if mode == "en" else "ru"
    await state.update_data(
        **{
            f"broadcast_{lang}_chat_id": message.chat.id,
            f"broadcast_{lang}_message_id": message.message_id,
        }
    )
    if mode == "both":
        await state.set_state(AdminFlow.broadcast_awaiting_message_en)
        await message.reply(
            "Принял русское. Теперь пришли английское — уйдёт тем, у кого бот говорит по-английски."
        )
        return
    await _broadcast_confirm(message, state, mode)


@router.message(StateFilter(AdminFlow.broadcast_awaiting_message_en))
async def broadcast_receive_en(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.update_data(
        broadcast_en_chat_id=message.chat.id, broadcast_en_message_id=message.message_id
    )
    await _broadcast_confirm(message, state, "both")


@router.callback_query(
    StateFilter(AdminFlow.broadcast_choosing_lang, AdminFlow.broadcast_confirming),
    F.data == "admin:bc:no",
)
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
    mode = data.get("broadcast_mode", "ru")
    audience = await _broadcast_audience(mode)
    # По одному источнику на язык: у каждой группы своё сообщение, и копируется
    # человеку именно оно.
    sources = {
        lang: (data.get(f"broadcast_{lang}_chat_id"), data.get(f"broadcast_{lang}_message_id"))
        for lang in audience
    }
    await state.clear()
    if any(chat_id is None or message_id is None for chat_id, message_id in sources.values()):
        await callback.answer("Сообщение потерялось, начни заново через /broadcast", show_alert=True)
        return

    await callback.answer("Рассылка запущена…")
    await ui.safe_edit(callback, "📢 Рассылка запущена, отчитаюсь по завершении…")

    sent = blocked = failed = 0
    for lang, telegram_ids in audience.items():
        src_chat_id, src_message_id = sources[lang]
        for telegram_id in telegram_ids:
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




# ---------- /ai_retry: ответить на вопрос, на котором тренер сорвался ----------
#
# Источник — лента user_events, а не ai_chat_messages: в вечный лог диалога
# сорвавшийся ход не пишется вовсе (вопрос и ответ ложатся туда парой только
# после ответа), а в ленте есть и сам вопрос (middleware пишет его до
# обработчика), и KIND_AI_FAILED сразу за ним.

AI_RETRY_LOOKBACK_DAYS = 7
_AI_RETRY_KINDS = (
    activity_log.KIND_MESSAGE,
    activity_log.KIND_REPLY_BUTTON,
    activity_log.KIND_CALLBACK,
    activity_log.KIND_AI_REPLY,
    activity_log.KIND_AI_FAILED,
)


async def find_failed_ai_question(telegram_id: int) -> Optional[dict]:
    """Последний сбой тренера за AI_RETRY_LOOKBACK_DAYS, после которого ответа
    не было, и вопрос перед ним. None — такого сбоя нет.

    question=None — сбой есть, но повторить нечего: ход начала кнопка, голос или
    фото (их в ленте нет, только пометка), тогда в asked — что это было.
    """
    now = dt.datetime.fromisoformat(db.now_iso())
    since = (now - dt.timedelta(days=AI_RETRY_LOOKBACK_DAYS)).isoformat(timespec="seconds")
    rows = await db.list_user_events_since(telegram_id, since, _AI_RETRY_KINDS)
    failed_at = None
    for i in range(len(rows) - 1, -1, -1):
        kind = rows[i]["kind"]
        if kind == activity_log.KIND_AI_REPLY:
            return None  # после последнего сбоя тренер уже отвечал
        if kind == activity_log.KIND_AI_FAILED:
            failed_at = i
            break
    if failed_at is None:
        return None
    failed = rows[failed_at]
    found = {
        "failed_id": failed["id"], "failed_at": failed["created_at"], "reason": failed["content"],
        "question": None, "asked": None, "asked_at": None,
    }
    for row in reversed(rows[:failed_at]):
        if row["kind"] in (activity_log.KIND_AI_REPLY, activity_log.KIND_AI_FAILED):
            break
        found["asked"], found["asked_at"] = row["content"], row["created_at"]
        content = (row["content"] or "").strip()
        if (
            row["kind"] == activity_log.KIND_MESSAGE
            and activity_log.is_plain_text(content)
            and not content.startswith("/")
        ):
            found["question"] = content
        break
    return found


async def _resolve_athlete(arg: str) -> Optional[int]:
    arg = arg.strip()
    if arg.lstrip("-").isdigit():
        user_id = int(arg)
        return user_id if await db.get_user(user_id) is not None else None
    return await db.find_user_id_by_username(arg)


async def _athlete_label(user_id: int) -> str:
    user = await db.get_user(user_id)
    if user is None:
        return str(user_id)
    return keyboards.admin_user_label(user["username"], keyboards.row_value(user, "email"), user_id)


@router.message(Command("ai_retry"))
async def cmd_ai_retry(message: Message, state: FSMContext, command: CommandObject | None = None):
    """Найти у атлета вопрос, на который тренер не ответил (сбой у нас), и после
    подтверждения ответить обычным ходом тренера — без списания квоты."""
    if not _is_admin(message.from_user.id):
        return
    arg = (command.args if command else None) or ""
    if not arg.strip():
        await message.answer(
            "Кому ответить: <code>/ai_retry 123456789</code> или <code>/ai_retry @username</code>",
            parse_mode="HTML",
        )
        return
    user_id = await _resolve_athlete(arg)
    if user_id is None:
        await message.answer(f"Не нашёл атлета {escape(arg.strip())} — проверь id или @ник.", parse_mode="HTML")
        return
    label = escape(await _athlete_label(user_id))
    found = await find_failed_ai_question(user_id)
    if found is None:
        await message.answer(
            f"У {label} за {AI_RETRY_LOOKBACK_DAYS} дн. нет вопроса без ответа — тренер ответил на всё.",
            parse_mode="HTML",
        )
        return
    failed_at = admin_time(found["failed_at"]).strftime("%d.%m %H:%M")
    if found["question"] is None:
        what = escape(_trim(found["asked"], 200)) if found["asked"] else "не нашёл, что он прислал"
        await message.answer(
            f"У {label} сбой тренера {failed_at}, но вопрос был не текстом ({what}) — "
            "повторить его я не могу.",
            parse_mode="HTML",
        )
        return
    user = await db.get_user(user_id)
    lang = (user["lang"] if user is not None else None) or i18n.DEFAULT_LANG
    asked_at = admin_time(found["asked_at"]).strftime("%d.%m %H:%M")
    truncated = len(found["question"]) >= activity_log.MAX_CONTENT_LEN
    await message.answer(
        f"🔁 Вопрос без ответа у {label}\n"
        f"Спросил: {asked_at}, сбой: {failed_at} ({escape(found['reason'])})\n\n"
        f"<blockquote>{escape(_trim(found['question'], 1500))}</blockquote>\n"
        + ("Вопрос в ленте обрезан — тренер получит только начало.\n" if truncated else "")
        + f"Ответить от тренера? Уйдёт атлету в чат на его языке ({lang}), квоту не спишу.",
        parse_mode="HTML",
        reply_markup=keyboards.yes_no_keyboard(
            f"admin:air:go:{user_id}:{found['failed_id']}", "admin:air:no",
            yes_text="Ответить", no_text="Отмена",
        ),
    )


@router.callback_query(F.data == "admin:air:no")
async def ai_retry_cancel(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    await ui.safe_edit(callback, "Отменил — атлету ничего не ушло.")
    await callback.answer()


@router.callback_query(F.data.startswith("admin:air:go:"))
async def ai_retry_confirm(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    from handlers import ai_trainer as ai_handler

    try:
        user_id, failed_id = (int(part) for part in callback.data.split(":")[3:5])
    except ValueError:
        await callback.answer("Кнопка битая — начни заново через /ai_retry", show_alert=True)
        return
    found = await find_failed_ai_question(user_id)
    if found is None or found["failed_id"] != failed_id or found["question"] is None:
        await callback.answer("Уже неактуально: тренер ответил или сбой сменился. Запусти /ai_retry заново.", show_alert=True)
        return
    label = escape(await _athlete_label(user_id))
    await callback.answer("Отвечаю…")
    await ui.safe_edit(callback, f"⏳ Тренер отвечает {label}…", parse_mode="HTML")
    logger.info("admin /ai_retry: answering user %s (failed event %s)", user_id, failed_id)
    try:
        answer = await ai_handler.retry_failed_question(callback.bot, state.storage, user_id, found["question"])
    except ai_handler.RetryBusy:
        result = "⏳ Атлет прямо сейчас ждёт другой ответ тренера — повтори через минуту."
    except ai_handler.RetryBlocked as exc:
        result = f"🛑 Не отправил: стоп по деньгам ({escape(str(exc))})."
    except TelegramForbiddenError:
        logger.info("admin /ai_retry: user %s blocked the bot", user_id)
        result = f"⚠️ Не отправил: {label} заблокировал бота."
    except Exception as exc:
        logger.exception("admin /ai_retry: failed for user %s", user_id)
        result = f"⚠️ Не отправил: {escape(type(exc).__name__)}: {escape(_trim(str(exc), 300))}"
    else:
        logger.info("admin /ai_retry: answered user %s, %d chars", user_id, len(answer))
        result = f"✅ Ответ отправлен {label}."
    with suppress(TelegramAPIError):
        await callback.message.answer(result, parse_mode="HTML")


# ---------- разовые релизные рассылки (announcements.py) ----------
#
# Механизм сам присылает админу анонс после разворота и ждёт добра — здесь
# живут кнопки под этим превью и ручной вход в тот же экран.

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
        if announcements.is_sending(ann.key):
            await message.answer(f"Релиз «{ann.key}» рассылаю прямо сейчас.")
            continue
        status = await db.get_announcement_status(ann.key)
        pending = await announcements.count_pending(ann)
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
    if announcements.is_sending(key):
        await callback.answer("Уже рассылаю.")
        return
    await db.set_announcement_status(key, announcements.STATUS_APPROVED)
    await callback.answer("Рассылка запущена…")
    await ui.safe_edit(callback, f"📢 Релиз «{key}» пошёл по базе, отчитаюсь по завершении…")
    # Отдельной задачей: рассылка на всю базу идёт минутами, а хендлер
    # callback'а столько держать нельзя — Telegram ждёт ответа секунды.
    # Гвард от повторного запуска (в том числе стартовой задачей) живёт внутри
    # deliver_and_report — см. announcements._sending.
    asyncio.create_task(announcements.deliver_and_report(callback.bot, ann))


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


# ---------- /admin_wipe: снести TEST_USER_ID для проверки онбординга ----------
#
# Нарочно не принимает id аргументом и нарочно не предлагает ADMIN_ID — это
# личный аккаунт админа с реальными данными, а не тестовый стенд. Выбирать
# можно только один-единственный заранее известный TEST_USER_ID, чтобы
# опечаткой или невнимательным тапом не задеть ни его, ни тем более живого
# постороннего пользователя.


async def _wipe_candidate() -> Optional[tuple[int, str]]:
    uid = config.TEST_USER_ID
    if uid is None:
        return None
    user = await db.get_user(uid)
    label = f"@{user['username']}" if user and user["username"] else str(uid)
    return uid, label


@router.message(Command("admin_wipe"))
async def cmd_admin_wipe(message: Message):
    if not _is_admin(message.from_user.id):
        return
    candidate = await _wipe_candidate()
    if candidate is None:
        await message.answer("TEST_USER_ID не настроен — сносить некого.")
        return
    uid, label = candidate
    b = InlineKeyboardBuilder()
    b.button(text=f"🧨 {label} ({uid})", callback_data=f"admin:wipe:ask:{uid}")
    b.adjust(1)
    await message.answer(
        "Снести тестовый аккаунт? Удалится ВСЁ — тренировки, вес, еда, программы, "
        "диалог с тренером, сам аккаунт. Следующий /start пройдёт как у нового.\n\n"
        "ADMIN_ID в списке нет и не будет — это личный аккаунт, не трогаем.",
        reply_markup=b.as_markup(),
    )


@router.callback_query(F.data.startswith("admin:wipe:ask:"))
async def admin_wipe_ask(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = int(callback.data.split(":")[3])
    if uid != config.TEST_USER_ID:
        await callback.answer("Сносить можно только TEST_USER_ID", show_alert=True)
        return
    candidate = await _wipe_candidate()
    label = candidate[1] if candidate else str(uid)
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
async def admin_wipe_go(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = int(callback.data.split(":")[3])
    if uid != config.TEST_USER_ID:
        await callback.answer("Сносить можно только TEST_USER_ID", show_alert=True)
        return
    try:
        left = await account_deletion.delete_account(uid, getattr(state, "storage", None))
    except Exception:
        # Снос идёт одной транзакцией: упало — значит не снеслось ничего. Молчать
        # тут нельзя, иначе «снёс» и «на месте» выглядят одинаково.
        logger.exception("Не смог снести аккаунт %s", uid)
        await ui.safe_edit(
            callback,
            f"Не снёс {uid} — база не дала, всё осталось на месте. Загляни в лог и жми ещё раз.",
        )
        await callback.answer("Не вышло", show_alert=True)
        return
    if left:
        tail = ", ".join(f"{table}: {count}" for table, count in sorted(left.items()))
        await ui.safe_edit(callback, f"Снёс {uid}, но кое-что уцелело — {tail}. Разбирайся.")
        await callback.answer("Снёс не всё", show_alert=True)
        return
    await ui.safe_edit(
        callback,
        f"Снёс {uid} целиком — ни строки в базе, ни черновика в диалоге. "
        "/start там теперь пройдёт как у новичка.",
    )
    await callback.answer("Готово")


# ---------- ответ в поддержку реплаем ----------
#
# Реплика атлета из приложения приходит админу в Telegram (api_v1_feedback.
# _send_feedback_to_admin). Реплай админа на неё — ответ в ветку поддержки:
# ложится в support_messages и уходит атлету пушем (api_v1_support.
# send_admin_reply). Ветка находится по message_id пересланного сообщения
# (support_messages.tg_admin_message_id), а у сообщений, отправленных до этой
# колонки, — по id в шапке «📱 Фидбек из приложения от id N».
#
# Фильтр, а не ранний return в хендлере: реплай на что угодно другое (отзыв из
# бота через copy_to, отчёт, бэкап) должен пройти мимо — дальше по роутерам,
# как и до этого хендлера. Хендлер последний в роутере: состояния рассылки
# выше по файлу забирают сообщение раньше.


async def _support_reply_target(message: Message) -> dict | bool:
    if message.from_user is None or not _is_admin(message.from_user.id):
        return False
    replied = message.reply_to_message
    if replied is None:
        return False
    if (message.text or "").startswith("/"):
        return False
    user_id = await db.support_user_by_tg_message(replied.message_id)
    if user_id is None:
        match = api_v1_feedback.ADMIN_HEADER_RE.match(replied.text or "")
        if match is None:
            return False
        user_id = int(match.group(1))
    return {"support_user_id": user_id}


@router.message(_support_reply_target)
async def support_reply(message: Message, support_user_id: int):
    text = (message.text or message.caption or "").strip()
    if not text:
        await message.reply("В приложение отвечаю только текстом — напиши словами.")
        return
    if len(text) > api_v1_support.MAX_REPLY_LENGTH:
        await message.reply(f"Длинно: уложись в {api_v1_support.MAX_REPLY_LENGTH} символов.")
        return
    if await db.get_user(support_user_id) is None:
        await message.reply(f"Пользователя {support_user_id} уже нет — ответить некому.")
        return
    try:
        _row, pushed = await api_v1_support.send_admin_reply(support_user_id, text)
    except Exception:
        logger.exception("support: не смог записать ответ пользователю %s", support_user_id)
        await message.reply("Не записал ответ — база не дала. Загляни в лог и попробуй ещё раз.")
        return
    push_note = f"пуш на {pushed} устр." if pushed else "пуша нет (нет iOS-токена или APNs)"
    await message.reply(f"✅ Ответ в поддержку для id {support_user_id} записан, {push_note}.")
