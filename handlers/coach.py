"""🧑‍🏫 Сервис для тренеров в боте: живой тренер смотрит дневник своих атлетов.

Правила и расчёты — coach_service.py, строки — db.py (coach_*), тот же
продукт у приложения — REST api_v1_coach.py. Здесь только экраны Telegram, и
тексты у них те же, что у iOS (Features/CoachService), чтобы оба клиента
говорили одно и то же. Вход — «🔧 Настройки» → «🧑‍🏫 Тренер» (`coach:hub`) и
ссылка-приглашение: тренер раздаёт https-страницу `<сервер>/coach/<код>`
(coach_invite_page.py), её кнопка «Продолжить в Telegram» ведёт сюда
`t.me/<бот>?start=coach_<код>`.

Сторона атлета:
- ссылка или «У меня есть код тренера» → кто зовёт, что увидит и чего не
  увидит никогда, переключатель веса тела (по умолчанию закрыт) →
  «Разрешить» → как назваться тренеру. До «Разрешить» не открыто ничего;
- «Мой тренер»: переключатель веса тела и «Закрыть доступ»;
- комментарий тренера приходит сообщением (coach_notify) и стоит под
  тренировкой в истории (`athlete_comments_block`); назначенная программа —
  тоже сообщением, с кнопкой «Открыть программу».

Сторона тренера:
- «Я тренер»: имя, код и ссылка, «Новый код»;
- «Мои клиенты» с сигналами → карточка клиента (программы и сделанные дни,
  рекорды, тренировки, «📈 Прогресс» — частота, тоннаж по неделям, рекорды и
  графики основных движений, а под ними «Все упражнения» → прогресс любого
  упражнения тем же экраном, что у атлета (история прогресса, без правки), вес тела — только открытый атлетом) → тренировка на
  чтение с комментариями → «Написать комментарий»; «Назначить программу»,
  «Сводка от AI», «Убрать из клиентов»;
- атлет нажал «Разрешить» — тренеру сообщение «Новый клиент» (coach_notify).

Каждый id атлета из callback_data проверяется связкой (`_linked`) до любого
чтения — как у REST. Переписки с AI-тренером и AI-разбора тренировки тут нет
нигде: карточка тренировки для тренера собирается без комментария AI.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from contextlib import suppress
from html import escape
from typing import Any, Optional
from urllib.parse import quote

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import acquisition
import ai_limits
import ai_trainer
import busy_lock
import charts
import coach_notify
import coach_service
import config
import db
import formatting
import i18n
import keyboards
import progress_data
import timeutil
import ui
import weekly_summary
from fsm import CoachFlow
from handlers import history as history_screens
from handlers import sharing
from state_scaffold import clear_state_keep_workout

logger = logging.getLogger(__name__)

router = Router(name="coach")

START_PREFIX = acquisition.COACH_PREFIX

CLIENTS_PAGE_SIZE = 8
# «Все упражнения» клиента в «📈 Прогрессе» — кнопками, по столько на страницу.
EXERCISES_PAGE_SIZE = 8
WORKOUTS_PAGE_SIZE = 8
# Тренировок прямо в карточке клиента — дальше «📚 Все тренировки».
CARD_WORKOUTS = 3
# Столбики недель в карточке клиента: высота — доля самой плотной недели
# окна, пустая неделя — точка, а не самый низкий столбик (ноль ≠ одна).
WEEK_BARS = "▁▂▃▄▅▆▇█"
WEEK_EMPTY = "·"
# Подпись кнопки тренировки: дата и упражнения, длиннее — обрезка.
WORKOUT_LABEL_LIMIT = 48
# Сколько символов комментариев встаёт под карточкой тренировки: карточка
# режется по лимиту Telegram, и простыня комментариев не должна съедать
# сами подходы (formatting.fit_workout_text оставляет сводке не меньше 200).
COMMENTS_BUDGET = 1500

# Код из ссылки целиком: https://<сервер>/coach/<код> (страница приглашения),
# t.me/<бот>?start=coach_<код> или старая trainingdiary://coach/<код>.
_CODE_IN_LINK = re.compile(r"coach[_/]([A-Za-z0-9-]+)", re.IGNORECASE)


# ---------- общее ----------


def _back_row(cb: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=i18n.t("btn.back"), callback_data=cb)]


async def _show(event, text: str, kb: Optional[InlineKeyboardMarkup]) -> None:
    """Экран на месте старого (кнопка) или новым сообщением (ответ на текст)."""
    if isinstance(event, CallbackQuery):
        await ui.safe_edit(event, text, reply_markup=kb, parse_mode="HTML")
    else:
        await event.answer(ui.fit_to_limit(text, ui.TEXT_LIMIT, "HTML"), reply_markup=kb, parse_mode="HTML")


async def _leave_input(state: FSMContext) -> None:
    """Снять своё ожидание текста, не трогая чужие состояния и данные:
    ушёл с экрана ввода — следующий текст не должен стать именем или
    комментарием."""
    current = await state.get_state()
    if current and current.startswith("CoachFlow:"):
        await state.set_state(None)


def _extract_code(raw: str) -> str:
    """Код из того, что прислал человек: сам код или ссылка целиком (и
    телеграмная, и приложения)."""
    text = (raw or "").strip()
    match = _CODE_IN_LINK.search(text)
    if match:
        text = match.group(1)
    return coach_service.normalize_invite_code(text)


def _deep_link(username: str, code: str) -> str:
    return coach_service.telegram_link(username, code)


def _coach_name(name: Optional[str]) -> str:
    return name or i18n.t("coach.unnamed")


def _validated_name(raw: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """(имя, None) или (None, текст ошибки) — те же правила, что у REST."""
    name = coach_service.clean_display_name(raw)
    if name is None:
        return None, i18n.t("coach.name.empty")
    if len(name) > coach_service.MAX_DISPLAY_NAME_LENGTH:
        return None, i18n.t("coach.name.too_long", max=coach_service.MAX_DISPLAY_NAME_LENGTH)
    return name, None


def _local_day(started_at: str, user: Any) -> str:
    started = dt.datetime.fromisoformat(started_at)
    local = timeutil.to_user_local(started, user)
    return formatting.format_day_month_ru(local.date(), timeutil.user_today(user))


async def _int_args(callback: CallbackQuery, count: int) -> Optional[list[int]]:
    try:
        parts = callback.data.split(":")[2:]
        values = [int(p) for p in parts[:count]]
        if len(values) != count:
            raise ValueError
        return values
    except (ValueError, AttributeError):
        await callback.answer()
        return None


# ---------- 🧑‍🏫 Тренер: обе стороны на одном экране ----------


async def _hub_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    coaches = await db.list_client_coaches(user_id)
    profile = await db.get_coach_profile(user_id)
    lines = [i18n.t("coach.hub.title"), "", f"<b>{i18n.t('coach.hub.my_coach')}</b>"]
    kb = InlineKeyboardBuilder()
    if not coaches:
        lines.append(i18n.t("coach.hub.no_coach"))
    for row in coaches:
        name = _coach_name(row["coach_name"])
        lines.append(i18n.t("coach.hub.coach_line", name=escape(name), me=escape(row["client_name"])))
        kb.row(InlineKeyboardButton(text=f"🧑‍🏫 {name}", callback_data=f"coach:my:{row['coach_id']}"))
    kb.row(InlineKeyboardButton(text=i18n.t("coach.btn.enter_code"), callback_data="coach:code"))
    lines += ["", f"<b>{i18n.t('coach.hub.coach_mode')}</b>"]
    if profile is not None:
        clients = await db.list_coach_clients(user_id)
        lines.append(i18n.t("coach.hub.coach_state", name=escape(profile["display_name"])))
        lines.append(
            i18n.t("coach.client_count", n=len(clients)) if clients else i18n.t("coach.no_clients_yet")
        )
        kb.row(InlineKeyboardButton(
            text=i18n.t("coach.btn.my_clients", n=len(clients)), callback_data="coach:clients:0"
        ))
        kb.row(
            InlineKeyboardButton(text=i18n.t("coach.btn.invite"), callback_data="coach:invite"),
            InlineKeyboardButton(text=i18n.t("coach.btn.rename"), callback_data="coach:become"),
        )
    else:
        lines.append(i18n.t("coach.hub.become_intro"))
        kb.row(InlineKeyboardButton(text=i18n.t("coach.btn.become"), callback_data="coach:become"))
    kb.row(*_back_row("settings:menu"))
    return "\n".join(lines), kb.as_markup()


async def show_hub(event, state: FSMContext) -> None:
    await clear_state_keep_workout(state)
    text, kb = await _hub_view(event.from_user.id)
    await _show(event, text, kb)


@router.callback_query(F.data == "coach:hub")
async def coach_hub(callback: CallbackQuery, state: FSMContext):
    await show_hub(callback, state)
    await callback.answer()


# ---------- атлет: код → кто зовёт → «Разрешить» ----------


@router.callback_query(F.data == "coach:code")
async def coach_code_prompt(callback: CallbackQuery, state: FSMContext):
    await state.set_state(CoachFlow.entering_code)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=i18n.t("btn.cancel"), callback_data="coach:hub")]
    ])
    await ui.safe_edit(callback, i18n.t("coach.code.prompt"), reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(StateFilter(CoachFlow.entering_code), F.text)
async def coach_code_entered(message: Message, state: FSMContext):
    code = _extract_code(message.text)
    if not code:
        await message.answer(
            i18n.t("coach.code.empty"),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=i18n.t("btn.cancel"), callback_data="coach:hub")]
            ]),
        )
        return
    await _show_preview(message, state, message.from_user.id, code, False)


async def _invite_and_profile(code: str):
    invite = await db.get_coach_invite(code) if code else None
    profile = await db.get_coach_profile(invite["owner_id"]) if invite is not None else None
    if invite is None or profile is None:
        return None, None
    return invite, profile


def _flags(bw: bool) -> str:
    return str(int(bw))


def _toggle_label(key_on: str, key_off: str, on: bool) -> str:
    return i18n.t(key_on) if on else i18n.t(key_off)


async def _preview_view(user_id: int, code: str, bw: bool) -> tuple[str, InlineKeyboardMarkup, bool]:
    """(текст, кнопки, можно ли разрешать). Превью ничего не открывает."""
    invite, profile = await _invite_and_profile(code)
    retry_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=i18n.t("coach.btn.another_code"), callback_data="coach:code")],
        [InlineKeyboardButton(text=i18n.t("btn.home_menu"), callback_data="hist:menu")],
    ])
    if invite is None:
        return i18n.t("api.error.invite_not_found"), retry_kb, False
    coach_id = invite["owner_id"]
    name = escape(profile["display_name"])
    if coach_id == user_id:
        return i18n.t("coach.invite.own"), retry_kb, False
    if await db.get_coach_link(coach_id, user_id) is not None:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=i18n.t("coach.btn.my_coach"), callback_data=f"coach:my:{coach_id}")],
            [InlineKeyboardButton(text=i18n.t("btn.home_menu"), callback_data="hist:menu")],
        ])
        return i18n.t("coach.already_sees", name=name), kb, False
    text = "\n\n".join([
        f"<b>{i18n.t('coach.invite_title', name=name)}</b>",
        i18n.t("coach.invite.sees"),
        i18n.t("coach.invite.never"),
        i18n.t("coach.invite.optional"),
        i18n.t("coach.invite.close_any_time"),
    ])
    c = invite["code"]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=_toggle_label("coach.btn.bodyweight_on", "coach.btn.bodyweight_off", bw),
            callback_data=f"coach:inv:{c}:{_flags(not bw)}",
        )],
        [InlineKeyboardButton(text=i18n.t("coach.btn.allow"), callback_data=f"coach:ok:{c}:{_flags(bw)}")],
        [InlineKeyboardButton(text=i18n.t("coach.btn.not_now"), callback_data="coach:no")],
    ])
    return text, kb, True


async def _show_preview(event, state: FSMContext, user_id: int, code: str, bw: bool) -> None:
    text, kb, ok = await _preview_view(user_id, code, bw)
    if ok or isinstance(event, CallbackQuery):
        await _leave_input(state)
    await _show(event, text, kb)


@router.message(CommandStart(deep_link=True, magic=F.args.startswith(START_PREFIX)))
async def open_coach_invite(message: Message, command: CommandObject, state: FSMContext):
    """Атлет открыл ссылку тренера. Как и визитка (handlers/sharing.open_shared),
    это бывает самым первым входом в бота: новичку — язык по клиенту Telegram,
    нижняя клавиатура и источник «по коду тренера» с тренером автором."""
    from handlers.persistent_menu import attach_silently

    user_id = message.from_user.id
    is_new = await db.get_user(user_id) is None
    await db.get_or_create_user(user_id, message.from_user.username, message.from_user.language_code)
    code = _extract_code((command.args or "")[len(START_PREFIX):])
    if is_new:
        guessed_lang = i18n.normalize(getattr(message.from_user, "language_code", None))
        await db.set_user_lang(user_id, guessed_lang)
        i18n.set_lang(guessed_lang)
        await attach_silently(message, user_id)
        invite = await db.get_coach_invite(code) if code else None
        await db.set_user_source(
            user_id, acquisition.SOURCE_COACH_INVITE, invite["owner_id"] if invite is not None else None
        )
    await clear_state_keep_workout(state)
    await _show_preview(message, state, user_id, code, False)


def _parse_flags(raw: str) -> bool:
    """Флаг веса тела из callback_data. Кнопки в уже отправленных
    сообщениях несут второй, старый разряд (`10`, `01`) — он не читается."""
    return (raw or "0")[0] == "1"


@router.callback_query(F.data.startswith("coach:inv:"))
async def coach_invite_toggle(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    code = parts[2] if len(parts) > 2 else ""
    bw = _parse_flags(parts[3] if len(parts) > 3 else "")
    await _show_preview(callback, state, callback.from_user.id, code, bw)
    await callback.answer()


@router.callback_query(F.data == "coach:no")
async def coach_invite_decline(callback: CallbackQuery, state: FSMContext):
    await _leave_input(state)
    await state.update_data(coach_pending=None)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=i18n.t("btn.home_menu"), callback_data="hist:menu")]
    ])
    await ui.safe_edit(callback, i18n.t("coach.invite.declined"), reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("coach:ok:"))
async def coach_invite_allow(callback: CallbackQuery, state: FSMContext):
    """«Разрешить» → как назваться тренеру. Связка заводится только после
    имени: тренер знает атлета по имени, которым тот назвался сам."""
    parts = callback.data.split(":")
    code = parts[2] if len(parts) > 2 else ""
    bw = _parse_flags(parts[3] if len(parts) > 3 else "")
    text, kb, ok = await _preview_view(callback.from_user.id, code, bw)
    if not ok:
        await _show(callback, text, kb)
        await callback.answer()
        return
    _, profile = await _invite_and_profile(code)
    await state.set_state(CoachFlow.naming_self)
    await state.update_data(coach_pending={"code": code, "bw": bw})
    rows = []
    first, _err = _validated_name(getattr(callback.from_user, "first_name", None))
    if first:
        rows.append([InlineKeyboardButton(text=i18n.t("coach.btn.use_name", name=first), callback_data="coach:nm")])
    rows.append([InlineKeyboardButton(text=i18n.t("btn.cancel"), callback_data="coach:no")])
    await ui.safe_edit(
        callback, i18n.t("coach.name.prompt", coach=escape(profile["display_name"])),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "coach:nm")
async def coach_use_telegram_name(callback: CallbackQuery, state: FSMContext):
    name, error = _validated_name(getattr(callback.from_user, "first_name", None))
    if name is None:
        await callback.answer(error, show_alert=True)
        return
    await _accept(callback, state, name)
    await callback.answer()


@router.message(StateFilter(CoachFlow.naming_self), F.text)
async def coach_self_named(message: Message, state: FSMContext):
    name, error = _validated_name(message.text)
    if name is None:
        await message.answer(error)
        return
    await _accept(message, state, name)


async def _accept(event, state: FSMContext, name: str) -> None:
    user_id = event.from_user.id
    pending = (await state.get_data()).get("coach_pending") or {}
    await _leave_input(state)
    await state.update_data(coach_pending=None)
    code = pending.get("code") or ""
    text, kb, ok = await _preview_view(user_id, code, bool(pending.get("bw")))
    if not ok:
        await _show(event, text, kb)
        return
    invite, profile = await _invite_and_profile(code)
    created = await db.create_coach_link(
        invite["owner_id"], user_id, name,
        show_bodyweight=bool(pending.get("bw")),
    )
    coach_name = escape(profile["display_name"])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=i18n.t("coach.btn.my_coach"), callback_data=f"coach:my:{invite['owner_id']}")],
        [InlineKeyboardButton(text=i18n.t("btn.home_menu"), callback_data="hist:menu")],
    ])
    if not created:
        await _show(event, i18n.t("coach.already_sees", name=coach_name), kb)
        return
    text = f"<b>{i18n.t('coach.linked_done', name=coach_name)}</b>\n\n{i18n.t('coach.linked_done_hint')}"
    await _show(event, text, kb)
    # Тренеру — «новый клиент» банером и в Telegram, на его языке.
    await coach_notify.deliver_new_client(invite["owner_id"], name, bot=event.bot)


# ---------- атлет: «Мой тренер» ----------


async def _my_coach_view(user_id: int, coach_id: int) -> Optional[tuple[str, InlineKeyboardMarkup]]:
    link = await db.get_coach_link(coach_id, user_id)
    if link is None:
        return None
    profile = await db.get_coach_profile(coach_id)
    name = _coach_name(profile["display_name"] if profile is not None else None)
    bw = bool(link["show_bodyweight"])
    text = "\n\n".join([
        f"🧑‍🏫 <b>{escape(name)}</b>\n<i>{i18n.t('coach.knows_you_as', name=escape(link['client_name']))}</i>",
        i18n.t("coach.my.sees"),
        i18n.t("coach.my.toggles_hint"),
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=_toggle_label("coach.btn.bodyweight_on", "coach.btn.bodyweight_off", bw),
            callback_data=f"coach:myt:{coach_id}:bw",
        )],
        [InlineKeyboardButton(text=i18n.t("coach.btn.revoke"), callback_data=f"coach:rv:{coach_id}")],
        _back_row("coach:hub"),
    ])
    return text, kb


async def _show_my_coach(callback: CallbackQuery, state: FSMContext, coach_id: int) -> None:
    await _leave_input(state)
    view = await _my_coach_view(callback.from_user.id, coach_id)
    if view is None:
        await callback.answer(i18n.t("coach.link_gone"), show_alert=True)
        await show_hub(callback, state)
        return
    await _show(callback, *view)
    await callback.answer()


@router.callback_query(F.data.startswith("coach:my:"))
async def coach_my(callback: CallbackQuery, state: FSMContext):
    args = await _int_args(callback, 1)
    if args is not None:
        await _show_my_coach(callback, state, args[0])


@router.callback_query(F.data.startswith("coach:myt:"))
async def coach_my_toggle(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    try:
        coach_id = int(parts[2])
    except (IndexError, ValueError):
        await callback.answer()
        return
    link = await db.get_coach_link(coach_id, callback.from_user.id)
    # Другой хвост (старая кнопка второго переключателя в уже отправленном
    # сообщении) ничего не меняет — просто перерисовывает экран.
    if link is not None and parts[-1] == "bw":
        await db.update_coach_link_visibility(
            coach_id, callback.from_user.id, show_bodyweight=not bool(link["show_bodyweight"])
        )
    await _show_my_coach(callback, state, coach_id)


@router.callback_query(F.data.startswith("coach:rv:"))
async def coach_revoke_ask(callback: CallbackQuery, state: FSMContext):
    args = await _int_args(callback, 1)
    if args is None:
        return
    coach_id = args[0]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=i18n.t("coach.btn.revoke"), callback_data=f"coach:rvy:{coach_id}")],
        [InlineKeyboardButton(text=i18n.t("btn.cancel"), callback_data=f"coach:my:{coach_id}")],
    ])
    await ui.safe_edit(callback, i18n.t("coach.revoke.confirm"), reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("coach:rvy:"))
async def coach_revoke(callback: CallbackQuery, state: FSMContext):
    args = await _int_args(callback, 1)
    if args is None:
        return
    coach_id = args[0]
    profile = await db.get_coach_profile(coach_id)
    removed = await db.delete_coach_link(coach_id, callback.from_user.id)
    await clear_state_keep_workout(state)
    text, kb = await _hub_view(callback.from_user.id)
    if removed:
        name = _coach_name(profile["display_name"] if profile is not None else None)
        text = f"{i18n.t('coach.access_closed', name=escape(name))}\n\n{text}"
    await _show(callback, text, kb)
    await callback.answer()


# ---------- тренер: «Я тренер», имя, приглашение ----------


@router.callback_query(F.data == "coach:become")
async def coach_become_prompt(callback: CallbackQuery, state: FSMContext):
    await state.set_state(CoachFlow.naming_coach)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=i18n.t("btn.cancel"), callback_data="coach:hub")]
    ])
    await ui.safe_edit(callback, i18n.t("coach.become.prompt"), reply_markup=kb)
    await callback.answer()


@router.message(StateFilter(CoachFlow.naming_coach), F.text)
async def coach_named(message: Message, state: FSMContext):
    name, error = _validated_name(message.text)
    if name is None:
        await message.answer(error)
        return
    user_id = message.from_user.id
    await db.upsert_coach_profile(user_id, name)
    if await db.get_coach_invite_for(user_id) is None:
        await db.replace_coach_invite(user_id, coach_service.new_invite_code())
    await _leave_input(state)
    text, kb = await _invite_view(message.bot, user_id)
    await _show(message, f"{i18n.t('coach.name_saved', name=escape(name))}\n\n{text}", kb)


async def _invite_view(bot, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    profile = await db.get_coach_profile(user_id)
    invite = await db.get_coach_invite_for(user_id)
    if invite is None:
        await db.replace_coach_invite(user_id, coach_service.new_invite_code())
        invite = await db.get_coach_invite_for(user_id)
    code = invite["code"]
    # Главная ссылка — https-страница приглашения: её открывает любой
    # мессенджер и любой телефон (с приложением — сразу в нём, без — выбор
    # «приложение, App Store или Telegram»). Без публичного адреса сервера —
    # прежняя ссылка в бота.
    link = coach_service.web_link(code) or _deep_link(await sharing.get_bot_username(bot), code)
    full_text = coach_service.invite_share_text(profile["display_name"], code, link)
    # Telegram сам ставит ссылку первой строкой над `text` — в `text` её нет,
    # иначе в сообщении было бы две одинаковые ссылки.
    share_text = "\n".join([
        i18n.t("coach.invite_share.lead", name=profile["display_name"]),
        i18n.t("coach.invite_share.code", code=code),
    ])
    share_url = f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(share_text, safe='')}"
    text = i18n.t("coach.invite_screen", link=escape(link), code=code)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=i18n.t("coach.btn.share"), url=share_url)],
        [InlineKeyboardButton(
            text=i18n.t("coach.btn.copy_link"), copy_text=CopyTextButton(text=full_text)
        )],
        [
            InlineKeyboardButton(text=i18n.t("coach.btn.my_clients_short"), callback_data="coach:clients:0"),
            InlineKeyboardButton(text=i18n.t("coach.btn.renew"), callback_data="coach:renew"),
        ],
        _back_row("coach:hub"),
    ])
    return text, kb


async def _require_coach(callback: CallbackQuery, state: FSMContext) -> bool:
    if await db.get_coach_profile(callback.from_user.id) is not None:
        return True
    await callback.answer(i18n.t("api.error.not_a_coach"), show_alert=True)
    await show_hub(callback, state)
    return False


@router.callback_query(F.data == "coach:invite")
async def coach_invite_screen(callback: CallbackQuery, state: FSMContext):
    await _leave_input(state)
    if not await _require_coach(callback, state):
        return
    text, kb = await _invite_view(callback.bot, callback.from_user.id)
    await _show(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data == "coach:renew")
async def coach_renew_ask(callback: CallbackQuery, state: FSMContext):
    if not await _require_coach(callback, state):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=i18n.t("coach.btn.renew_confirm"), callback_data="coach:renewy")],
        [InlineKeyboardButton(text=i18n.t("btn.cancel"), callback_data="coach:invite")],
    ])
    await ui.safe_edit(callback, i18n.t("coach.renew.confirm"), reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "coach:renewy")
async def coach_renew(callback: CallbackQuery, state: FSMContext):
    """Новый код вместо прежнего: старый перестаёт открываться, уже связанные
    атлеты остаются — их доступ держит связка, а не код."""
    if not await _require_coach(callback, state):
        return
    await db.replace_coach_invite(callback.from_user.id, coach_service.new_invite_code())
    text, kb = await _invite_view(callback.bot, callback.from_user.id)
    await _show(callback, f"{i18n.t('coach.invite_renewed')}\n\n{text}", kb)
    await callback.answer()


# ---------- тренер: «Мои клиенты» ----------


def signal_texts(signals: dict[str, Any]) -> list[str]:
    """Плашки строки клиента — только то, что подтверждают данные; порядок
    тот же, что у приложения (CoachSignal.list): сначала кого пора пнуть."""
    out = []
    if signals["missing"]:
        days = signals["days_since_last_workout"]
        out.append(
            i18n.t("coach.signal.missing_days", n=days) if days is not None
            else i18n.t("coach.signal.no_workouts")
        )
    if signals["behind_program"]:
        out.append(i18n.t("coach.signal.behind_program"))
    if signals["new_records"] > 0:
        out.append(i18n.t("coach.signal.records", n=signals["new_records"]))
    if signals["workouts_this_week"] > 0:
        out.append(i18n.t("coach.signal.week", n=signals["workouts_this_week"]))
    return out


def _has_warning(signals: dict[str, Any]) -> bool:
    return bool(signals["missing"] or signals["behind_program"])


async def _clients_with_signals(coach_id: int) -> list[tuple[Any, Any, dict[str, Any]]]:
    out = []
    for link in await db.list_coach_clients(coach_id):
        client = await db.get_user(link["client_id"])
        if client is not None:
            out.append((link, client, await coach_service.client_signals(client, link, coach_id)))
    # Сначала те, у кого предупреждение, потом остальные — каждая группа по
    # имени (CoachClientOrder у приложения): тренер открывает список ради
    # первой строки.
    out.sort(key=lambda item: (not _has_warning(item[2]), item[0]["client_name"].casefold()))
    return out


async def _clients_view(coach_id: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
    items = await _clients_with_signals(coach_id)
    kb = InlineKeyboardBuilder()
    head = i18n.t("coach.clients.title")
    if not items:
        kb.row(InlineKeyboardButton(text=i18n.t("coach.btn.invite"), callback_data="coach:invite"))
        kb.row(*_back_row("coach:hub"))
        return f"{head}\n\n{i18n.t('coach.clients.empty')}", kb.as_markup()
    pages = max(1, (len(items) + CLIENTS_PAGE_SIZE - 1) // CLIENTS_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    shown = items[page * CLIENTS_PAGE_SIZE:(page + 1) * CLIENTS_PAGE_SIZE]
    blocks = [head]
    for link, _client, signals in shown:
        tags = signal_texts(signals) or [i18n.t("coach.signal.none")]
        blocks.append(f"<b>{escape(link['client_name'])}</b>\n{' · '.join(tags)}")
        kb.row(InlineKeyboardButton(text=link["client_name"], callback_data=f"coach:cl:{link['client_id']}"))
    blocks.append(f"<i>{i18n.t('coach.clients.ai_hint')}</i>")
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text=keyboards.PAGE_PREV_TEXT(), callback_data=f"coach:clients:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text=keyboards.PAGE_NEXT_TEXT(), callback_data=f"coach:clients:{page + 1}"))
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text=i18n.t("coach.btn.ai_summary"), callback_data="coach:ai"))
    kb.row(*_back_row("coach:hub"))
    return "\n\n".join(blocks), kb.as_markup()


@router.callback_query(F.data.startswith("coach:clients:"))
async def coach_clients(callback: CallbackQuery, state: FSMContext):
    await _leave_input(state)
    if not await _require_coach(callback, state):
        return
    args = await _int_args(callback, 1)
    if args is None:
        return
    text, kb = await _clients_view(callback.from_user.id, args[0])
    await _show(callback, text, kb)
    await callback.answer()


async def _linked(callback: CallbackQuery, client_id: int):
    """Связка и строка атлета — или отказ тем же текстом, что у REST
    (`not_linked`): чужой дневник не отличить от несуществующего."""
    link = await db.get_coach_link(callback.from_user.id, client_id)
    client = await db.get_user(client_id) if link is not None else None
    if link is None or client is None:
        await callback.answer(i18n.t("api.error.not_linked"), show_alert=True)
        return None, None
    return link, client


def visibility_line(link: Any) -> str:
    if link["show_bodyweight"]:
        return i18n.t("coach.visibility.bodyweight")
    return i18n.t("coach.visibility.none")


def _workout_button(w: Any, names: list[str], client: Any, commented: bool, client_id: int) -> InlineKeyboardButton:
    label = _local_day(w["started_at"], client)
    if names:
        label += f" · {', '.join(names)}"
    if commented:
        label = f"📝 {label}"
    return InlineKeyboardButton(
        text=keyboards._shorten_label(label, WORKOUT_LABEL_LIMIT),
        callback_data=f"coach:w:{client_id}:{w['id']}",
    )


def week_bars(weekly: list[dict[str, Any]]) -> str:
    """Тренировки по неделям одной строкой: «·▂▅▂█» — от старых к новым,
    последняя — текущая неделя."""
    peak = max((w["workouts"] for w in weekly), default=0)
    if peak <= 0:
        return WEEK_EMPTY * len(weekly)
    top = len(WEEK_BARS) - 1
    return "".join(
        WEEK_BARS[max(0, round(w["workouts"] / peak * top))] if w["workouts"] else WEEK_EMPTY
        for w in weekly
    )


def _activity_block(activity: dict[str, Any], client: Any) -> Optional[str]:
    """Неделя за неделей — сразу под шапкой, как в приложении: частота и
    столбики по неделям. Дневник пуст — блока нет (плашка «без тренировок»
    уже сказала это); одна тренировка за окно — только частота: столбик
    среди пустых недель не показывает динамику."""
    if not activity["total_workouts"]:
        return None
    freq = activity["frequency"]
    rows = [
        i18n.t("coach.client.weeks_header"),
        i18n.t(
            "coach.progress.frequency", weeks=activity["weeks"], n=freq["workouts"],
            avg=formatting.format_weight(freq["per_week"]), this_week=freq["this_week"],
        ),
    ]
    if freq["workouts"] >= 2:
        first = dt.date.fromisoformat(activity["weekly"][0]["week_start"])
        rows.append(f"<code>{week_bars(activity['weekly'])}</code>")
        rows.append(i18n.t(
            "coach.client.weeks_scale",
            date=escape(formatting.format_day_month_ru(first, timeutil.user_today(client))),
        ))
    return "\n".join(rows)


async def _client_view(coach_id: int, link: Any, client: Any) -> tuple[str, InlineKeyboardMarkup]:
    """Карточка клиента в порядке приложения (вариант «тёмная шапка + список
    разделов»): кто и как у него неделя → тренировки по неделям → программы
    и рекорды. Кнопки: два действия тренера, разделы, три свежие тренировки
    и «Убрать из клиентов» — последней строкой, красной, отдельно от всего."""
    client_id = link["client_id"]
    signals = await coach_service.client_signals(client, link, coach_id)
    lines = [f"<b>{escape(link['client_name'])}</b>", " · ".join(signal_texts(signals) or [i18n.t("coach.signal.none")])]
    if signals["last_workout_at"]:
        when = formatting.format_day_month_ru(
            dt.date.fromisoformat(signals["last_workout_at"]), timeutil.user_today(client)
        )
        lines.append(i18n.t("coach.last_workout", date=when))
    lines.append(f"<i>{visibility_line(link)}</i>")
    blocks = ["\n".join(lines)]

    activity = _activity_block(await coach_service.client_activity(client), client)
    if activity:
        blocks.append(activity)

    programs = await coach_service.assigned_programs(coach_id, client_id)
    if programs:
        rows = [i18n.t("coach.client.programs_header")]
        for p in programs:
            rows.append(i18n.t(
                "coach.client.program_line", name=escape(p["name"]),
                done=p["done_days"], total=len(p["days"]),
            ))
        blocks.append("\n".join(rows))
    records = await coach_service.recent_records(client)
    if records:
        rows = [i18n.t("coach.client.records_header")]
        rows += [
            i18n.t("coach.client.record_line", exercise=escape(r["exercise"]), best=escape(r["best_set"]))
            for r in records
        ]
        blocks.append("\n".join(rows))

    workouts = await db.list_workouts(client_id, limit=CARD_WORKOUTS + 1, status="finished")
    more = len(workouts) > CARD_WORKOUTS
    workouts = workouts[:CARD_WORKOUTS]
    blocks.append(
        f"{i18n.t('coach.client.workouts_header')}\n"
        + (i18n.t("coach.client.workouts_hint") if workouts else i18n.t("coach.client.no_workouts"))
    )
    kb = InlineKeyboardBuilder()
    # Действия тренера — сверху, как главная и вторая кнопки в шапке
    # приложения; дальше разделы, потом свежие тренировки.
    kb.row(InlineKeyboardButton(text=i18n.t("coach.btn.assign"), callback_data=f"coach:asg:{client_id}"))
    kb.row(InlineKeyboardButton(text=i18n.t("coach.btn.ai_summary"), callback_data=f"coach:ai:{client_id}"))
    kb.row(InlineKeyboardButton(text=i18n.t("coach.btn.progress"), callback_data=f"coach:pg:{client_id}"))
    if link["show_bodyweight"]:
        kb.row(InlineKeyboardButton(text=i18n.t("coach.btn.bodyweight"), callback_data=f"coach:bw:{client_id}"))
    ids = [w["id"] for w in workouts]
    contents = await db.list_workout_contents(ids)
    comments = await db.count_coach_comments_by_workout(ids)
    for w in workouts:
        names, _sets = contents.get(w["id"], ([], 0))
        names = coach_service.shown_exercise_names(names)
        kb.row(_workout_button(w, names, client, comments.get(w["id"], 0) > 0, client_id))
    if more:
        kb.row(InlineKeyboardButton(text=i18n.t("coach.btn.all_workouts"), callback_data=f"coach:ws:{client_id}:0"))
    kb.row(*_back_row("coach:clients:0"))
    # Опасное — последним и красным (Telegram красит кнопку со style=danger;
    # клиент без поддержки покажет обычную — она всё равно ниже «Назад»).
    kb.row(InlineKeyboardButton(text=i18n.t("coach.btn.remove"), callback_data=f"coach:rm:{client_id}", style="danger"))
    return "\n\n".join(blocks), kb.as_markup()


async def _show_client(callback: CallbackQuery, state: FSMContext, client_id: int, notice: str = "") -> None:
    await _leave_input(state)
    link, client = await _linked(callback, client_id)
    if link is None:
        return
    text, kb = await _client_view(callback.from_user.id, link, client)
    await _show(callback, f"{notice}\n\n{text}" if notice else text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("coach:cl:"))
async def coach_client(callback: CallbackQuery, state: FSMContext):
    args = await _int_args(callback, 1)
    if args is not None:
        await _show_client(callback, state, args[0])


@router.callback_query(F.data.startswith("coach:ws:"))
async def coach_client_workouts(callback: CallbackQuery, state: FSMContext):
    args = await _int_args(callback, 2)
    if args is None:
        return
    client_id, page = args
    link, client = await _linked(callback, client_id)
    if link is None:
        return
    page = max(0, page)
    workouts = await db.list_workouts(
        client_id, limit=WORKOUTS_PAGE_SIZE + 1, offset=page * WORKOUTS_PAGE_SIZE, status="finished"
    )
    has_next = len(workouts) > WORKOUTS_PAGE_SIZE
    workouts = workouts[:WORKOUTS_PAGE_SIZE]
    ids = [w["id"] for w in workouts]
    contents = await db.list_workout_contents(ids)
    comments = await db.count_coach_comments_by_workout(ids)
    kb = InlineKeyboardBuilder()
    for w in workouts:
        names, _sets = contents.get(w["id"], ([], 0))
        names = coach_service.shown_exercise_names(names)
        kb.row(_workout_button(w, names, client, comments.get(w["id"], 0) > 0, client_id))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text=keyboards.PAGE_PREV_TEXT(), callback_data=f"coach:ws:{client_id}:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text=keyboards.PAGE_NEXT_TEXT(), callback_data=f"coach:ws:{client_id}:{page + 1}"))
    if nav:
        kb.row(*nav)
    kb.row(*_back_row(f"coach:cl:{client_id}"))
    body = i18n.t("coach.client.workouts_hint") if workouts else i18n.t("coach.client.no_workouts")
    text = f"<b>{escape(link['client_name'])}</b>\n{i18n.t('coach.client.workouts_header')}\n\n{body}"
    await _show(callback, text, kb.as_markup())
    await callback.answer()


# ---------- 📈 прогресс клиента ----------

# Недель текстом на экране прогресса — свежие сверху; остальные недели окна
# (coach_service.PROGRESS_WEEKS) есть в частоте и на графиках.
PROGRESS_TEXT_WEEKS = 8
# Точек списком под графиком движения — свежие сверху.
LIFT_CAPTION_POINTS = 6


def _chartable(lift: dict[str, Any]) -> bool:
    """График — от двух недель: одна точка — это точка, а не линия (тот же
    порог, что у графика упражнения в истории)."""
    return len(lift["points"]) >= 2


def _best_text(record: dict[str, Any], unit: str) -> str:
    if record["metric"] == "reps":
        return i18n.t("coach.progress.reps", n=record["best_reps"])
    e1rm = f"{formatting.format_weight(record['best_e1rm'])}{formatting.unit_label(unit)}"
    return i18n.t("coach.progress.set_e1rm", set=record["best_set"], e1rm=e1rm)


def _progress_view(link: Any, client: Any, data: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    client_id = link["client_id"]
    today = timeutil.user_today(client)
    head = i18n.t("coach.progress.title", name=escape(link["client_name"]))
    kb = InlineKeyboardBuilder()
    if not data["total_workouts"]:
        kb.row(*_back_row(f"coach:cl:{client_id}"))
        return f"{head}\n\n{i18n.t('coach.progress.empty')}", kb.as_markup()

    freq = data["frequency"]
    blocks = [head + "\n" + i18n.t(
        "coach.progress.frequency", weeks=data["weeks"], n=freq["workouts"],
        avg=formatting.format_weight(freq["per_week"]), this_week=freq["this_week"],
    )]
    rows = [i18n.t("coach.progress.weeks_header")]
    for week in list(reversed(data["weekly"]))[:PROGRESS_TEXT_WEEKS]:
        date = escape(formatting.format_day_month_ru(dt.date.fromisoformat(week["week_start"]), today))
        if week["workouts"]:
            rows.append(i18n.t(
                "coach.progress.week_line", date=date, n=week["workouts"],
                tonnage=weekly_summary.tonnage_label(week["tonnage"], data["unit"]),
            ))
        else:
            rows.append(i18n.t("coach.progress.week_empty", date=date))
    blocks.append("\n".join(rows))

    if data["recent_records"]:
        rows = [i18n.t("coach.progress.recent_header", days=data["recent_record_days"])]
        rows += [
            i18n.t("coach.client.record_line", exercise=escape(r["exercise"]), best=escape(r["best_set"]))
            for r in data["recent_records"]
        ]
        blocks.append("\n".join(rows))
    if data["records"]:
        rows = [i18n.t("coach.progress.best_header")]
        rows += [
            i18n.t(
                "coach.client.record_line", exercise=escape(r["exercise"]),
                best=escape(_best_text(r, data["unit"])),
            )
            for r in data["records"]
        ]
        blocks.append("\n".join(rows))

    lifts = [lift for lift in data["lifts"] if _chartable(lift)]
    blocks.append(i18n.t("coach.progress.charts_hint") if lifts else i18n.t("coach.progress.charts_few"))
    for lift in lifts:
        kb.row(InlineKeyboardButton(
            text=keyboards._shorten_label(i18n.t("coach.btn.progress_lift", exercise=lift["exercise"]), WORKOUT_LABEL_LIMIT),
            callback_data=f"coach:pgl:{client_id}:{lift['exercise_id']}",
        ))
    # Под основными движениями — любое другое: тот же экран прогресса
    # упражнения, что атлет видит у себя (coach:px → coach:pe).
    kb.row(InlineKeyboardButton(text=i18n.t("coach.btn.all_exercises"), callback_data=f"coach:px:{client_id}:0"))
    kb.row(*_back_row(f"coach:cl:{client_id}"))
    return "\n\n".join(blocks), kb.as_markup()


@router.callback_query(F.data.startswith("coach:pg:"))
async def coach_client_progress(callback: CallbackQuery, state: FSMContext):
    args = await _int_args(callback, 1)
    if args is None:
        return
    await _leave_input(state)
    link, client = await _linked(callback, args[0])
    if link is None:
        return
    data = await coach_service.client_progress(client)
    text, kb = _progress_view(link, client, data)
    await _show(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("coach:pgl:"))
async def coach_client_lift_chart(callback: CallbackQuery, state: FSMContext):
    """График основного движения клиента по неделям — та же картинка, что у
    атлета на экране прогресса упражнения (charts.render_metric_over_sessions),
    только точка — лучшая тренировка недели. Упражнение берётся из расчёта
    прогресса этого же клиента, а не по id из кнопки: чужое упражнение туда не
    попадёт."""
    args = await _int_args(callback, 2)
    if args is None:
        return
    client_id, exercise_id = args
    link, client = await _linked(callback, client_id)
    if link is None:
        return
    data = await coach_service.client_progress(client)
    lifts = [lift for lift in data["lifts"] if _chartable(lift)]
    lift = next((x for x in lifts if x["exercise_id"] == exercise_id), None)
    if lift is None:
        await callback.answer(i18n.t("coach.progress.lift_gone"), show_alert=True)
        return
    today = timeutil.user_today(client)
    unit = formatting.unit_label(data["unit"])
    points = [
        (dt.datetime.combine(dt.date.fromisoformat(p["week_start"]), dt.time()), float(p["value"]))
        for p in lift["points"]
    ]
    png = await asyncio.to_thread(
        charts.render_metric_over_sessions,
        points,
        f"{lift['exercise']} — {lift['metric_label']}",
        # подпись оси — с единицами: «e1RM, кг»; у повторов единиц нет
        lift["metric_label"] if lift["metric"] == "reps" else f"{lift['metric_label']}, {unit}",
        show_weekly_rate=False,
        mark_records=True,
    )
    lines = [i18n.t("coach.progress.lift_caption", exercise=escape(lift["exercise"]), metric=lift["metric_label"])]
    for p in list(reversed(lift["points"]))[:LIFT_CAPTION_POINTS]:
        date = escape(formatting.format_day_month_ru(dt.date.fromisoformat(p["week_start"]), today))
        if lift["metric"] == "reps":
            best = i18n.t("coach.progress.reps", n=p["value"])
        else:
            best = i18n.t(
                "coach.progress.set_e1rm", set=p["top_set"] or "",
                e1rm=f"{formatting.format_weight(p['value'])}{unit}",
            )
        lines.append(i18n.t("coach.progress.lift_point", date=date, best=escape(best)))
    kb = InlineKeyboardBuilder()
    for other in lifts:
        if other["exercise_id"] != exercise_id:
            kb.row(InlineKeyboardButton(
                text=keyboards._shorten_label(
                    i18n.t("coach.btn.progress_lift", exercise=other["exercise"]), WORKOUT_LABEL_LIMIT
                ),
                callback_data=f"coach:pgl:{client_id}:{other['exercise_id']}",
            ))
    kb.row(*_back_row(f"coach:pg:{client_id}"))
    await ui.safe_edit_photo(
        callback, png, "progress.png", "\n".join(lines), reply_markup=kb.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


# ---------- 📈 любое упражнение клиента ----------


async def _exercises_view(link: Any, client: Any, page: int) -> tuple[str, InlineKeyboardMarkup]:
    """«Все упражнения» клиента: всё, в чём есть законченные подходы, частые и
    свежие выше (coach_service.client_exercises), страницами."""
    client_id = link["client_id"]
    items = await coach_service.client_exercises(client)
    head = i18n.t("coach.exercises.title", name=escape(link["client_name"]))
    kb = InlineKeyboardBuilder()
    if not items:
        kb.row(*_back_row(f"coach:pg:{client_id}"))
        return f"{head}\n\n{i18n.t('coach.exercises.empty')}", kb.as_markup()
    pages = max(1, (len(items) + EXERCISES_PAGE_SIZE - 1) // EXERCISES_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    for item in items[page * EXERCISES_PAGE_SIZE:(page + 1) * EXERCISES_PAGE_SIZE]:
        kb.row(InlineKeyboardButton(
            text=keyboards._shorten_label(
                i18n.t("coach.exercises.button", exercise=item["exercise"], n=item["sessions"]),
                WORKOUT_LABEL_LIMIT,
            ),
            callback_data=f"coach:pe:{client_id}:{item['exercise_id']}:{keyboards.DEFAULT_PROGRESS_LIMIT}:{page}",
        ))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text=keyboards.PAGE_PREV_TEXT(), callback_data=f"coach:px:{client_id}:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text=keyboards.PAGE_NEXT_TEXT(), callback_data=f"coach:px:{client_id}:{page + 1}"))
    if nav:
        kb.row(*nav)
    kb.row(*_back_row(f"coach:pg:{client_id}"))
    return f"{head}\n\n{i18n.t('coach.exercises.hint')}", kb.as_markup()


@router.callback_query(F.data.startswith("coach:px:"))
async def coach_client_exercises(callback: CallbackQuery, state: FSMContext):
    args = await _int_args(callback, 2)
    if args is None:
        return
    await _leave_input(state)
    client_id, page = args
    link, client = await _linked(callback, client_id)
    if link is None:
        return
    text, kb = await _exercises_view(link, client, page)
    await _show(callback, text, kb)
    await callback.answer()


def _exercise_progress_keyboard(client_id: int, exercise_id: int, limit: int, page: int) -> InlineKeyboardMarkup:
    """Те же периоды, что у атлета под графиком (keyboards.PROGRESS_PERIODS),
    и без кнопок правки: дневник атлета тренеру — только на чтение."""
    kb = InlineKeyboardBuilder()
    for value, label in keyboards.PROGRESS_PERIODS:
        text = f"• {label} •" if value == limit else str(label)
        kb.button(text=text, callback_data=f"coach:pe:{client_id}:{exercise_id}:{value}:{page}")
    kb.adjust(len(keyboards.PROGRESS_PERIODS))
    kb.row(*_back_row(f"coach:px:{client_id}:{page}"))
    return kb.as_markup()


@router.callback_query(F.data.startswith("coach:pe:"))
async def coach_client_exercise_progress(callback: CallbackQuery, state: FSMContext):
    """Прогресс одного упражнения клиента — ровно тот экран, что атлет видит
    на «📈 Прогресс → упражнение» (history.progress_screen: график, золотая
    книга, подходы по дням с заметками), по его формуле и в его единице, а
    подписи — на языке тренера. Сначала связка, потом — что упражнение этого
    атлета: id из кнопки мог быть подменён."""
    args = await _int_args(callback, 4)
    if args is None:
        return
    await _leave_input(state)
    client_id, exercise_id, limit, page = args
    link, client = await _linked(callback, client_id)
    if link is None:
        return
    exercise = await coach_service.client_exercise(client, exercise_id)
    if exercise is None:
        await callback.answer(i18n.t("coach.exercises.gone"), show_alert=True)
        return
    limit = max(1, min(limit, 9999))
    sessions = await progress_data.load_sessions(exercise_id, client["e1rm_formula"])
    notes = await db.list_workout_notes_for_exercise(exercise_id)
    name = sharing.shown_exercise_name(exercise["display_name"])
    text, png = await history_screens.progress_screen(name, sessions, notes, client, limit)
    kb = _exercise_progress_keyboard(client_id, exercise_id, limit, page)
    if png:
        await ui.safe_edit_photo(callback, png, "chart.png", text, reply_markup=kb, parse_mode="HTML")
    else:
        await ui.safe_edit(callback, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


# ---------- тренировка клиента: на чтение + комментарии ----------


def _clip_comments(rows: list[Any], render) -> list[str]:
    """Свежие комментарии, пока влезают в COMMENTS_BUDGET; самый свежий —
    всегда (он и так не длиннее MAX_COMMENT_LENGTH)."""
    out: list[str] = []
    used = 0
    for row in reversed(rows):
        chunk = render(row)
        if out and used + len(chunk) > COMMENTS_BUDGET:
            break
        out.append(chunk)
        used += len(chunk)
    return list(reversed(out))


async def _coach_workout_text(coach_id: int, link: Any, client: Any, workout: Any) -> str:
    """Карточка законченной тренировки атлета глазами тренера: подходы,
    рекорды и тоннаж — тем же кодом, что у самого атлета, но без разбора
    AI-тренера (его тренер не видит никогда) и с комментариями тренера."""
    from handlers.workout import _finished_summary

    # Каталожные упражнения атлета — на языке тренера (coach_service.shown_exercise_names).
    summary_fn, tonnage, _duration = await _finished_summary(
        workout, client, workout["note"], shown_name=sharing.shown_exercise_name
    )
    suffix = ""
    equivalent = formatting.format_tonnage_equivalent(tonnage, seed=workout["id"], unit=client["unit"])
    if equivalent:
        suffix += "\n\n" + i18n.t(
            "workout.session_tonnage_total",
            tonnage=formatting.format_tonnage(tonnage, client["unit"]), equivalent=equivalent,
        )
    comments = await db.list_coach_comments(workout["id"], coach_id=coach_id)

    def render(row) -> str:
        when = _local_day(row["created_at"], client)
        stamp = i18n.t("coach.comment_read", date=when) if row["read_at"] else when
        return f"<i>{escape(stamp)}</i>\n{escape(row['text'])}"

    if comments:
        suffix += "\n\n" + i18n.t("coach.workout.comments_header") + "\n" + "\n\n".join(_clip_comments(comments, render))
    else:
        suffix += "\n\n<i>" + i18n.t("coach.workout.comment_hint") + "</i>"
    head = f"<b>{escape(link['client_name'])}</b>\n\n"
    return head + formatting.fit_workout_text(summary_fn, suffix, limit=formatting.MESSAGE_LIMIT - len(head))


async def _client_workout(callback: CallbackQuery, client_id: int, workout_id: int):
    link, client = await _linked(callback, client_id)
    if link is None:
        return None, None, None
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != client_id or workout["status"] != "finished":
        await ui.alert_workout_not_found(callback)
        return None, None, None
    return link, client, workout


async def _show_client_workout(
    callback: CallbackQuery, state: FSMContext, client_id: int, workout_id: int, notice: str = ""
) -> None:
    await _leave_input(state)
    link, client, workout = await _client_workout(callback, client_id, workout_id)
    if link is None:
        return
    text = await _coach_workout_text(callback.from_user.id, link, client, workout)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=i18n.t("coach.btn.write_comment"), callback_data=f"coach:cm:{client_id}:{workout_id}")],
        _back_row(f"coach:cl:{client_id}"),
    ])
    await _show(callback, f"{notice}\n\n{text}" if notice else text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("coach:w:"))
async def coach_client_workout(callback: CallbackQuery, state: FSMContext):
    args = await _int_args(callback, 2)
    if args is not None:
        await _show_client_workout(callback, state, *args)


@router.callback_query(F.data.startswith("coach:cm:"))
async def coach_comment_prompt(callback: CallbackQuery, state: FSMContext):
    args = await _int_args(callback, 2)
    if args is None:
        return
    client_id, workout_id = args
    if not await _require_coach(callback, state):
        return
    link, client, workout = await _client_workout(callback, client_id, workout_id)
    if link is None:
        return
    await state.set_state(CoachFlow.writing_comment)
    await state.update_data(coach_comment={"cid": client_id, "wid": workout_id, "text": None})
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=i18n.t("btn.cancel"), callback_data=f"coach:w:{client_id}:{workout_id}")]
    ])
    text = i18n.t(
        "coach.comment.prompt", name=escape(link["client_name"]),
        date=escape(_local_day(workout["started_at"], client)), max=coach_service.MAX_COMMENT_LENGTH,
    )
    await ui.safe_edit(callback, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(StateFilter(CoachFlow.writing_comment), F.text)
async def coach_comment_written(message: Message, state: FSMContext):
    """Текст комментария — сначала превью и «Отправить»: этот текст уйдёт
    живому человеку, и случайная строка (подход, набранный не в тот экран)
    не должна улететь ему сама."""
    draft = (await state.get_data()).get("coach_comment") or {}
    if not draft.get("cid") or not draft.get("wid"):
        await _leave_input(state)
        await message.answer(i18n.t("coach.comment.draft_gone"))
        return
    text = (message.text or "").strip()
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=i18n.t("btn.cancel"), callback_data=f"coach:w:{draft['cid']}:{draft['wid']}")]
    ])
    if not text:
        await message.answer(i18n.t("api.error.text_empty"), reply_markup=cancel_kb)
        return
    if len(text) > coach_service.MAX_COMMENT_LENGTH:
        await message.answer(
            i18n.t("coach.comment.too_long", n=len(text), max=coach_service.MAX_COMMENT_LENGTH),
            reply_markup=cancel_kb,
        )
        return
    link = await db.get_coach_link(message.from_user.id, draft["cid"])
    if link is None:
        await _leave_input(state)
        await message.answer(i18n.t("api.error.not_linked"))
        return
    await state.update_data(coach_comment={**draft, "text": text})
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=i18n.t("coach.btn.send"), callback_data="coach:cms")],
        [InlineKeyboardButton(text=i18n.t("coach.btn.rewrite"), callback_data=f"coach:cm:{draft['cid']}:{draft['wid']}")],
        [InlineKeyboardButton(text=i18n.t("btn.cancel"), callback_data=f"coach:w:{draft['cid']}:{draft['wid']}")],
    ])
    await message.answer(
        i18n.t("coach.comment.preview", name=escape(link["client_name"]), text=escape(text)),
        reply_markup=kb, parse_mode="HTML",
    )


@router.callback_query(F.data == "coach:cms")
async def coach_comment_send(callback: CallbackQuery, state: FSMContext):
    draft = (await state.get_data()).get("coach_comment") or {}
    text = draft.get("text")
    if not text or not draft.get("cid") or not draft.get("wid"):
        await callback.answer(i18n.t("coach.comment.draft_gone"), show_alert=True)
        return
    profile = await db.get_coach_profile(callback.from_user.id)
    if profile is None:
        await callback.answer(i18n.t("api.error.not_a_coach"), show_alert=True)
        return
    link, client, workout = await _client_workout(callback, draft["cid"], draft["wid"])
    if link is None:
        return
    # Снимаем черновик до записи: второй тап по «Отправить» не должен
    # отправить тот же комментарий ещё раз.
    await state.update_data(coach_comment=None)
    await _leave_input(state)
    await db.add_coach_comment(callback.from_user.id, draft["cid"], draft["wid"], text)
    await coach_notify.deliver_comment(
        client, profile["display_name"], text, draft["wid"], bot=callback.bot
    )
    notice = i18n.t("coach.comment.sent", name=escape(link["client_name"]))
    await _show_client_workout(callback, state, draft["cid"], draft["wid"], notice=notice)


# ---------- вес тела — только открытый атлетом ----------


@router.callback_query(F.data.startswith("coach:bw:"))
async def coach_client_bodyweight(callback: CallbackQuery, state: FSMContext):
    args = await _int_args(callback, 1)
    if args is None:
        return
    link, client = await _linked(callback, args[0])
    if link is None:
        return
    if not link["show_bodyweight"]:
        await callback.answer(i18n.t("api.error.coach_hidden"), show_alert=True)
        return
    rows = list(reversed(await db.list_bodyweight_logs(args[0], limit=10)))
    unit = formatting.unit_label(client["unit"])
    lines = [i18n.t("coach.bodyweight.title", name=escape(link["client_name"]))]
    if not rows:
        lines.append(i18n.t("coach.bodyweight.empty"))
    for r in rows:
        when = _local_day(r["logged_at"], client)
        lines.append(f"{escape(when)} — <b>{formatting.format_weight(r['weight'])}{unit}</b>")
    kb = InlineKeyboardMarkup(inline_keyboard=[_back_row(f"coach:cl:{args[0]}")])
    await _show(callback, "\n".join(lines), kb)
    await callback.answer()


@router.callback_query(F.data.startswith("coach:fd:"))
async def coach_client_legacy_button(callback: CallbackQuery, state: FSMContext):
    """Кнопка первой версии карточки клиента в уже отправленном сообщении:
    такого экрана больше нет — открываем саму карточку."""
    args = await _int_args(callback, 1)
    if args is not None:
        await _show_client(callback, state, args[0])


# ---------- назначить программу ----------


@router.callback_query(F.data.startswith("coach:asg:"))
async def coach_assign_pick(callback: CallbackQuery, state: FSMContext):
    args = await _int_args(callback, 1)
    if args is None:
        return
    if not await _require_coach(callback, state):
        return
    link, _client = await _linked(callback, args[0])
    if link is None:
        return
    programs = await db.list_programs(callback.from_user.id)
    kb = InlineKeyboardBuilder()
    for p in programs:
        kb.row(InlineKeyboardButton(
            text=f"🗂 {p['name']} · {i18n.t('btn.program_days', n=p['day_count'])}",
            callback_data=f"coach:asp:{args[0]}:{p['id']}",
        ))
    kb.row(*_back_row(f"coach:cl:{args[0]}"))
    body = i18n.t("coach.assign.intro") if programs else i18n.t("coach.assign.empty")
    text = f"{i18n.t('coach.assign.title', name=escape(link['client_name']))}\n\n{body}"
    await _show(callback, text, kb.as_markup())
    await callback.answer()


_ASSIGN_ERROR_KEYS = {
    "not_found": "api.error.not_found",
    "coach_program_empty": "api.error.coach_program_empty",
    "client_routine_limit": "api.error.client_routine_limit",
    "name_taken": "coach.assign.retry",
}


@router.callback_query(F.data.startswith("coach:asp:"))
async def coach_assign(callback: CallbackQuery, state: FSMContext):
    args = await _int_args(callback, 2)
    if args is None:
        return
    client_id, program_id = args
    if not await _require_coach(callback, state):
        return
    link, client = await _linked(callback, client_id)
    if link is None:
        return
    try:
        new_id, name = await coach_service.assign_program(callback.from_user.id, client_id, program_id)
    except coach_service.AssignError as exc:
        await callback.answer(i18n.t(_ASSIGN_ERROR_KEYS.get(exc.code, "api.error.not_found")), show_alert=True)
        return
    await _show_client(callback, state, client_id, notice=i18n.t("coach.program_assigned", name=escape(name)))
    # Атлету — «программа от тренера» банером и в Telegram, на его языке.
    profile = await db.get_coach_profile(callback.from_user.id)
    coach_name = profile["display_name"] if profile is not None else None
    await coach_notify.deliver_program(client, coach_name, name, new_id, bot=callback.bot)


# ---------- убрать из клиентов ----------


@router.callback_query(F.data.startswith("coach:rm:"))
async def coach_remove_ask(callback: CallbackQuery, state: FSMContext):
    args = await _int_args(callback, 1)
    if args is None:
        return
    link, _client = await _linked(callback, args[0])
    if link is None:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=i18n.t("coach.btn.remove"), callback_data=f"coach:rmy:{args[0]}", style="danger")],
        [InlineKeyboardButton(text=i18n.t("btn.cancel"), callback_data=f"coach:cl:{args[0]}")],
    ])
    await ui.safe_edit(
        callback, i18n.t("coach.remove.confirm", name=escape(link["client_name"])),
        reply_markup=kb, parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("coach:rmy:"))
async def coach_remove(callback: CallbackQuery, state: FSMContext):
    args = await _int_args(callback, 1)
    if args is None:
        return
    link, _client = await _linked(callback, args[0])
    if link is None:
        return
    await db.delete_coach_link(callback.from_user.id, args[0])
    text, kb = await _clients_view(callback.from_user.id, 0)
    await _show(callback, f"{i18n.t('coach.removed', name=escape(link['client_name']))}\n\n{text}", kb)
    await callback.answer()


# ---------- 🤖 сводка от AI ----------


def _summary_view(
    link: Any, result: dict[str, Any], *, saved_on: Optional[str] = None, client_id: Optional[int] = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Сводка сообщением: заголовок, текст, кто не вошёл. saved_on — дата
    сохранённой сводки: тогда под текстом строка «Сводка от …» и кнопка
    «Обновить сводку» (платный вызов — только по ней)."""
    head = (
        i18n.t("coach.ai_summary.header_one", name=escape(link["client_name"])) if link is not None
        else i18n.t("coach.ai_summary.header_all")
    )
    parts = [head]
    if result["text"]:
        parts.append(formatting.ai_markdown_to_html(result["text"]))
    else:
        parts.append(i18n.t("coach.summary_none_one" if link is not None else "coach.summary_none"))
    if result["text"] and result["skipped_no_consent"]:
        parts.append(f"<i>{i18n.t('coach.summary_skipped', n=result['skipped_no_consent'])}</i>")
    if saved_on is not None:
        parts.append(f"<i>{i18n.t('coach.ai_summary.saved_on', date=escape(saved_on))}</i>")
    back_cb = f"coach:cl:{client_id}" if client_id is not None else "coach:clients:0"
    rows = []
    if saved_on is not None:
        refresh_cb = f"coach:ai_new:{client_id}" if client_id is not None else "coach:ai_new"
        rows.append([InlineKeyboardButton(text=i18n.t("coach.btn.ai_summary_refresh"), callback_data=refresh_cb)])
    rows.append(_back_row(back_cb))
    text = ui.fit_to_limit("\n\n".join(parts), ui.TEXT_LIMIT, "HTML")
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _ai_summary(
    callback: CallbackQuery, state: FSMContext, client_id: Optional[int], *, refresh: bool = False,
) -> None:
    """Сводка тренеру — по всем клиентам или по одному.

    Есть сохранённая (coach_service.saved_summary: та же пара, тот же язык,
    не старше срока) и тренер не жал «Обновить» — показываем её с датой,
    бесплатно. Иначе — тот же путь, что у REST: coach_service.ai_summary →
    paid_call (лимит вопроса тренеру и цена в лог), та же бронь «один платный
    ход за раз», что у чата с тренером в боте (handlers.ai_trainer._busy)."""
    from handlers import ai_trainer as ai_chat

    coach_id = callback.from_user.id
    if not await _require_coach(callback, state):
        return
    link = None
    if client_id is not None:
        link, _client = await _linked(callback, client_id)
        if link is None:
            return
    if not refresh:
        saved = await coach_service.saved_summary(coach_id, client_id)
        if saved is not None:
            coach = await db.get_user(coach_id)
            saved_on = _local_day(saved["created_at"], coach) if coach is not None else saved["created_at"][:10]
            text, kb = _summary_view(link, saved, saved_on=saved_on, client_id=client_id)
            await callback.answer()
            await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
            return
    if not ai_trainer.is_configured():
        await callback.answer(i18n.t("ai.screen.not_configured"), show_alert=True)
        return
    if not busy_lock.try_claim(ai_chat._busy, coach_id):
        await callback.answer(i18n.t("ai.screen.busy"), show_alert=True)
        return
    back_cb = f"coach:cl:{client_id}" if client_id is not None else "coach:clients:0"
    back_kb = InlineKeyboardMarkup(inline_keyboard=[_back_row(back_cb)])
    try:
        await callback.answer()
        placeholder = await callback.message.answer(i18n.t("coach.ai_summary.thinking"))
        try:
            result = await asyncio.wait_for(
                coach_service.ai_summary(coach_id, client_id), timeout=config.AI_TOTAL_ANSWER_SECONDS
            )
        except ai_trainer.LimitBlocked as exc:
            await ai_limits.reply(callback.message, exc.block, reply_markup=back_kb)
            return
        except Exception:
            logger.exception("coach: ai summary failed for %s", coach_id)
            await callback.message.answer(i18n.t("ai.screen.answer_failed"), reply_markup=back_kb)
            return
        finally:
            with suppress(Exception):
                await placeholder.delete()
    finally:
        ai_chat._busy.discard(coach_id)

    text, kb = _summary_view(link, result, client_id=client_id)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "coach:ai")
