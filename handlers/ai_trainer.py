"""AI-тренер: чат с Grok, у которого есть доступ к данным текущего пользователя."""

import asyncio
import base64
import logging
import random
from contextlib import suppress
from html import escape
from typing import Optional

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

import ai_trainer
import config
import db
import exercise_mentions
import formatting
import keyboards
import ui
from fsm import AITrainerFlow

router = Router(name="ai_trainer")

logger = logging.getLogger(__name__)

# Сколько последних реплик (вопрос+ответ = 2) держим в контексте диалога.
# Чётное число, чтобы история всегда начиналась с реплики пользователя.
HISTORY_LIMIT = 12

# Telegram обрезает сообщения на 4096 символах; режем с запасом.
TG_CHUNK = 4000

# С какого остатка начинаем показывать, сколько вопросов осталось на сегодня.
_QUOTA_WARN_AT = 3

# Лимит xAI на размер одного изображения (см. xai_sdk.chat.image).
MAX_IMAGE_BYTES = 10 * 1024 * 1024

# Вопрос по умолчанию, если пользователь прислал фото без подписи.
DEFAULT_PHOTO_QUESTION = "Посмотри на фото и прокомментируй."

# Черновик — превью, а не сообщение: показываем хвост генерации, чтобы длинный
# ответ не упирался в лимиты, а живая «печать» была видна.
MAX_DRAFT_CHARS = 1000

# Лимит на размер голосового (Telegram сам не режет сильнее, но перестрахуемся).
MAX_VOICE_BYTES = 20 * 1024 * 1024

# Длиннее — явно не короткий вопрос, дороже распознавать и дольше ждать ответ.
MAX_VOICE_SECONDS = 300

INTRO_TEXT = (
    "🤖 <b>ПРИВЕТ, АТЛЕТ. ТРЕНЕР НА СВЯЗИ.</b>\n\n"
    "У меня есть доступ к истории твоих тренировок и многолетний тренерский опыт. "
    "Спрашивай что угодно:\n"
    "• «Как прогресс в жиме лёжа? Почему не растёт присед?»\n"
    "• «Дай совет по программе тренировок»\n"
    "• «Сколько белка есть, чтобы расти?»\n\n"
    "Пиши вопрос 👇 (можно голосом — жми на 🎤)"
)

# Shown instead of the full intro when returning to a conversation that's already
# going — repeating the whole "привет, вот что я умею" would read as if the
# trainer had forgotten the last few messages.
RESUME_TEXT = "🤖 <b>ТРЕНЕР НА СВЯЗИ.</b> Продолжаем — пиши вопрос 👇"

# Пользователи, чей вопрос сейчас обрабатывается — защита от параллельных запросов.
_busy: set[int] = set()

# Крутятся в placeholder-сообщении, пока модель думает — вместо голого "печатает..."
# на несколько секунд/десятков секунд (особенно с tool-calls и веб-поиском под капотом).
RUNNING_REPLIES = [
    "💪 держи паузу, сейчас будет по делу...",
    "🧠 включаю тренерский мозг, момент...",
    "🔥 разминаюсь перед ответом...",
    "🎯 целюсь в точный совет, не спугни...",
    "🧘 собираю мысли, не гони...",
    "🏋️ гружу знания, как штангу — по чуть-чуть...",
    "📖 сверяюсь с методикой, секунду...",
    "⏱️ отдыхаю между подходами мысли, погоди...",
    "🥩 перевариваю вопрос, дай времени...",
    "🧊 остываю от подхода, сейчас отвечу...",
    "🩹 разбираю по косточкам, момент...",
    "🚿 после подхода думается чётче, секунду...",
    "🧢 не гони, тренер думает медленно, но метко...",
    "🥊 бью по вопросу точно, момент...",
    "🍗 заряжаюсь белком мысли, момент...",
    "🧱 закладываю фундамент ответа...",
    "⚡ собираю энергию для ответа...",
    "🗿 стою как штанга — думаю тяжело, но верно...",
    "🧭 нахожу верное направление, секунду...",
    "🛠️ докручиваю ответ, почти готово...",
]

