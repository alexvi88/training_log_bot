"""📤 Шаринг программ и упражнений между пользователями.

Механика — «переслал визитку»: владелец жмёт «Поделиться», бот отвечает
сообщением-визиткой с превью и URL-кнопкой (deep link `t.me/<bot>?start=sh_…`).
Визитку пересылают куда угодно — URL-кнопки, в отличие от callback-кнопок,
переживают пересылку. Получатель открывает ссылку, видит то же превью у себя
в боте и решает: «➕ Добавить себе» или нет. Ничего не импортируется без
явного согласия.

Шарится снапшот, а не живая ссылка (см. db.shared_items): владелец может потом
переименовать или удалить оригинал — визитка продолжит работать, а получатель
никогда не читает чужие живые строки.

Импорт резолвит упражнения по имени в три шага: своё с таким именем → форк
шаблона из каталога → создать новое под «Другое» (тот же компромисс, что у
«создать все» в CSV-импорте: группу можно поправить потом).
"""

import json
import logging
from html import escape
from typing import Any, Optional

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import db

logger = logging.getLogger(__name__)

router = Router(name="sharing")

START_PREFIX = "sh_"

# Снапшот не должен раздувать ни start-параметр (там только токен), ни превью.
MAX_SHARED_EXERCISES = 30
MAX_NAME_LEN = 80
MAX_DESCRIPTION_LEN = 1500

# Куда падают упражнения, чьё имя не нашлось ни у получателя, ни в каталоге.
FALLBACK_GROUP_NAME = "Другое"

_bot_username: Optional[str] = None


async def _get_bot_username(bot) -> str:
    global _bot_username
    if _bot_username is None:
        _bot_username = (await bot.get_me()).username
    return _bot_username


def _deep_link(username: str, token: str) -> str:
    return f"https://t.me/{username}?start={START_PREFIX}{token}"


# ---------- создание визитки ----------


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


def _routine_preview_lines(payload: dict[str, Any]) -> list[str]:
    lines = [f"🗂 <b>{escape(payload['name'])}</b>"]
    for i, ex in enumerate(payload["exercises"], start=1):
        suffix = f" — {escape(ex['target'])}" if ex.get("target") else ""
        lines.append(f"{i}. {escape(ex['name'])}{suffix}")
    return lines


def _program_preview_lines(payload: dict[str, Any]) -> list[str]:
    lines = [f"🗂 <b>{escape(payload['name'])}</b>"]
    for day in payload["days"]:
        lines.append(f"\n<b>{escape(day['name'])}</b>")
        for ex in day["exercises"]:
            suffix = f" — {escape(ex['target'])}" if ex.get("target") else ""
            lines.append(f"• {escape(ex['name'])}{suffix}")
    return lines


def _share_card_keyboard(url: str, label: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, url=url)]])


@router.callback_query(F.data.startswith("share:rt:"))
async def share_routine(callback: CallbackQuery, state: FSMContext):
    """«📤 Поделиться» на экране программы: отдельное сообщение-визитка,
    которое владелец пересылает кому хочет."""
    routine_id = int(callback.data.split(":")[2])
    routine = await db.get_routine(routine_id)
    if routine is None or routine["user_id"] != callback.from_user.id:
        await callback.answer("Программа не найдена", show_alert=True)
        return
    exercises = await db.list_routine_exercises(routine_id)
    if not exercises:
        await callback.answer("В программе нет упражнений — нечем делиться", show_alert=True)
        return

    payload = {
        "name": routine["name"][:MAX_NAME_LEN],
        "exercises": [
            {"name": ex["display_name"][:MAX_NAME_LEN], "target": ex["target"]}
            for ex in exercises[:MAX_SHARED_EXERCISES]
        ],
    }
    token = await db.create_shared_item(callback.from_user.id, "routine", json.dumps(payload, ensure_ascii=False))
    url = _deep_link(await _get_bot_username(callback.bot), token)

    text = "\n".join(
        _routine_preview_lines(payload)
        + ["", "<i>Перешли это сообщение — по кнопке программу можно забрать себе.</i>"]
    )
    await callback.message.answer(
        text, parse_mode="HTML", reply_markup=_share_card_keyboard(url, "➕ Забрать программу себе")
    )
    await callback.answer("Визитка готова — пересылай 📤")