async def coach_ai_all(callback: CallbackQuery, state: FSMContext):
    await _ai_summary(callback, state, None)


@router.callback_query(F.data.startswith("coach:ai:"))
async def coach_ai_one(callback: CallbackQuery, state: FSMContext):
    args = await _int_args(callback, 1)
    if args is not None:
        await _ai_summary(callback, state, args[0])


@router.callback_query(F.data == "coach:ai_new")
async def coach_ai_all_refresh(callback: CallbackQuery, state: FSMContext):
    await _ai_summary(callback, state, None, refresh=True)


@router.callback_query(F.data.startswith("coach:ai_new:"))
async def coach_ai_one_refresh(callback: CallbackQuery, state: FSMContext):
    args = await _int_args(callback, 1)
    if args is not None:
        await _ai_summary(callback, state, args[0], refresh=True)


# ---------- комментарии тренера под тренировкой атлета ----------


async def athlete_comments_block(workout_id: int, user: Any) -> str:
    """Блок «🧑‍🏫 Твой тренер» под карточкой своей тренировки в истории (пусто
    — комментариев нет). Открыл — прочитал, как у GET /workouts/{id}/coach-comments."""
    comments = await db.list_coach_comments(workout_id)
    if not comments:
        return ""
    await db.mark_coach_comments_read(workout_id, user["telegram_id"])

    def render(row) -> str:
        name = escape(_coach_name(row["coach_name"]))
        when = escape(_local_day(row["created_at"], user))
        return f"<b>{name}</b> · <i>{when}</i>\n{escape(row['text'])}"

    return "\n\n" + i18n.t("coach.card.header") + "\n" + "\n\n".join(_clip_comments(comments, render))
