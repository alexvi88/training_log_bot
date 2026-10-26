"""События сервиса для тренеров доезжают до человека: банер на iPhone и сообщение в Telegram.

Три события, у каждого — пара «APNs + Telegram» и тексты на языке получателя:

- комментарий тренера к тренировке → атлету (`deliver_comment`);
- тренер назначил программу → атлету (`deliver_program`), банер ведёт в
  программу, кнопка в боте — «Открыть программу» (`rt:prg:<id>`);
- атлет принял приглашение → тренеру (`deliver_new_client`), банер ведёт в
  карточку этого клиента поверх «Мои клиенты», кнопка в боте — в неё же
  (`coach:cl:<id>`);
- связку разорвали (`deliver_unlinked`) → второй стороне: атлет закрыл
  доступ — тренеру «Клиент закрыл доступ» (в «Мои клиенты»), тренер убрал
  клиента — атлету «Тренер больше не ведёт тебя» (в его программу от тренера,
  если она есть).
- клиент закончил тренировку (`notify_client_workout_finished` →
  `deliver_client_workout`) → каждому его тренеру: «Клиент закончил
  тренировку», факты только из этой тренировки; банер ведёт в эту тренировку
  поверх карточки клиента, кнопка в боте — в неё же (`coach:w:<id>:<id>`).
  Один раз на тренировку, не ночью банером, по тумблеру тренера — подробно у
  `deliver_client_workout`.

Ниже — подробно про комментарий; остальные устроены так же.

Сервис для тренеров (coach_service.py) пишет комментарий из двух мест — REST
`/v1/coach/.../comments` (приложение) и экран тренировки клиента в боте
(handlers/coach.py). Оба зовут `deliver_comment` после записи, и атлет получает
его одинаково, откуда бы тренер ни писал:

- APNs — на каждое iOS-устройство атлета (банер «Комментарий тренера»), как
  было у REST с первого дня;
- Telegram — сообщение с текстом и кнопкой «Открыть тренировку», если у атлета
  есть Telegram (`telegram_linked`, id больше нуля: у аккаунта только из
  приложения id синтетический, писать туда некуда).

Два канала — как у остальных пушей продукта: банер на телефоне и сообщение в
боте — это разные места, и человек, который сидит только в одном из них, не
должен терять комментарий. Тексты — на языке атлета (`users.lang`), а не того,
кто пишет: фоновой отправке язык из апдейта не достаётся (CLAUDE.md, «Фоновая
задача, отправляющая текст без апдейта»).

Ни один шаг не бросает: комментарий к этому моменту уже записан, и сбой
доставки не должен превращаться в ошибку у тренера.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field
from html import escape
from typing import Any, Awaitable, Callable, Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import chat_bottom
import config
import db
import formatting
import i18n
import inbox
import push_ios
import timeutil

logger = logging.getLogger(__name__)

# Сколько символов комментария влезает в тело банера после имени тренера.
PUSH_TEXT_CLIP = 60
# Имя программы в банере — длинное название не должно съесть «Глянь и начинай».
PUSH_PROGRAM_CLIP = 40


def _lang_of(client: Any) -> str:
    lang = client["lang"]
    return lang if lang in i18n.SUPPORTED else i18n.DEFAULT_LANG


def has_telegram(client: Any) -> bool:
    """Есть ли куда писать в Telegram: аккаунт из приложения без Telegram
    живёт под синтетическим отрицательным id (db.create_app_only_user)."""
    return bool(client["telegram_linked"]) and client["telegram_id"] > 0


def _name_or_unnamed(name: Optional[str], lang: str) -> str:
    return name or i18n.t_in(lang, "coach.unnamed")


# ---------- общий каркас: банер, Telegram, короткоживущий бот ----------


async def _push_ios(
    recipient: Any, title_key: str, body_key: str, *, category: str, route: Optional[dict], **params: str
) -> int:
    """Банер получателю на его языке, на каждое iOS-устройство, и строка в
    его «Уведомлениях» (inbox.deliver) — даже когда банер не дошёл. Возвращает,
    сколько устройств приняли банер. Не бросает."""
    user_id = recipient["telegram_id"]
    try:
        lang = _lang_of(recipient)
        title = i18n.t_in(lang, title_key)
        body = i18n.t_in(lang, body_key, **params)
        delivery = await inbox.deliver(user_id, category, title, body, lang=lang, route=route)
        return delivery.accepted
    except Exception:
        logger.exception("coach: %s push failed for user %s", category, user_id)
        return 0


async def _send_telegram(
    bot, recipient: Any, build: Callable[[], Awaitable[tuple[str, Optional[InlineKeyboardMarkup]]]], what: str
) -> bool:
    """Сообщение получателю в Telegram. False — писать некуда или не дошло
    (остановил бота, сеть). Не бросает."""
    if bot is None or not has_telegram(recipient):
        return False
    chat_id = recipient["telegram_id"]
    try:
        body, kb = await build()
        sent = await bot.send_message(chat_id, body, parse_mode="HTML", reply_markup=kb)
    except Exception:
        logger.exception("coach: telegram %s failed for user %s", what, chat_id)
        return False
    # Сообщение легло под экран получателя: следующий тап по старому экрану
    # должен перерисовать его ниже, а не править на месте над сообщением. Бот
    # из main() отмечает это сам (chat_bottom.TrackOutgoing), короткоживущий
    # бот REST-слоя — нет; повторная отметка ничего не ломает.
    message_id = getattr(sent, "message_id", None)
    if isinstance(message_id, int):
        chat_bottom.note_message(chat_id, message_id)
    return True


async def _with_own_bot(recipient: Any, send: Callable[[Any], Awaitable[bool]]) -> bool:
    """REST-слою своего Bot не досталось (маршруты /v1 собираются раньше, чем
    main() создаёт бота) — короткоживущий на одну отправку, как у
    api_v1_feedback._send_feedback_to_admin."""
    if not config.BOT_TOKEN or not has_telegram(recipient):
        return False
    from aiogram import Bot

    bot = Bot(token=config.BOT_TOKEN)
    try:
        return await send(bot)
    finally:
        await bot.session.close()


async def _telegram_any_bot(
    bot, recipient: Any, send: Callable[[Any], Awaitable[bool]], what: str
) -> bool:
    """Живой бот хендлера — сразу; без него (REST) — короткоживущий. Не бросает."""
    try:
        if bot is not None:
            return await send(bot)
        return await _with_own_bot(recipient, send)
    except Exception:
        logger.exception("coach: telegram %s (rest) failed for user %s", what, recipient["telegram_id"])
        return False


# ---------- комментарий тренера → атлету ----------


async def push_ios_comment(client: Any, coach_name: str, text: str, workout_id: int) -> int:
    """Банер атлету «Комментарий тренера» на его языке, на каждое устройство.
    Возвращает, сколько устройств приняли банер."""
    return await _push_ios(
        client, "coach.push.comment_title", "coach.push.comment_body",
        category=push_ios.COACH_COMMENT, route=push_ios.workout_detail_route(workout_id),
        coach=push_ios._clip_param(coach_name, 20),
        text=push_ios._clip_param(text, PUSH_TEXT_CLIP),
    )


async def telegram_comment_message(
    client: Any, coach_name: str, text: str, workout_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и кнопка сообщения атлету — на ЕГО языке, какой бы ни был у
    того, кто вызвал (тренер мог писать по-английски)."""
    with i18n.use_lang(_lang_of(client)):
        workout = await db.get_workout(workout_id)
        when = ""
        if workout is not None:
            started = dt.datetime.fromisoformat(workout["started_at"])
            local = timeutil.to_user_local(started, client)
            when = formatting.format_day_month_ru(local.date(), timeutil.user_today(client))
        body = i18n.t(
            "coach.tg.comment",
            coach=escape(coach_name or i18n.t("coach.unnamed")),
            date=escape(when),
            text=escape(text),
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=i18n.t("coach.btn.open_workout"), callback_data=f"hist:item:{workout_id}")
        ]])
    return body, kb