# Интервал ротации placeholder-текста, секунды.
RUNNING_INTERVAL = 2.8


def _pick(replies: list[str]) -> str:
    return random.choice(replies)


def _pick_different(replies: list[str], exclude: Optional[str]) -> str:
    """Случайная реплика, отличная от предыдущей — иначе editText упадёт с
    "message is not modified", да и ротация без этого выглядит нечестно."""
    if len(replies) <= 1:
        return _pick(replies)
    choice = exclude
    while choice == exclude:
        choice = _pick(replies)
    return choice


class _RunningDisplay:
    """Крутит placeholder, пока модель думает. Реальные статусы от ai_trainer.ask
    (веб-поиск, конкретный tool-call — см. StatusCallback) идут через set_status и
    показывают, что происходит на самом деле; в паузах между ними (или если модель
    отвечает без единого tool-call) cycle_idle крутит случайные фразы-заполнители,
    чтобы сообщение не выглядело зависшим. Сам ответ приходит одним куском в конце
    (см. _handle_question) — без построчной печати вживую."""

    def __init__(self, placeholder: Message, initial_text: str) -> None:
        self._placeholder = placeholder
        self._last_text = initial_text
        self._lock = asyncio.Lock()

    async def set_status(self, text: str) -> None:
        async with self._lock:
            if text == self._last_text:
                return
            self._last_text = text
            with suppress(TelegramBadRequest):
                await self._placeholder.edit_text(text)

    async def cycle_idle(self) -> None:
        while True:
            await asyncio.sleep(RUNNING_INTERVAL)
            async with self._lock:
                self._last_text = _pick_different(RUNNING_REPLIES, self._last_text)
                with suppress(TelegramBadRequest):
                    await self._placeholder.edit_text(self._last_text)


class _DraftStreamer:
    """Печатает ответ тренера в пузыре-черновике, пока модель его генерирует.

    Стриминг у бота уже был и был выпилен: он строился на редактировании
    сообщения, а это лимиты на частоту правок и мигающий сырой markdown, из
    которого текст «пересобирался» на глазах. sendMessageDraft — нативный
    механизм ровно под это: черновик живёт вне ленты сообщений, анимируется
    клиентом и не оставляет следа, а готовый ответ приходит одним обычным
    сообщением, как и раньше.

    Метод появился в Bot API 9.3, поэтому любая ошибка отправки просто
    выключает стриминг до конца ответа — на старых клиентах и серверах всё
    работает как прежде, только без анимации.
    """

    def __init__(self, message: Message) -> None:
        self._message = message
        # Идентификатор черновика произвольный, но постоянный в пределах ответа:
        # им же черновик потом гасится пустым текстом.
        self._draft_id = message.message_id
        self._enabled = True
        self._started = False

    async def push(self, text: str) -> None:
        if not self._enabled or not text:
            return
        try:
            await self._message.bot.send_message_draft(
                chat_id=self._message.chat.id,
                draft_id=self._draft_id,
                text=text[-MAX_DRAFT_CHARS:],
            )
            self._started = True
        except (TelegramBadRequest, TelegramAPIError):
            # Не тот сервер/клиент, слишком часто, что угодно — молча живём
            # дальше без черновика: ответ всё равно придёт целиком.
            self._enabled = False

    async def close(self) -> None:
        """Погасить черновик, чтобы он не остался висеть рядом с ответом."""
        if not self._started:
            return
        with suppress(TelegramBadRequest, TelegramAPIError):
            await self._message.bot.send_message_draft(
                chat_id=self._message.chat.id, draft_id=self._draft_id, text=""
            )