@router.callback_query(F.data.startswith("share:pgm:"))
async def share_program(callback: CallbackQuery, state: FSMContext):
    """«📤 Поделиться программой» на экране списка дней: одна визитка на всю
    многодневку, а не по дню за раз (см. share_routine)."""
    anchor_id = int(callback.data.split(":")[2])
    anchor = await db.get_routine(anchor_id)
    if anchor is None or anchor["user_id"] != callback.from_user.id or not anchor["program_name"]:
        await callback.answer("Программа не найдена", show_alert=True)
        return
    days = await db.list_program_days(callback.from_user.id, anchor["program_name"])
    day_payloads = []
    for day in days:
        exercises = await db.list_routine_exercises(day["id"])
        if not exercises:
            continue
        day_payloads.append(
            {
                "name": day["name"][:MAX_NAME_LEN],
                "exercises": [
                    {"name": ex["display_name"][:MAX_NAME_LEN], "target": ex["target"]}
                    for ex in exercises[:MAX_SHARED_EXERCISES]
                ],
            }
        )
    if not day_payloads:
        await callback.answer("В программе нет упражнений — нечем делиться", show_alert=True)
        return

    payload = {"name": anchor["program_name"][:MAX_NAME_LEN], "days": day_payloads}
    token = await db.create_shared_item(callback.from_user.id, "program", json.dumps(payload, ensure_ascii=False))
    url = _deep_link(await _get_bot_username(callback.bot), token)

    text = "\n".join(
        _program_preview_lines(payload)
        + ["", "<i>Перешли это сообщение — по кнопке программу можно забрать себе.</i>"]
    )
    await callback.message.answer(
        text, parse_mode="HTML", reply_markup=_share_card_keyboard(url, "➕ Забрать программу себе")
    )
    await callback.answer("Визитка готова — пересылай 📤")


@router.callback_query(F.data.startswith("share:ex:"))
async def share_exercise(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    ex = await db.get_exercise(ex_id)
    if ex is None or ex["user_id"] != callback.from_user.id:
        await callback.answer("Упражнение не найдено", show_alert=True)
        return
    group = await db.get_muscle_group(ex["primary_group_id"]) if ex["primary_group_id"] else None
    description = (ex["description"] or "")[:MAX_DESCRIPTION_LEN] or None

    payload = {
        "name": ex["display_name"][:MAX_NAME_LEN],
        "group": group["name"] if group else None,
        "description": description,
        # file_id живёт в рамках одного бота — у получателя фото откроется.
        "photo_file_id": ex["custom_photo_file_id"],
    }
    token = await db.create_shared_item(callback.from_user.id, "exercise", json.dumps(payload, ensure_ascii=False))
    url = _deep_link(await _get_bot_username(callback.bot), token)

    lines = [f"🏋️ <b>{escape(payload['name'])}</b>"]
    if payload["group"]:
        lines.append(f"Группа: {escape(payload['group'])}")
    if description:
        lines.append("")
        lines.append(escape(description))
    lines += ["", "<i>Перешли это сообщение — по кнопке упражнение можно добавить себе.</i>"]
    await callback.message.answer(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=_share_card_keyboard(url, "➕ Добавить упражнение себе"),
    )
    await callback.answer("Визитка готова — пересылай 📤")


# ---------- превью у получателя ----------


def _accept_keyboard(token: str, kind: str) -> InlineKeyboardMarkup:
    label = "➕ Добавить упражнение себе" if kind == "exercise" else "➕ Добавить программу себе"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"share:add:{token}")],
        [InlineKeyboardButton(text="🏠 В меню", callback_data="share:skip")],
    ])


@router.message(CommandStart(deep_link=True, magic=F.args.startswith(START_PREFIX)))
async def open_shared(message: Message, command: CommandObject, state: FSMContext):
    """Получатель перешёл по ссылке из визитки: показать превью и спросить.

    Ничего не добавляется на этом шаге — «тебе прислали → ты смотришь → сам
    решаешь». Битый/устаревший токен не роняет /start: юзер получает внятный
    ответ и остаётся в боте.
    """
    await db.get_or_create_user(message.from_user.id, message.from_user.username)
    await state.clear()
    token = (command.args or "")[len(START_PREFIX):]
    row = await db.get_shared_item(token)
    if row is None:
        await message.answer("🤷 Эта ссылка устарела или битая. Открой меню: /start")
        return
    payload = json.loads(row["payload"])

    if row["kind"] == "program":
        n = len(payload["days"])
        head = f"Тебе прислали программу — {n} {_days_word(n)}.\n\n"
        text = head + "\n".join(_program_preview_lines(payload))
    elif row["kind"] == "routine":
        n = len(payload["exercises"])
        head = f"Тебе прислали программу — {n} упр.\n\n"
        text = head + "\n".join(_routine_preview_lines(payload))
    else:
        text = f"Тебе прислали упражнение:\n\n🏋️ <b>{escape(payload['name'])}</b>"
        if payload.get("group"):
            text += f"\nГруппа: {escape(payload['group'])}"
        if payload.get("description"):
            text += f"\n\n{escape(payload['description'])}"

    if row["owner_id"] == message.from_user.id:
        text += "\n\n<i>Это твоя собственная визитка — добавлять не нужно.</i>"
        await message.answer(text, parse_mode="HTML")
        return
    await message.answer(text, parse_mode="HTML", reply_markup=_accept_keyboard(token, row["kind"]))


