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

import acquisition
import config
import db
from formatting import MESSAGE_LIMIT, telegram_length
from state_scaffold import clear_state_keep_ai

logger = logging.getLogger(__name__)

router = Router(name="sharing")

START_PREFIX = "sh_"

# Снапшот не должен раздувать ни start-параметр (там только токен), ни превью.
MAX_SHARED_EXERCISES = 30
# 6×30 уже даёт 8023 символа против лимита Telegram в 4096 — без этого
# «Поделиться программой» на большой многодневке молча ничего не отправляет
# (TelegramBadRequest из callback.message.answer никто не ловит). Порог берём
# не «впритык»: даже 6×15 с длинными именами (MAX_NAME_LEN) может не влезть,
# поэтому _program_preview_lines ниже всё равно режет текст по факту, а не
# полагается только на эти счётчики снапшота.
MAX_SHARED_DAYS = 6

# Пределы визитки — те же, что у ручного ввода, а не свои собственные. Раньше
# здесь стоял общий MAX_NAME_LEN = 80 на всё, и принятая программа могла выйти
# длиннее, чем разрешает её же переименование (48), а упражнение — длиннее, чем
# разрешает создание (60). Исправить такое имя было нечем: экран переименования
# отвечал «слишком длинное» на то самое имя, которое сам и завёл. Лимиты не
# случайны — имя едет прямо в подпись кнопки списка.
MAX_NAME_LEN = config.MAX_EXERCISE_NAME_LENGTH
MAX_PROGRAM_NAME_LEN = config.MAX_PROGRAM_NAME_LENGTH
MAX_DESCRIPTION_LEN = config.MAX_EXERCISE_DESCRIPTION_LENGTH

# Версия формата payload'а в shared_items — токены уже гуляющие в чатах были
# созданы без поля "v" (читатели ниже трактуют его отсутствие как v=0), но с
# этого момента пишем версию всегда: ретрофитить её в старые визитки было бы
# нельзя, а стоит это сейчас — ничего.
#
# v2 добавил "total_days": сколько дней было в программе до обрезки. У визиток
# v0/v1 этого числа нет и восстановить его нечем (снапшот уже обрезан), поэтому
# по ним считаем, что уехало всё — см. _program_days_totals.
#
# v3 добавил "empty_days": сколько дней не уехало из-за пустоты. Причин
# недобора две — пустой день и предел визитки, — а поля было одно, и причину
# угадывали по счётчику: «ровно MAX_SHARED_DAYS в визитке — значит упёрлись в
# лимит». Программа из шести рабочих дней и двух пустых уезжала со всем
# содержимым, но получала «уехало 6 из 8 — больше не влезает»: потери не было,
# а бот утверждал обратное. Теперь причины считаются, а не угадываются.
#
# v4 добавила в снапшот программы описание (programs.description) — старые
# визитки его просто не несут, поэтому на импорте оно читается через .get.
PAYLOAD_VERSION = 4

# Превью должно гарантированно влезать в лимит Telegram при ЛЮБОМ снапшоте —
# в том числе созданном до появления MAX_SHARED_DAYS. Резервируем место под
# шапку получателя («Тебе прислали программу — N дней.») и футер-подсказку
# (вместе с предупреждением про недоехавшие дни, см. _omitted_days_note),
# которые дописываются поверх результата _program_preview_lines /
# _routine_preview_lines и на превью-бюджет не претендуют.
_PREVIEW_HEAD_RESERVE = 200
_PREVIEW_FOOTER_RESERVE = 200
PREVIEW_BUDGET = MESSAGE_LIMIT - _PREVIEW_HEAD_RESERVE - _PREVIEW_FOOTER_RESERVE

# Куда падают упражнения, чьё имя не нашлось ни у получателя, ни в каталоге.
FALLBACK_GROUP_NAME = "Другое"

_bot_username: Optional[str] = None


async def get_bot_username(bot) -> str:
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