async def send_telegram_comment(bot, client: Any, coach_name: str, text: str, workout_id: int) -> bool:
    """Сообщение атлету в Telegram. False — писать некуда или не дошло
    (атлет остановил бота, сеть) — комментарий всё равно лежит под тренировкой."""
    return await _send_telegram(
        bot, client, lambda: telegram_comment_message(client, coach_name, text, workout_id), "comment"
    )


async def _send_with_own_bot(client: Any, coach_name: str, text: str, workout_id: int) -> bool:
    """Комментарий из REST — короткоживущим ботом (см. `_with_own_bot`)."""
    return await _with_own_bot(
        client, lambda bot: send_telegram_comment(bot, client, coach_name, text, workout_id)
    )


async def deliver_comment(
    client: Any, coach_name: str, text: str, workout_id: int, *, bot: Optional[Any] = None
) -> tuple[int, bool]:
    """Оба канала разом: (сколько iOS-устройств приняли банер, ушло ли
    сообщение в Telegram). `bot` — живой бот хендлера; без него (REST) —
    короткоживущий."""
    pushed = await push_ios_comment(client, coach_name, text, workout_id)
    if bot is not None:
        telegram = await send_telegram_comment(bot, client, coach_name, text, workout_id)
    else:
        try:
            telegram = await _send_with_own_bot(client, coach_name, text, workout_id)
        except Exception:
            logger.exception("coach: telegram comment (rest) failed for user %s", client["telegram_id"])
            telegram = False
    return pushed, telegram