async def ai_keyboard(
    user_id: int, answer: Optional[str] = None, program_name: Optional[str] = None
) -> InlineKeyboardMarkup:
    """AI-trainer reply keyboard: 'К тренировке' instead of 'Меню' while a workout is active.

    `answer` — текст ответа тренера, если он есть: упомянутые в нём упражнения
    пользователя становятся кнопками-ссылками на свои карточки.

    `program_name` — название программы, которую тренер собрал этим ответом
    (см. ai_trainer.propose_program): добавляет кнопку с превью и сохранением.
    """
    active = await db.get_active_workout(user_id)
    mentioned = await exercise_mentions.find_in_text(
        user_id, answer, limit=exercise_mentions.MAX_MENTIONS_TOTAL
    )
    return keyboards.ai_trainer_keyboard(
        has_active_workout=bool(active), exercises=mentioned, program_name=program_name
    )


@router.callback_query(F.data == "menu:ai")
async def menu_ai(callback: CallbackQuery, state: FSMContext):
    if not ai_trainer.is_configured():
        await callback.answer(
            "AI-тренер не настроен: администратору нужно задать XAI_API_KEY.",
            show_alert=True,
        )
        return
    await state.set_state(AITrainerFlow.chatting)
    # The conversation is deliberately NOT cleared here: stepping out to look at
    # a workout and coming back used to reset the trainer to its intro with no
    # memory of what was just discussed.
    data = await state.get_data()
    text = INTRO_TEXT if not data.get("ai_history") else RESUME_TEXT
    await ui.safe_edit(
        callback, text, reply_markup=await ai_keyboard(callback.from_user.id), parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "ai:menu")
async def ai_to_menu(callback: CallbackQuery, state: FSMContext):
    """Keeps the AI-тренер's last reply in the chat instead of deleting it —
    unlike other "back to menu" buttons, this one sits on a real conversation."""
    from handlers.workout import _show_main_menu
    await _show_main_menu(callback, state, delete_current=False)
    await callback.answer()


@router.callback_query(F.data == "ai:resume_workout")
async def ai_resume_workout(callback: CallbackQuery, state: FSMContext):
    """'К тренировке' from the AI-trainer chat — unlike menu:resume_workout, keeps the
    AI conversation in the chat instead of deleting the message the button was on.
    """
    from handlers.workout import _enter_live

    active = await db.get_active_workout(callback.from_user.id)
    if not active:
        await callback.answer("Нет активной тренировки", show_alert=True)
        return
    await callback.answer()
    await _enter_live(callback, state, active["id"], delete_message=False)


@router.callback_query(F.data.startswith("ai:excard:"))
async def ai_exercise_card(callback: CallbackQuery, state: FSMContext):
    """Карточка упражнения, упомянутого в ответе тренера.

    Шлём новым сообщением (а не правим текущее, как prog:card:), чтобы сам
    ответ с советом остался в чате — на него ещё смотреть, открыв карточку.
    """
    from handlers.exercises import send_exercise_card

    ex_id = int(callback.data.split(":")[2])
    # So the card's "⬅️ Назад" closes it (ai:closecard below) instead of
    # dropping into the exercises menu list — see show_exercise_groups, which
    # clears this once the user actually enters that menu on its own.
    await state.update_data(exm_from_ai=True)
    if not await send_exercise_card(callback.message, state, callback.from_user.id, ex_id):
        await callback.answer("Упражнение не найдено", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("ai:tpladd:"))
async def ai_add_template(callback: CallbackQuery, state: FSMContext):
    """Каталожное упражнение из ответа тренера, которого у пользователя ещё
    нет ("Из каталога на плечи бери эти: ..."): форкаем шаблон в своё и сразу
    открываем карточку — прямая ссылка на карточку тут не сработала бы, само
    упражнение у пользователя до этого тапа просто не существовало."""
    from handlers.exercises import send_exercise_card

    template_id = int(callback.data.split(":")[2])
    ex_id = await db.fork_exercise_from_template(callback.from_user.id, template_id)
    await state.update_data(exm_from_ai=True)
    if not await send_exercise_card(callback.message, state, callback.from_user.id, ex_id):
        await callback.answer("Упражнение не найдено", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data == "ai:closecard")