@router.callback_query(F.data == "share:skip")
async def share_skip(callback: CallbackQuery, state: FSMContext):
    from handlers.workout import _show_main_menu
    await _show_main_menu(callback, state)
    await callback.answer()


# ---------- импорт ----------


async def _fallback_group_id(user_id: int) -> Optional[int]:
    groups = await db.list_muscle_groups(user_id)
    for g in groups:
        if g["name"].strip().lower() == FALLBACK_GROUP_NAME.lower():
            return g["id"]
    return groups[0]["id"] if groups else None


async def _resolve_group_id(user_id: int, group_name: Optional[str]) -> Optional[int]:
    if group_name:
        for g in await db.list_muscle_groups(user_id):
            if g["name"].strip().lower() == group_name.strip().lower():
                return g["id"]
    return await _fallback_group_id(user_id)


async def _resolve_exercise(user_id: int, name: str) -> int:
    """Своё по имени → форк шаблона каталога → создать под «Другое».

    В отличие от create_routine_from_program, нерезолвящееся имя не
    выбрасывается: в чужой программе кастомные названия — норма, и потерять
    половину упражнений при импорте хуже, чем создать их под «Другое»."""
    existing = await db.find_exercise_by_name(user_id, name)
    if existing:
        return existing["id"]
    forked = await db.get_or_create_user_exercise_by_name(user_id, name)
    if forked is not None:
        return forked
    return await db.create_exercise(user_id, name, await _fallback_group_id(user_id))


@router.callback_query(F.data.startswith("share:add:"))
async def share_add(callback: CallbackQuery, state: FSMContext):
    token = callback.data.split(":", 2)[2]
    row = await db.get_shared_item(token)
    if row is None:
        await callback.answer("Ссылка устарела", show_alert=True)
        return
    user_id = callback.from_user.id
    payload = json.loads(row["payload"])

    if row["kind"] == "program":
        for day in payload["days"]:
            routine_id = await db.create_routine(user_id, day["name"], program_name=payload["name"])
            order = 0
            seen: set[int] = set()
            for ex in day["exercises"]:
                ex_id = await _resolve_exercise(user_id, ex["name"])
                if ex_id in seen:
                    continue
                seen.add(ex_id)
                await db.add_routine_exercise(routine_id, ex_id, order, ex.get("target"))
                order += 1
        # Кнопку убираем: второй тап по «Добавить» иначе плодит дубликаты.
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            f"✅ Программа «{escape(payload['name'])}» у тебя — {len(payload['days'])} "
            f"{_days_word(len(payload['days']))} в 🗂 Программы.\n"
            "Новые упражнения легли в «Другое», группу можно поменять в ⚙️ Упражнения.",
            parse_mode="HTML",
        )
        await callback.answer("Добавил 👌")
        return

    if row["kind"] == "routine":
        routine_id = await db.create_routine(user_id, payload["name"])
        order = 0
        seen: set[int] = set()
        for ex in payload["exercises"]:
            ex_id = await _resolve_exercise(user_id, ex["name"])
            if ex_id in seen:
                continue
            seen.add(ex_id)
            await db.add_routine_exercise(routine_id, ex_id, order, ex.get("target"))
            order += 1
        # Кнопку убираем: второй тап по «Добавить» иначе плодит дубликаты.
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            f"✅ Программа «{escape(payload['name'])}» у тебя — 🗂 Программы.\n"
            "Новые упражнения легли в «Другое», группу можно поменять в ⚙️ Упражнения.",
            parse_mode="HTML",
        )
        await callback.answer("Добавил 👌")
        return

    # kind == "exercise"
    existing = await db.find_exercise_by_name(user_id, payload["name"])
    if existing:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer(f"«{payload['name']}» у тебя уже есть", show_alert=True)
        return
    group_id = await _resolve_group_id(user_id, payload.get("group"))
    ex_id = await db.create_exercise(user_id, payload["name"], group_id)
    if payload.get("description"):
        await db.set_exercise_description(ex_id, payload["description"])
    if payload.get("photo_file_id"):
        await db.set_exercise_photo(ex_id, payload["photo_file_id"])
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        f"✅ «{escape(payload['name'])}» добавлено — ⚙️ Упражнения.", parse_mode="HTML"
    )
    await callback.answer("Добавил 👌")