# ---------- тренер назначил программу → атлету ----------


async def push_ios_program(client: Any, coach_name: Optional[str], program_name: str, program_id: int) -> int:
    """Банер атлету «Программа от тренера»; тап — в эту программу."""
    lang = _lang_of(client)
    return await _push_ios(
        client, "coach.push.program_title", "coach.push.program_body",
        category=push_ios.COACH_PROGRAM, route=push_ios.program_route(program_id),
        coach=push_ios._clip_param(_name_or_unnamed(coach_name, lang), 20),
        name=push_ios._clip_param(program_name, PUSH_PROGRAM_CLIP),
    )


async def telegram_program_message(
    client: Any, coach_name: Optional[str], program_name: str, program_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и кнопка «Открыть программу» — на языке атлета."""
    with i18n.use_lang(_lang_of(client)):
        body = i18n.t(
            "coach.tg.program",
            coach=escape(coach_name or i18n.t("coach.unnamed")),
            name=escape(program_name),
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=i18n.t("btn.open_program"), callback_data=f"rt:prg:{program_id}")
        ]])
    return body, kb


async def send_telegram_program(
    bot, client: Any, coach_name: Optional[str], program_name: str, program_id: int
) -> bool:
    return await _send_telegram(
        bot, client, lambda: telegram_program_message(client, coach_name, program_name, program_id), "program"
    )


async def deliver_program(
    client: Any, coach_name: Optional[str], program_name: str, program_id: int, *, bot: Optional[Any] = None
) -> tuple[int, bool]:
    """Программа уже лежит у атлета (coach_service.assign_program) — сообщить
    обоими каналами: (сколько устройств приняли банер, ушло ли в Telegram).
    Не бросает: сбой доставки не должен стать ошибкой у тренера."""
    pushed = await push_ios_program(client, coach_name, program_name, program_id)
    telegram = await _telegram_any_bot(
        bot, client,
        lambda b: send_telegram_program(b, client, coach_name, program_name, program_id),
        "program",
    )
    return pushed, telegram


# ---------- атлет принял приглашение → тренеру ----------


async def push_ios_new_client(coach: Any, client_name: str, client_id: int) -> int:
    """Банер тренеру «Новый клиент»; тап — в карточку этого клиента поверх
    «Мои клиенты» (старые сборки не знают `client_id` и открывают список)."""
    return await _push_ios(
        coach, "coach.push.new_client_title", "coach.push.new_client_body",
        category=push_ios.COACH_NEW_CLIENT, route=push_ios.coach_clients_route(client_id),
        name=push_ios._clip_param(client_name, 24),
    )


async def telegram_new_client_message(
    coach: Any, client_name: str, client_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    """Текст на языке тренера и кнопка с именем клиента — сразу в его карточку
    (`coach:cl:<id>`, оттуда «Назад» ведёт в «Мои клиенты»)."""
    with i18n.use_lang(_lang_of(coach)):
        body = i18n.t("coach.tg.new_client", name=escape(client_name))
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=client_name, callback_data=f"coach:cl:{int(client_id)}")
        ]])
    return body, kb


async def send_telegram_new_client(bot, coach: Any, client_name: str, client_id: int) -> bool:
    return await _send_telegram(
        bot, coach, lambda: telegram_new_client_message(coach, client_name, client_id), "new client"
    )


async def deliver_new_client(
    coach_id: int, client_name: str, client_id: int, *, bot: Optional[Any] = None
) -> tuple[int, bool]:
    """Атлет только что нажал «Разрешить» — тренеру обоими каналами: (сколько
    устройств приняли банер, ушло ли в Telegram). Строку тренера читаем
    здесь: нужен его язык и его каналы. Не бросает: связка уже заведена."""
    try:
        coach = await db.get_user(coach_id)
    except Exception:
        logger.exception("coach: new client notify — can't read coach %s", coach_id)
        return 0, False
    if coach is None:
        return 0, False
    pushed = await push_ios_new_client(coach, client_name, client_id)
    telegram = await _telegram_any_bot(
        bot, coach, lambda b: send_telegram_new_client(b, coach, client_name, client_id), "new client"
    )
    return pushed, telegram


# ---------- связку разорвали → второй стороне ----------
#
# Подключение сопровождается пушем «Новый клиент», а разрыв раньше проходил
# молча: тренер просто не находил клиента в списке, атлет узнавал, только
# открыв «Тренер». Теперь вторая сторона слышит об этом тем же набором
# каналов — банер, строка в «Уведомлениях», Telegram — и на своём языке.


async def push_ios_client_left(coach: Any, client_name: str) -> int:
    """Банер тренеру «Клиент закрыл доступ»; тап — в «Мои клиенты», где
    неделю стоит серая строка."""
    return await _push_ios(
        coach, "coach.push.client_left_title", "coach.push.client_left_body",
        category=push_ios.COACH_CLIENT_LEFT, route=push_ios.coach_clients_route(),
        name=push_ios._clip_param(client_name, 24),
    )


async def telegram_client_left_message(coach: Any, client_name: str) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и кнопка «Мои клиенты» — на языке тренера."""
    with i18n.use_lang(_lang_of(coach)):
        body = i18n.t("coach.tg.client_left", name=escape(client_name))
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=i18n.t("coach.btn.my_clients_short"), callback_data="coach:clients:0")
        ]])
    return body, kb