async def ai_close_exercise_card(callback: CallbackQuery, state: FSMContext):
    """"⬅️ Назад" on a card opened from the AI-тренер chat: just closes it (and
    its reference photos, if any) instead of opening any other screen — the
    trainer's reply is right underneath and becomes the bottom of the chat
    again, same as before the card was ever opened."""
    from handlers.exercises import _clear_exercise_media

    await _clear_exercise_media(callback.bot, callback.message.chat.id, state)
    with suppress(TelegramBadRequest):
        await callback.message.delete()
    await callback.answer()


@router.callback_query(F.data.startswith("ai:mpage:"))
async def ai_mentions_page(callback: CallbackQuery, state: FSMContext):
    """Стрелки листания упоминаний под ответом тренера — id упомянутых
    упражнений едут прямо в callback_data (см. keyboards.ai_trainer_keyboard),
    так что тут только перерисовываем клавиатуру, не трогая сам текст ответа."""
    _, _, page_str, ids_csv = callback.data.split(":", 3)
    page = int(page_str)
    exercises = []
    for raw_id in ids_csv.split(","):
        if not raw_id:
            continue
        ex = await db.get_exercise(int(raw_id))
        # A page can mix the user's own exercises with not-yet-added catalog
        # templates (see keyboards.ai_trainer_keyboard) — templates have no
        # user_id of their own, so only ownership-check the non-template rows.
        if ex is not None and (ex["is_template"] or ex["user_id"] == callback.from_user.id):
            exercises.append(ex)
    active = await db.get_active_workout(callback.from_user.id)
    draft = (await state.get_data()).get("ai_program_draft")
    program_name = draft.get("name") if draft else None
    kb = keyboards.ai_trainer_keyboard(
        has_active_workout=bool(active), exercises=exercises, page=page, program_name=program_name
    )
    with suppress(TelegramBadRequest):
        await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer()


_PROGRAM_GONE = (
    "Это предложение уже неактуально — попроси тренера собрать программу заново."
)


async def _program_draft(callback: CallbackQuery, state: FSMContext) -> Optional[dict]:
    """Черновик программы из FSM или None с алертом, если его уже нет.

    Живёт ровно один черновик на пользователя (см. _handle_question), так что
    кнопка под старым ответом вполне может указывать в пустоту — молча сохранять
    в этом случае нечего, а сохранять «что-то другое» было бы хуже всего.
    """
    data = await state.get_data()
    draft = data.get("ai_program_draft")
    if not draft or not draft.get("days"):
        await callback.answer(_PROGRAM_GONE, show_alert=True)
        return None
    return draft


@router.callback_query(F.data == "ai:prog:view")
async def ai_program_view(callback: CallbackQuery, state: FSMContext):
    """Превью программы, собранной тренером.

    Отдельным сообщением, а не правкой ответа: сам разбор с логикой сплита
    нужен рядом, пока пользователь решает, брать программу или нет.
    """
    draft = await _program_draft(callback, state)
    if draft is None:
        return
    text = formatting.build_ai_program_preview(draft["name"], draft["days"])
    await callback.message.answer(
        text, parse_mode="HTML", reply_markup=keyboards.ai_program_preview_keyboard()
    )
    await callback.answer()


@router.callback_query(F.data == "ai:prog:save")
async def ai_program_save(callback: CallbackQuery, state: FSMContext):
    """Сохранение программы: каждый её день — отдельная программа в списке.

    Здесь же и происходит единственная запись за всю фичу: до этого тапа
    предложение нигде не материализовалось, в том числе не форкало пользователю
    упражнения из каталога (см. ai_trainer._propose_program)."""
    draft = await _program_draft(callback, state)
    if draft is None:
        return

    user_id = callback.from_user.id
    days = draft["days"]
    existing = await db.count_routines(user_id)
    if existing + len(days) > ai_trainer.MAX_ROUTINES_PER_USER:
        await callback.answer(
            f"У тебя уже {existing} программ — больше {ai_trainer.MAX_ROUTINES_PER_USER} "
            "не влезет. Удали лишние в «🗂 Программы» и попробуй ещё раз.",
            show_alert=True,
        )
        return

    for day in days:
        await db.create_routine_from_program(
            # .get на target: черновик переживает перезапуск в FSM-сторадже, так
            # что тут может лежать предложение, собранное ещё прошлой версией.
            user_id, day["name"], [(item["name"], item.get("target")) for item in day["items"]]
        )
    await state.update_data(ai_program_draft=None)

    word = formatting.plural_ru(len(days), ("программу", "программы", "программ"))
    text = (
        f"✅ <b>Добавил {len(days)} {word}.</b>\n\n"
        "Ищи их в «🗂 Программы» — оттуда начинается тренировка по любой из них."
    )
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=keyboards.ai_program_saved_keyboard()
        )
    await callback.answer("Готово 💪")