def _program_days_totals(payload: dict[str, Any]) -> tuple[int, int]:
    """(сколько дней реально в визитке, сколько было в программе).

    Второе число — из "total_days" (payload v2+). У визиток постарше поля нет, и
    честного ответа по ним уже не получить, поэтому считаем, что уехало всё:
    придумывать потерю хуже, чем промолчать о ней.
    """
    in_card = len(payload["days"])
    return in_card, max(int(payload.get("total_days", in_card)), in_card)


def _omitted_days_note(payload: dict[str, Any]) -> Optional[str]:
    """Предупреждение про дни, которых в визитке нет вовсе — или None.

    Это не то же, что «…и ещё K дней» в превью: там дни в визитке есть, просто
    не поместились в текст сообщения, а здесь получатель их не получит совсем.
    Раньше про такую потерю не узнавал никто: восьмидневная программа уезжала
    пятью днями, отправителю бот говорил «Визитка готова», получателю — «5
    дней», и обе стороны считали, что передали программу целиком.
    """
    in_card, total = _program_days_totals(payload)
    if in_card >= total:
        return None
    return f"⚠️ Уехало {in_card} {_days_word(in_card)} из {total} — {_omitted_reason(payload)}."


def _omitted_reason(payload: dict[str, Any]) -> str:
    """Почему уехали не все дни. Причин две, и раньше их путали: обе выводились
    из одного счётчика по правилу «ровно MAX_SHARED_DAYS в визитке — значит
    лимит». Шесть рабочих дней и два пустых уезжали со всем содержимым, а
    человек читал «больше не влезает» — про потерю, которой не было."""
    in_card, total = _program_days_totals(payload)
    empty = int(payload.get("empty_days", 0))
    # v0–v2 счётчика пустых дней не несут, и восстановить его нечем — там
    # называем факт без причины, а не выдумываем её.
    if int(payload.get("v", 0)) < 3:
        return "визитка вместила не всё"
    over_limit = total - in_card - empty
    if empty and over_limit > 0:
        return "пустые дни не передаются, а остальное не влезло в одну визитку"
    if empty:
        return "пустые дни не передаются"
    return "больше в одну визитку не влезает"


def _routine_preview_lines(payload: dict[str, Any], budget: int = PREVIEW_BUDGET) -> list[str]:
    """Как _program_preview_lines ниже, но для одного дня/шаблона: список
    режется по бюджету символов, а не только по MAX_SHARED_EXERCISES снапшота."""
    header = f"🗂 <b>{escape(payload['name'])}</b>"
    lines = [header]
    exercises = payload["exercises"]
    shown = 0
    for i, ex in enumerate(exercises, start=1):
        suffix = f" — {escape(ex['target'])}" if ex.get("target") else ""
        candidate = f"{i}. {escape(ex['name'])}{suffix}"
        if shown > 0 and telegram_length("\n".join(lines + [candidate])) > budget:
            break
        lines.append(candidate)
        shown += 1
    remaining = len(exercises) - shown
    if remaining > 0:
        lines.append(f"…и ещё {remaining} упражн.")
    return lines


def _program_preview_lines(payload: dict[str, Any], budget: int = PREVIEW_BUDGET) -> list[str]:
    """Строит превью многодневки, гарантируя, что итог уложится в `budget`
    символов (Telegram-счёт, см. formatting.telegram_length) — независимо от
    того, сколько дней/упражнений реально лежит в снапшоте.

    Существующие визитки могли быть созданы до появления MAX_SHARED_DAYS, так
    что режем по факту: сначала сокращаем список упражнений внутри дня
    («…и ещё K»), а если не влезает даже один день целиком — обрезаем список
    дней («…и ещё K дней») и останавливаемся."""
    lines = [f"🗂 <b>{escape(payload['name'])}</b>"]
    days = payload["days"]
    shown_days = 0
    for day in days:
        day_lines = [f"\n<b>{escape(day['name'])}</b>"]
        exercises = day["exercises"]
        shown_ex = 0
        for ex in exercises:
            suffix = f" — {escape(ex['target'])}" if ex.get("target") else ""
            candidate = f"• {escape(ex['name'])}{suffix}"
            trial = lines + day_lines + [candidate]
            if shown_ex > 0 and telegram_length("\n".join(trial)) > budget:
                break
            day_lines.append(candidate)
            shown_ex += 1
        remaining_ex = len(exercises) - shown_ex
        if remaining_ex > 0:
            day_lines.append(f"…и ещё {remaining_ex} упражн.")

        trial_total = lines + day_lines
        if shown_days > 0 and telegram_length("\n".join(trial_total)) > budget:
            break
        lines = trial_total
        shown_days += 1

    remaining_days = len(days) - shown_days
    if remaining_days > 0:
        lines.append(f"\n…и ещё {remaining_days} {_days_word(remaining_days)}")
    return lines