async def push_ios_coach_left(client: Any, coach_name: Optional[str], program_id: Optional[int]) -> int:
    """Банер атлету «Тренер больше не ведёт тебя». Про программы — только если
    программа от этого тренера у атлета правда лежит (`program_id`); тогда и
    тап ведёт в неё."""
    lang = _lang_of(client)
    body_key = "coach.push.coach_left_body_programs" if program_id is not None else "coach.push.coach_left_body"
    return await _push_ios(
        client, "coach.push.coach_left_title", body_key,
        category=push_ios.COACH_UNLINKED,
        route=push_ios.program_route(program_id) if program_id is not None else None,
        coach=push_ios._clip_param(_name_or_unnamed(coach_name, lang), 20),
    )


async def telegram_coach_left_message(
    client: Any, coach_name: Optional[str], program_id: Optional[int]
) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    """Текст на языке атлета; кнопка «Программы» — только когда программа от
    тренера у него есть."""
    with i18n.use_lang(_lang_of(client)):
        key = "coach.tg.coach_left_programs" if program_id is not None else "coach.tg.coach_left"
        body = i18n.t(key, coach=escape(coach_name or i18n.t("coach.unnamed")))
        kb = None
        if program_id is not None:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=i18n.t("btn.programs"), callback_data="rt:manage")
            ]])
    return body, kb


async def _latest_coach_program(coach_id: int, client_id: int) -> Optional[int]:
    """Свежая программа от этого тренера, которая у атлета всё ещё есть:
    разрыв её не трогает (db.delete_coach_link), удалил сам — её нет."""
    try:
        rows = await db.list_coach_assignments(coach_id, client_id)
    except Exception:
        logger.exception("coach: unlink notify — can't read programs %s → %s", coach_id, client_id)
        return None
    return rows[0]["program_id"] if rows else None