@router.callback_query(F.data == "ai:prog:drop")
async def ai_program_drop(callback: CallbackQuery, state: FSMContext):
    """«Не надо»: убираем превью и черновик. Ответ тренера с разбором остаётся —
    попросить переделать программу можно прямо следующей репликой."""
    await state.update_data(ai_program_draft=None)
    with suppress(TelegramBadRequest):
        await callback.message.delete()
    await callback.answer("Убрал")


@router.callback_query(F.data.startswith("ai:comment:"))
async def ai_comment_workout(callback: CallbackQuery, state: FSMContext):
    """Ручной запрос комментария к тренировке — кнопка на карточке завершённой тренировки.

    Работает и на свежезавершённой карточке, и на карточке из истории: правит то же
    сообщение на месте, убирая из клавиатуры только саму эту кнопку.
    """
    workout_id = int(callback.data.split(":")[2])
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != callback.from_user.id:
        await callback.answer("Тренировка не найдена", show_alert=True)
        return
    if not ai_trainer.is_configured():
        await callback.answer("AI-тренер не настроен.", show_alert=True)
        return
    await callback.answer()

    comment = workout["ai_comment"]
    if not comment:
        try:
            comment = await ai_trainer.comment_on_workout(callback.from_user.id, workout_id)
        except Exception:
            logger.exception("AI trainer workout comment failed for workout %s", workout_id)
            await callback.message.answer("⚠️ Не получилось получить комментарий, попробуй ещё раз позже.")
            return
        await db.set_workout_ai_comment(workout_id, comment)

    comment_block = formatting.build_ai_comment_block(comment)
    new_text = (callback.message.html_text or "") + "\n" + comment_block
    existing_kb = callback.message.reply_markup
    rows = existing_kb.inline_keyboard if existing_kb else []
    new_rows = [
        [btn for btn in row if not (btn.callback_data or "").startswith("ai:comment:")] for row in rows
    ]
    new_rows = [r for r in new_rows if r]
    new_markup = InlineKeyboardMarkup(inline_keyboard=new_rows) if new_rows else None

    if formatting.telegram_length(new_text) > formatting.MESSAGE_LIMIT:
        # A long card plus a comment can pass Telegram's cap, and the edit was
        # wrapped in suppress() — so the user was told the comment was coming
        # and then nothing changed. Deliver it as its own message instead.
        with suppress(TelegramBadRequest):
            await callback.message.edit_reply_markup(reply_markup=new_markup)
        await callback.message.answer(comment_block, parse_mode="HTML")
        return
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(new_text, parse_mode="HTML", reply_markup=new_markup)


async def _download_photo_as_data_url(message: Message) -> Optional[str]:
    photo = message.photo[-1]
    if photo.file_size and photo.file_size > MAX_IMAGE_BYTES:
        return None
    buf = await message.bot.download(photo)
    return "data:image/jpeg;base64," + base64.b64encode(buf.read()).decode()