def _share_card_keyboard(url: str, label: str, token: str) -> InlineKeyboardMarkup:
    """Владельческая визитка: сверху — ссылка на пересылку, снизу — отзыв.
    Кнопка отзыва не url, а callback — она остаётся с владельцем и не
    путешествует вместе с пересланным сообщением (в отличие от url-кнопки)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, url=url)],
            [InlineKeyboardButton(text="🚫 Отозвать ссылку", callback_data=f"share:revoke:{token}")],
        ]
    )


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
        "v": PAYLOAD_VERSION,
        "name": routine["name"][:MAX_PROGRAM_NAME_LEN],
        "exercises": [
            {"name": ex["display_name"][:MAX_NAME_LEN], "target": ex["target"]}
            for ex in exercises[:MAX_SHARED_EXERCISES]
        ],
    }
    token = await db.create_shared_item(callback.from_user.id, "routine", json.dumps(payload, ensure_ascii=False))
    url = _deep_link(await get_bot_username(callback.bot), token)

    text = "\n".join(
        _routine_preview_lines(payload)
        + ["", "<i>Перешли это сообщение — по кнопке программу можно забрать себе.</i>"]
    )
    await callback.message.answer(
        text, parse_mode="HTML", reply_markup=_share_card_keyboard(url, "➕ Забрать программу себе", token)
    )
    await callback.answer("Визитка готова — пересылай 📤")


async def _send_program_card(callback: CallbackQuery, program_id: int, program_name: str) -> None:
    """Собрать снапшот программы и отдать владельцу визитку — общее тело обеих
    ручек ниже (адресованной id программы и старой, адресованной днём).

    Дни добираются до MAX_SHARED_DAYS *непустых*: пустой день получателю ничего
    не сообщает, но и место в лимите занимать не должен — иначе восьмидневная
    программа с одним пустым днём уезжала пятью днями вместо шести.
    """
    days = await db.list_program_days_by_id(program_id)
    day_payloads = []
    non_empty_days = 0
    for day in days:
        exercises = await db.list_routine_exercises(day["id"])
        if not exercises:
            continue
        non_empty_days += 1
        if len(day_payloads) >= MAX_SHARED_DAYS:
            continue
        day_payloads.append(
            {
                "name": day["name"][:MAX_PROGRAM_NAME_LEN],
                "exercises": [
                    {"name": ex["display_name"][:MAX_NAME_LEN], "target": ex["target"]}
                    for ex in exercises[:MAX_SHARED_EXERCISES]
                ],
            }
        )
    if not day_payloads:
        await callback.answer("В программе нет упражнений — нечем делиться", show_alert=True)
        return

    # Описание уезжает вместе с составом: получателю оно нужнее, чем автору —
    # он-то видит чужую программу впервые. Старые снапшоты (v3) его не несут,
    # поэтому на импорте читается через .get.
    program = await db.get_program(program_id)
    payload = {
        "v": PAYLOAD_VERSION,
        "name": program_name[:MAX_PROGRAM_NAME_LEN],
        "description": program["description"] if program else None,
        "days": day_payloads,
        # Сколько дней было в программе — единственное, из чего обе стороны
        # потом узнают, что уехало не всё (см. _omitted_days_note).
        "total_days": len(days),
        # Отдельно от общего числа: без этого причину недобора приходилось
        # угадывать по счётчику, и она угадывалась неверно (см. PAYLOAD_VERSION).
        "empty_days": len(days) - non_empty_days,
    }
    token = await db.create_shared_item(callback.from_user.id, "program", json.dumps(payload, ensure_ascii=False))
    url = _deep_link(await get_bot_username(callback.bot), token)

    note = _omitted_days_note(payload)
    text = "\n".join(
        _program_preview_lines(payload)
        + ([f"\n{note}"] if note else [])
        + ["", "<i>Перешли это сообщение — по кнопке программу можно забрать себе.</i>"]
    )
    await callback.message.answer(
        text, parse_mode="HTML", reply_markup=_share_card_keyboard(url, "➕ Забрать программу себе", token)
    )
    if note is None:
        await callback.answer("Визитка готова — пересылай 📤")
        return
    # Тост — чтобы человек заметил потерю сразу, а не отправив визитку другу;
    # то же самое написано в самой визитке, тост её только не даёт проскочить.
    in_card, total = _program_days_totals(payload)
    await callback.answer(
        f"Визитка готова, но уехало {in_card} {_days_word(in_card)} из {total}", show_alert=True
    )


@router.callback_query(F.data.startswith("share:prg:"))
async def share_program(callback: CallbackQuery, state: FSMContext):
    """«📤 Поделиться» на экране «⚙️ Изменить программу»: одна визитка на всю
    многодневку, а не по дню за раз (см. share_routine).

    В callback'е — id программы (`programs.id`). Ручка раньше читала это число
    как id дня-якоря, хотя кнопка отдаёт id программы с тех пор, как у программы
    появился свой id: у человека с двумя программами «Программа Б» уезжала
    визиткой «Программы А», потому что день с таким id принадлежал ей. У кого
    программа одна, id совпадали случайно — поэтому баг и не замечали.
    """
    program = await db.get_program(int(callback.data.split(":")[2]))
    if program is None or program["user_id"] != callback.from_user.id:
        await callback.answer("Программа не найдена", show_alert=True)
        return
    await _send_program_card(callback, program["id"], program["name"])


@router.callback_query(F.data.startswith("share:pgm:"))
async def share_program_legacy(callback: CallbackQuery, state: FSMContext):
    """Старая кнопка «Поделиться программой» — в ней id дня-якоря.

    Программа не имела собственного id, и кнопки адресовались одним из её дней;
    такие сообщения остались в чатах. Резолвим якорь в его программу, а не
    роняем — ровно как rt:pgm: в handlers.routines. Переиспользовать этот же
    префикс под id программы нельзя: старая кнопка тогда поделилась бы чужой
    программой, то есть тем же багом, только наоборот.
    """
    anchor = await db.get_routine(int(callback.data.split(":")[2]))
    if anchor is None or anchor["user_id"] != callback.from_user.id or anchor["program_id"] is None:
        await callback.answer("Программа не найдена", show_alert=True)
        return
    await _send_program_card(callback, anchor["program_id"], anchor["program_name"])


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
        "v": PAYLOAD_VERSION,
        "name": ex["display_name"][:MAX_NAME_LEN],
        "group": group["name"] if group else None,
        "description": description,
        # file_id живёт в рамках одного бота — у получателя фото откроется.
        "photo_file_id": ex["custom_photo_file_id"],
    }
    token = await db.create_shared_item(callback.from_user.id, "exercise", json.dumps(payload, ensure_ascii=False))
    url = _deep_link(await get_bot_username(callback.bot), token)

    lines = [f"🏋️ <b>{escape(payload['name'])}</b>"]
    if payload["group"]:
        lines.append(f"Группа: {escape(payload['group'])}")
    if description:
        lines.append("")
        lines.append(escape(description))
    lines += ["", "<i>Перешли это сообщение — по кнопке упражнение можно добавить себе.</i>"]
    await callback.message.answer(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=_share_card_keyboard(url, "➕ Добавить упражнение себе", token),
    )
    await callback.answer("Визитка готова — пересылай 📤")


@router.callback_query(F.data.startswith("share:revoke:"))
async def share_revoke(callback: CallbackQuery, state: FSMContext):
    """«🚫 Отозвать ссылку» на владельческой визитке: ссылка становится
    нерабочей для всех уже разосланных копий сразу — get_shared_item(token)
    начинает возвращать None, а open_shared/share_add это уже умеют трактовать
    как «ссылка устарела», так что отдельно оповещать получателей не нужно."""
    token = callback.data.split(":", 2)[2]
    row = await db.get_shared_item(token)
    if row is None:
        await callback.answer("Ссылка уже недействительна", show_alert=True)
        return
    if not await db.delete_shared_item(token, callback.from_user.id):
        await callback.answer("Это не твоя визитка", show_alert=True)
        return
    taken = row["taken_count"]
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer(
        "Ссылка отозвана 🚫" if not taken
        else f"Ссылка отозвана 🚫 До этого её забрали: {taken}",
        show_alert=True,
    )


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
    from handlers.persistent_menu import attach_silently

    # Визитка — второй вход для новичка: сюда попадают по ссылке, не нажав
    # «Start». Клавиатура нужна так же, а «обновил меню» — так же не нужно.
    is_new = await db.get_user(message.from_user.id) is None
    await db.get_or_create_user(message.from_user.id, message.from_user.username)
    if is_new:
        await attach_silently(message, message.from_user.id)
    # /start по чужой ссылке сбрасывает поток, но переписка с AI-тренером и
    # черновик его программы переживают такие переходы — сохраняем их.
    await clear_state_keep_ai(state)
    token = (command.args or "")[len(START_PREFIX):]
    row = await db.get_shared_item(token)
    if is_new:
        # Визитка приводит людей не хуже рекламы, и в воронке (см. acquisition.py)
        # стоит своей строкой — с автором, если ссылка ещё живая. По битой
        # ссылке человек тоже пришёл, и терять его из отчёта незачем.
        await db.set_user_source(
            message.from_user.id,
            acquisition.SOURCE_SHARED_CARD,
            row["owner_id"] if row is not None else None,
        )
    if row is None:
        await message.answer("🤷 Эта ссылка устарела или битая. Открой меню: /start")
        return
    payload = json.loads(row["payload"])
    owner = await db.get_user(row["owner_id"])
    # "v" отсутствует у визиток, созданных до PAYLOAD_VERSION — трактуем это
    # как версию 0. Сейчас формат для v0/v1 совпадает, так что читать общий
    # payload можно без разбора версий; поле только фиксирует точку отсчёта на
    # будущее (см. PAYLOAD_VERSION).
    payload.setdefault("v", 0)
    from_whom = f"от @{owner['username']}" if owner and owner["username"] else "от кого-то"

    if row["kind"] == "program":
        # «N дней» — про то, что реально лежит в визитке, и «из M», если у
        # отправителя было больше: получатель должен видеть, что забирает часть,
        # до того как решит забрать (раньше про урезку не говорили вообще).
        n, total = _program_days_totals(payload)
        out_of = "" if n >= total else f" из {total}"
        head = f"Тебе прислали программу {from_whom} — {n} {_days_word(n)}{out_of}.\n\n"
        note = _omitted_days_note(payload)
        text = head + "\n".join(_program_preview_lines(payload) + ([f"\n{note}"] if note else []))
    elif row["kind"] == "routine":
        n = len(payload["exercises"])
        head = f"Тебе прислали программу {from_whom} — {n} упр.\n\n"
        text = head + "\n".join(_routine_preview_lines(payload))
    else:
        text = f"Тебе прислали упражнение {from_whom}:\n\n🏋️ <b>{escape(payload['name'])}</b>"
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


async def _dedupe_program_name(user_id: int, name: str, owner_username: Optional[str]) -> str:
    """Не дать импортированной программе слиться с одноимённой, которая уже
    есть у получателя — включая повторный импорт той же визитки.

    Программа с уже занятым именем не создаётся, а сливается с тем, что там
    было — раньше молча (имя было единственным, что программу опознавало), а
    теперь её просто не даст создать уникальный индекс. Оба исхода одинаково
    неуместны для импорта, поэтому входящую копию переименовываем.

    Одиночные программы проверяем отдельно: индекс их не покрывает (они не
    строки в `programs`), а два одинаковых имени в одном списке путают ровно
    так же.
    """
    unique = await db.unique_program_name(
        user_id, name, suffix=f"от @{owner_username}" if owner_username else None
    )
    standalone = {r["name"].strip().lower() for r in await db.list_standalone_routines(user_id)}
    n = 2
    while unique.strip().lower() in standalone:
        unique = f"{name} ({n})"
        n += 1
    return unique


async def _resolve_exercise(user_id: int, name: str) -> int:
    """Своё по имени → форк шаблона каталога → создать под «Другое».

    В отличие от create_routine_from_program, нерезолвящееся имя не
    выбрасывается: в чужой программе кастомные названия — норма, и потерять
    половину упражнений при импорте хуже, чем создать их под «Другое».

    Имя режем и здесь, а не только при сборке визитки: визитки со старым лимитом
    (80 символов на всё) уже разосланы по чатам, и по ним всё ещё приходят имена
    длиннее, чем разрешает ручной ввод."""
    name = name.strip()[: config.MAX_EXERCISE_NAME_LENGTH].rstrip()
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
    if row["owner_id"] == user_id:
        # open_shared прячет кнопку «Добавить» для владельца, но эта визитка
        # живёт в пересланном сообщении — кнопка может вернуться к владельцу
        # чужими руками (переслали ему обратно). Проверка нужна здесь же, а не
        # только там: callback приходит напрямую по callback_data, экран
        # open_shared в этот момент никто не открывал.
        await callback.answer("Это твоя собственная визитка — добавлять не нужно", show_alert=True)
        return
    payload = json.loads(row["payload"])

    # Импорт — одна из четырёх дверей, через которые появляются программы, и
    # единственная, где количество дней задаёт кто-то другой. Лимит проверяем
    # тем же общим бюджетом, что и остальные три (см. db.routine_budget):
    # раньше присланной программой на 40 дней его можно было просто перешагнуть.
    incoming = len(payload["days"]) if row["kind"] == "program" else 1
    over_budget = await db.routine_budget(user_id, incoming)
    if over_budget:
        await callback.answer(over_budget, show_alert=True)
        return

    if row["kind"] == "program":
        owner = await db.get_user(row["owner_id"])
        owner_name = owner["username"] if owner else None
        program_name = await _dedupe_program_name(user_id, payload["name"], owner_name)
        program_id = await db.create_program(
            user_id, program_name, source="import",
            source_ref=f"@{owner_name}" if owner_name else None,
            description=payload.get("description"),
        )
        for day in payload["days"]:
            routine_id = await db.create_routine(user_id, day["name"], program_id=program_id)
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
            f"✅ Программа «{escape(program_name)}» у тебя — {len(payload['days'])} "
            f"{_days_word(len(payload['days']))} в 🗂 Программы.\n"
            "Новые упражнения легли в «Другое», группу можно поменять в ⚙️ Упражнения.",
            parse_mode="HTML",
        )
        await db.mark_shared_item_taken(token)
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
        await db.mark_shared_item_taken(token)
        await callback.answer("Добавил 👌")
        return

    # kind == "exercise"
    # Под лимиты ручного ввода: визитки со старым общим лимитом (80 на имя,
    # 1500 на описание) уже разосланы, и по ним приезжает то, что сам человек
    # у себя завести бы не смог.
    name = payload["name"].strip()[: config.MAX_EXERCISE_NAME_LENGTH].rstrip()
    existing = await db.find_exercise_by_name(user_id, name)
    if existing:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer(f"«{name}» у тебя уже есть", show_alert=True)
        return
    group_id = await _resolve_group_id(user_id, payload.get("group"))
    ex_id = await db.create_exercise(user_id, name, group_id)
    if payload.get("description"):
        await db.set_exercise_description(
            ex_id, payload["description"][: config.MAX_EXERCISE_DESCRIPTION_LENGTH]
        )
    if payload.get("photo_file_id"):
        await db.set_exercise_photo(ex_id, payload["photo_file_id"])
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        f"✅ «{escape(name)}» добавлено — ⚙️ Упражнения.", parse_mode="HTML"
    )
    await db.mark_shared_item_taken(token)
    await callback.answer("Добавил 👌")