async def deliver_unlinked(
    coach_id: int,
    client_id: int,
    *,
    by: str,
    client_name: str,
    coach_name: Optional[str] = None,
    bot: Optional[Any] = None,
) -> tuple[int, bool]:
    """Связку уже разорвали (db.delete_coach_link) — сказать второй стороне:
    `by` = db.COACH_UNLINK_BY_CLIENT — тренеру, COACH_UNLINK_BY_COACH — атлету.
    (сколько устройств приняли банер, ушло ли в Telegram). Не бросает."""
    recipient_id = coach_id if by == db.COACH_UNLINK_BY_CLIENT else client_id
    try:
        recipient = await db.get_user(recipient_id)
    except Exception:
        logger.exception("coach: unlink notify — can't read user %s", recipient_id)
        return 0, False
    if recipient is None:
        return 0, False
    if by == db.COACH_UNLINK_BY_CLIENT:
        pushed = await push_ios_client_left(recipient, client_name)
        telegram = await _telegram_any_bot(
            bot, recipient,
            lambda b: _send_telegram(
                b, recipient, lambda: telegram_client_left_message(recipient, client_name), "client left"
            ),
            "client left",
        )
        return pushed, telegram
    program_id = await _latest_coach_program(coach_id, client_id)
    pushed = await push_ios_coach_left(recipient, coach_name, program_id)
    telegram = await _telegram_any_bot(
        bot, recipient,
        lambda b: _send_telegram(
            b, recipient, lambda: telegram_coach_left_message(recipient, coach_name, program_id), "coach left"
        ),
        "coach left",
    )
    return pushed, telegram


# ---------- клиент закончил тренировку → тренеру ----------
#
# Владелец: «а мне не должен приходить пуш, если клиент закончил треню? жена
# потренила — ничего не пришло». Тренер узнавал о тренировке клиента, только
# открыв «Мои клиенты».
#
# Правила:
#
# - Откуда. Все пути завершения (бот: «Завершить», «Завершить задним числом»
#   на зависшей, «Завершить и начать» у программ; `/v1/.../finish` приложения)
#   зовут `notify_client_workout_finished` сразу после удачного
#   `db.finish_workout`. Он делает одну дешёвую проверку (есть ли у атлета
#   тренер) и уводит доставку в фоновую задачу: финиш не ждёт ни Telegram, ни
#   Apple и не падает от них.
# - Сколько. Ровно одно сообщение на тренировку: метка `workouts.
#   coach_notified_at` ставится одним UPDATE (`db.claim_coach_workout_notice`)
#   до отправки — повтор финиша из офлайн-очереди, двойной тап и повторное
#   завершение той же тренировки второго не дают. Тренировка меньше чем из
#   CLIENT_WORKOUT_MIN_SETS подходов — не повод писать тренеру (открыл и
#   закрыл), метка не ставится.
# - Что. Только факты из этой тренировки: сколько упражнений, тоннаж (если
#   больше нуля), рекорды (один — с названием упражнения, несколько — числом).
#   Названия упражнений — данные атлета, на ЕГО языке
#   (coach_service.client_exercise_name); подписи — на языке тренера. Имя —
#   то, под которым атлет назвался этому тренеру (coach_links.client_name).
# - Когда. Тихие часы — по поясу тренера, те же 22:00–09:00, что у ежедневных
#   пушей (engagement.is_quiet_hour). Ночью банера нет: строка ложится в
#   «Уведомления» без баннера, а сообщение в Telegram уходит как всегда —
#   беззвучно (весь бот стоит на disable_notification, здесь это сказано
#   явно), то есть никого не будит и ждёт утра в чате. Откладывать на утро
#   нечего: оба тихих канала и так дождутся тренера, а отдельная очередь
#   «доставить в 9:00» ради одного банера — лишняя движущаяся часть. Пояс 0
#   (не выбран, см. engagement.tz_is_known) читаем как config.DEFAULT_TZ_OFFSET.
# - Чей тумблер. «Тренировки клиентов» тренера
#   (users.client_workout_pushes_enabled, по умолчанию включён): выключен —
#   ни банера, ни Telegram, ни строки в «Уведомлениях».
# - Аналитика. Банер считает apns.send_alert (`push_sent` с категорией),
#   Telegram — db.record_push с tg_message_id (тап по кнопке — push_open).
#   Категория в has_push_today не засчитывается (engagement.
#   DAILY_SLOT_EXCLUDED_CATEGORIES): тренер с десятком клиентов иначе
#   лишился бы своего собственного ежедневного пуша.

# Меньше двух подходов — не тренировка, о которой стоит писать тренеру.
CLIENT_WORKOUT_MIN_SETS = 2
# Название упражнения с рекордом в банере.
PUSH_EXERCISE_CLIP = 40
# Имя клиента в банере — как у «новый клиент».
PUSH_CLIENT_NAME_CLIP = 24