def _try_claim_busy(user_id: int) -> bool:
    """Atomically check-and-reserve `_busy` for this user.

    Must be called with no `await` between the membership check and the
    `.add()` — asyncio's cooperative scheduling guarantees two back-to-back
    synchronous statements can't be interleaved by another task, which two
    separately-awaited steps can. The previous shape (check here, `.add()`
    several awaits later inside `_handle_question`, after reading the daily
    count and FSM state) left exactly that gap: two fast messages from the
    same user both passed the check before either reservation landed, so both
    reached the model — double the Grok cost, and the daily-question limit
    (charged only after a successful answer) could be exceeded by however many
    requests raced through the gap.
    """
    if user_id in _busy:
        return False
    _busy.add(user_id)
    return True


async def _handle_question(
    message: Message,
    state: FSMContext,
    question: str,
    history_question: str,
    image_data_url: Optional[str] = None,
) -> None:
    """Общая логика для текстовых и фото-вопросов: запрос к модели, история, отправка ответа.

    question — то, что реально уходит модели на этот ход (текст +, если есть, фото).
    history_question — облегчённая версия для ai_history/БД: фото туда не попадают
    (не пересылать же их каждый следующий ход), только текст/подпись или заглушка.

    Caller owns the `_busy` reservation end-to-end (claimed atomically before
    any await, released in the caller's `finally`) — this function assumes the
    reservation is already held and never touches `_busy` itself.
    """
    user_id = message.from_user.id
    asked_today = await db.get_ai_question_count_today(user_id)
    if asked_today >= config.AI_QUESTION_DAILY_LIMIT:
        await message.reply(
            "На сегодня лимит вопросов исчерпан 😮‍💨 Дай тренеру передохнуть — возвращайся завтра."
        )
        return

    data = await state.get_data()
    history = data.get("ai_history", [])

    # The daily counter is charged only once there's an answer to show for it —
    # a provider outage shouldn't cost the user one of their questions.
    running_text = _pick(RUNNING_REPLIES)
    placeholder = await message.answer(running_text)
    display = _RunningDisplay(placeholder, running_text)
    running_task = asyncio.create_task(display.cycle_idle())
    streamer = _DraftStreamer(message)

    # Программа, если тренер собрал её этим ответом (см. propose_program). Держим
    # в ячейке, а не в возврате ask(): текст ответа и черновик — разные вещи, и
    # черновик может прийти в любом раунде tool-calls, в том числе не последнем.
    program_draft: dict = {}

    async def collect_program(draft: dict) -> None:
        program_draft.clear()
        program_draft.update(draft)

    try:
        answer = await ai_trainer.ask(
            user_id, question, history, image_data_url=image_data_url,
            on_status=display.set_status, on_program=collect_program,
            on_chunk=streamer.push,
        )
    except Exception:
        logger.exception("AI trainer request failed for user %s", user_id)
        with suppress(TelegramBadRequest):
            await placeholder.edit_text(
                "⚠️ Не получилось получить ответ, попробуй ещё раз чуть позже.",
                reply_markup=await ai_keyboard(user_id),
            )
        return
    finally:
        running_task.cancel()
        with suppress(asyncio.CancelledError):
            await running_task
        await streamer.close()

    await db.increment_ai_question_count(user_id)
    # Warn before the wall, not at it — the old behaviour only ever mentioned the
    # limit by refusing.
    left = config.AI_QUESTION_DAILY_LIMIT - (asked_today + 1)
    quota_note = f"\n\n<i>Осталось вопросов сегодня: {left}</i>" if 0 < left <= _QUOTA_WARN_AT else ""

    history = (
        history
        + [
            {"role": "user", "content": history_question},
            {"role": "assistant", "content": answer},
        ]
    )[-HISTORY_LIMIT:]
    # Черновик один на пользователя: новое предложение затирает старое, а ответ
    # без программы — стирает его вовсе, чтобы кнопка под прошлым ответом не
    # сохранила программу из позапрошлого разговора (см. ai_program_view).
    await state.update_data(ai_history=history, ai_program_draft=program_draft or None)

    # Full, permanent log — separate from the live window above, which is capped
    # (and lost on a restart, unlike this). Lets the model pull it back via the
    # get_full_chat_history tool if a later question references it.
    await db.add_ai_chat_message(user_id, "user", history_question)
    await db.add_ai_chat_message(user_id, "assistant", answer)

    reply_markup = await ai_keyboard(
        user_id, answer=answer, program_name=program_draft.get("name") if program_draft else None
    )
    chunks = [answer[i : i + TG_CHUNK] for i in range(0, len(answer), TG_CHUNK)]
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        markup = reply_markup if is_last else None
        html_chunk = formatting.markdown_bold_to_html(chunk)
        if is_last:
            html_chunk += quota_note
        if i == 0:
            try:
                await placeholder.edit_text(html_chunk, parse_mode="HTML", reply_markup=markup)
                continue
            except TelegramBadRequest:
                pass  # разошлось с ротацией (например текст не изменился) — просто шлём отдельным сообщением
        await message.answer(html_chunk, parse_mode="HTML", reply_markup=markup)


@router.message(AITrainerFlow.chatting, F.text)
async def ai_question(message: Message, state: FSMContext):
    question = (message.text or "").strip()
    if not question:
        return
    user_id = message.from_user.id
    if not _try_claim_busy(user_id):
        await message.reply("Секунду, ещё думаю над прошлым вопросом 😅")
        return
    try:
        await _handle_question(message, state, question, history_question=question)
    finally:
        _busy.discard(user_id)


@router.message(AITrainerFlow.chatting, F.photo)
async def ai_photo_question(message: Message, state: FSMContext):
    user_id = message.from_user.id
    # Claimed before the first await (the photo download below) — the previous
    # shape checked `_busy` here but only reserved it deep inside
    # _handle_question, leaving the whole download+dispatch window unguarded.
    if not _try_claim_busy(user_id):
        await message.reply("Секунду, ещё думаю над прошлым вопросом 😅")
        return
    try:
        caption = (message.caption or "").strip()
        question = caption or DEFAULT_PHOTO_QUESTION

        image_data_url = await _download_photo_as_data_url(message)
        if image_data_url is None:
            await message.reply("Фото слишком большое, пришли поменьше.")
            return

        history_question = f"[фото] {caption}" if caption else "[прислал фото]"
        await _handle_question(
            message, state, question, history_question=history_question, image_data_url=image_data_url
        )
    finally:
        _busy.discard(user_id)


async def _download_voice_as_file(message: Message):
    voice = message.voice
    if voice.file_size and voice.file_size > MAX_VOICE_BYTES:
        return None
    buf = await message.bot.download(voice)
    buf.name = "voice.ogg"
    return buf


@router.message(AITrainerFlow.chatting, F.voice)
async def ai_voice_question(message: Message, state: FSMContext):
    user_id = message.from_user.id
    if not _try_claim_busy(user_id):
        await message.reply("Секунду, ещё думаю над прошлым вопросом 😅")
        return
    try:
        if not ai_trainer.is_voice_configured():
            await message.reply("Голосовой ввод пока не настроен, напиши вопрос текстом.")
            return
        if message.voice.duration and message.voice.duration > MAX_VOICE_SECONDS:
            await message.reply("Голосовое слишком длинное, запиши покороче.")
            return

        voice_file = await _download_voice_as_file(message)
        if voice_file is None:
            await message.reply("Голосовое слишком большое, запиши покороче.")
            return

        try:
            question = await ai_trainer.transcribe_voice(voice_file, user_id)
        except Exception:
            logger.exception("AI trainer voice transcription failed for user %s", user_id)
            await message.reply("⚠️ Не получилось распознать голосовое, попробуй ещё раз или напиши текстом.")
            return

        if not question:
            await message.reply("🤐 Не удалось разобрать речь, попробуй ещё раз.")
            return

        # Echo what was heard: on a misheard question the answer otherwise looks
        # like the trainer hallucinating, with nothing pointing at the
        # transcription. Set logging already does this ("🎙 Записал: …").
        await message.reply(f"🎙 <i>{escape(question)}</i>", parse_mode="HTML")
        await _handle_question(message, state, question, history_question=question)
    finally:
        _busy.discard(user_id)