# Фоновые доставки: цикл событий держит задачи слабыми ссылками, без своей
# сильной задача могла бы пропасть посреди отправки (как
# handlers.workout._background_tasks).
_background_tasks: set[asyncio.Task] = set()


@dataclass
class ClientWorkoutFacts:
    """Что тренеру сказать про тренировку — только из неё самой."""

    exercises: int
    sets: int
    tonnage: float
    unit: str
    # Упражнения с рекордом этой тренировки — уже на языке атлета.
    records: list[str] = field(default_factory=list)


async def client_workout_facts(client: Any, workout_id: int) -> ClientWorkoutFacts:
    """Упражнения, подходы, тоннаж по нагрузке (как итоги финиша,
    BlockView.load_tonnage) и рекорды e1RM или повторов, поставленные именно
    в этой тренировке (view_builder mark_records — та же 🔥, что в карточке)."""
    import coach_service
    import view_builder

    blocks = await view_builder.build_block_views(workout_id, client["e1rm_formula"], mark_records=True)
    records = [
        coach_service.client_exercise_name(client, block.exercise_name)
        for block in blocks
        if block.record_e1rm_delta is not None or block.record_reps is not None
    ]
    return ClientWorkoutFacts(
        exercises=len(blocks),
        sets=sum(len(block.sets) for block in blocks),
        tonnage=sum(block.load_tonnage for block in blocks),
        unit=client["unit"],
        records=records,
    )


def client_workout_summary(lang: str, facts: ClientWorkoutFacts, *, clip: bool = False) -> str:
    """«5 упражнений · 3.2 тонны · рекорд: Жим штанги лёжа» на языке `lang`
    (тренера). Части без данных выпадают: тоннажа нет — его не пишем, рекордов
    нет — тоже. `clip` — для банера: длинное название упражнения подрезается."""
    with i18n.use_lang(lang):
        parts = [i18n.t("coach.client_workout.exercises", n=facts.exercises)]
        if facts.tonnage > 0:
            parts.append(formatting.format_tonnage(facts.tonnage, facts.unit))
        if len(facts.records) == 1:
            name = facts.records[0]
            if clip:
                name = push_ios._clip_param(name, PUSH_EXERCISE_CLIP)
            parts.append(i18n.t("coach.client_workout.record_one", exercise=name))
        elif facts.records:
            parts.append(i18n.t("coach.client_workout.records", n=len(facts.records)))
    return " · ".join(parts)


def _utc_now() -> dt.datetime:
    """Naive UTC — отдельно, чтобы тесты могли поставить часы."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def coach_local_now(coach: Any) -> dt.datetime:
    """Часы тренера. Пояс 0 — «не выбран» (engagement.tz_is_known), а не UTC:
    тогда — пояс по умолчанию новых аккаунтов."""
    offset = coach["tz_offset"] or config.DEFAULT_TZ_OFFSET
    return _utc_now() + dt.timedelta(hours=offset)


def is_coach_quiet_time(coach: Any) -> bool:
    import engagement

    return engagement.is_quiet_hour(coach_local_now(coach).hour)


async def telegram_client_workout_message(
    coach: Any, client_name: str, client_id: int, workout_id: int, facts: ClientWorkoutFacts
) -> tuple[str, InlineKeyboardMarkup]:
    """Текст на языке тренера и кнопка «Открыть тренировку» — экран
    тренировки клиента в боте (`coach:w:<клиент>:<тренировка>`)."""
    lang = _lang_of(coach)
    with i18n.use_lang(lang):
        body = i18n.t(
            "coach.tg.client_workout",
            name=escape(client_name),
            summary=escape(client_workout_summary(lang, facts)),
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=i18n.t("coach.btn.open_workout"),
                callback_data=f"coach:w:{int(client_id)}:{int(workout_id)}",
            )
        ]])
    return body, kb


async def _send_telegram_client_workout(
    bot, coach: Any, body: str, kb: InlineKeyboardMarkup
) -> Optional[int]:
    """Сообщение тренеру — беззвучно, как весь бот. id ушедшего сообщения
    (для record_push) или None — писать некуда или не дошло. Не бросает."""
    if not has_telegram(coach):
        return None
    chat_id = coach["telegram_id"]

    async def send(b) -> Optional[int]:
        sent = await b.send_message(
            chat_id, body, parse_mode="HTML", reply_markup=kb, disable_notification=True
        )
        message_id = getattr(sent, "message_id", None)
        if isinstance(message_id, int):
            chat_bottom.note_message(chat_id, message_id)
            return message_id
        return None

    try:
        if bot is not None:
            return await send(bot)
        if not config.BOT_TOKEN:
            return None
        from aiogram import Bot

        own = Bot(token=config.BOT_TOKEN)
        try:
            return await send(own)
        finally:
            await own.session.close()
    except Exception:
        logger.exception("coach: telegram client workout failed for coach %s", chat_id)
        return None


async def _deliver_to_coach(
    coach: Any, client_name: str, client_id: int, workout_id: int, facts: ClientWorkoutFacts, bot
) -> tuple[int, bool]:
    """Один тренер: банер (днём) + строка в «Уведомлениях» + Telegram.
    (сколько устройств приняли банер, ушло ли в Telegram)."""
    coach_id = coach["telegram_id"]
    lang = _lang_of(coach)
    quiet = is_coach_quiet_time(coach)
    pushed = 0
    try:
        title = i18n.t_in(lang, "coach.push.client_workout_title")
        body = i18n.t_in(
            lang, "coach.push.client_workout_body",
            name=push_ios._clip_param(client_name, PUSH_CLIENT_NAME_CLIP),
            summary=client_workout_summary(lang, facts, clip=True),
        )
        delivery = await inbox.deliver(
            coach_id, push_ios.COACH_CLIENT_WORKOUT, title, body, lang=lang,
            route=push_ios.coach_client_workout_route(client_id, workout_id),
            push=not quiet,
        )
        pushed = delivery.accepted
    except Exception:
        logger.exception("coach: client workout push failed for coach %s", coach_id)
    message_id: Optional[int] = None
    try:
        text, kb = await telegram_client_workout_message(coach, client_name, client_id, workout_id, facts)
        message_id = await _send_telegram_client_workout(bot, coach, text, kb)
        if message_id is not None:
            await db.record_push(
                coach_id, push_ios.COACH_CLIENT_WORKOUT, text,
                coach_local_now(coach).date().isoformat(), tg_message_id=message_id,
            )
    except Exception:
        logger.exception("coach: client workout telegram failed for coach %s", coach_id)
    return pushed, message_id is not None


async def deliver_client_workout(client_id: int, workout_id: int, *, bot: Optional[Any] = None) -> int:
    """Тренировка атлета `client_id` уже закончена — сказать каждому его
    тренеру (правила — в комментарии над разделом). Возвращает, скольким
    тренерам ушло. Не бросает: финиш к этому моменту давно записан."""
    try:
        workout = await db.get_workout(workout_id)
        if workout is None or workout["user_id"] != client_id or workout["status"] != "finished":
            return 0
        if workout["coach_notified_at"] is not None:
            return 0
        links = await db.list_client_coaches(client_id)
        if not links:
            return 0
        client = await db.get_user(client_id)
        if client is None:
            return 0
        facts = await client_workout_facts(client, workout_id)
        if facts.sets < CLIENT_WORKOUT_MIN_SETS:
            return 0
        if not await db.claim_coach_workout_notice(workout_id):
            return 0
    except Exception:
        logger.exception("coach: client workout notify — can't prepare workout %s", workout_id)
        return 0
    notified = 0
    for link in links:
        try:
            coach = await db.get_user(link["coach_id"])
            if coach is None or not coach["client_workout_pushes_enabled"]:
                continue
            await _deliver_to_coach(coach, link["client_name"], client_id, workout_id, facts, bot)
            notified += 1
        except Exception:
            logger.exception("coach: client workout notify failed for coach %s", link["coach_id"])
    return notified


async def notify_client_workout_finished(client_id: int, workout_id: int, *, bot: Optional[Any] = None) -> None:
    """Звать сразу после удачного db.finish_workout на любом пути
    завершения. Атлет без тренера — одна выборка по индексу и всё; с тренером
    — доставка уходит в фон. Не бросает и не ждёт отправки."""
    try:
        if not await db.client_has_coach(client_id):
            return
        task = asyncio.create_task(deliver_client_workout(client_id, workout_id, bot=bot))
    except Exception:
        logger.exception("coach: can't schedule client workout notify for workout %s", workout_id)
        return
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def wait_background() -> None:
    """Дождаться фоновых доставок — для тестов."""
    while _background_tasks:
        await asyncio.gather(*list(_background_tasks), return_exceptions=True)

