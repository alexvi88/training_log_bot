"""AI-тренер: чат с Grok, у которого есть доступ к данным текущего пользователя."""

import asyncio
import base64
import json
import logging
import secrets
from contextlib import suppress
from html import escape
from typing import Any, Callable, Optional, Sequence

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import ai_trainer
import config
import db
import exercise_mentions
import formatting
import keyboards
import program_mentions
import running_texts
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
#
# Окно намеренно большое: почти любой ответ тренера (3–10 коротких абзацев) целиком
# помещается в него и не едет вовсе — текст просто дописывается снизу. С
# прежней тысячей символов окно начинало ползти уже на середине среднего
# ответа, и дальше весь пузырь ехал вверх до самого конца генерации. Ползёт
# теперь только по-настоящему длинный разбор, и то в самом конце.
MAX_DRAFT_CHARS = 2400

# Потолок Telegram на текст черновика — «после разбора entities», то есть
# считается видимый текст, а не наша HTML-разметка (см. _draft_html: таблица,
# разложенная в строки, видимый текст удлиняет, и упереться в лимит реально).
DRAFT_TEXT_LIMIT = 4096

# Минимальная пауза между двумя перерисовками черновика — см.
# config.AI_DRAFT_INTERVAL_SECONDS: это про глаза, а не про лимиты Telegram.
DRAFT_MIN_INTERVAL = config.AI_DRAFT_INTERVAL_SECONDS

# Дольше этого ждать флудвейт бессмысленно — ответ придёт раньше черновика.
MAX_DRAFT_RETRY_WAIT = 5.0

# Лимит на размер голосового (Telegram сам не режет сильнее, но перестрахуемся).
MAX_VOICE_BYTES = 20 * 1024 * 1024

# Длиннее — явно не короткий вопрос, дороже распознавать и дольше ждать ответ.
MAX_VOICE_SECONDS = 300

INTRO_TEXT = (
    "🤖 <b>ПРИВЕТ, АТЛЕТ. ТРЕНЕР НА СВЯЗИ.</b>\n\n"
    "У меня есть доступ к истории твоих тренировок и многолетний тренерский опыт. "
    "Спрашивай что угодно:\n"
    "• «Как прогресс в жиме лёжа? Почему не растёт присед?»\n"
    "• «Составь мне программу на массу 3 раза в неделю»\n"
    "• «Сколько белка есть, чтобы расти?»\n\n"
    "Пиши вопрос 👇 (можно голосом — жми на 🎤)"
)

# Shown instead of the full intro when returning to a conversation that's already
# going — repeating the whole "привет, вот что я умею" would read as if the
# trainer had forgotten the last few messages.
RESUME_TEXT = "🤖 <b>ТРЕНЕР НА СВЯЗИ.</b> Продолжаем — пиши вопрос 👇"

# Общий текст дневного лимита: показывается и на вопрос в чате, и на кнопку
# «Составить с AI-тренером» — расходовать они пытаются один и тот же счётчик.
DAILY_LIMIT_TEXT = (
    "На сегодня лимит вопросов исчерпан 😮‍💨 Дай тренеру передохнуть — возвращайся завтра."
)

# Пользователи, чей вопрос сейчас обрабатывается — защита от параллельных запросов.
_busy: set[int] = set()

# Крутятся в placeholder-сообщении, пока модель думает — вместо голого "печатает..."
# на несколько секунд/десятков секунд (особенно с tool-calls и веб-поиском под капотом).
# Сами пулы фраз и подбор темы по вопросу — в running_texts.py: там же объяснено,
# почему это важно (первая фраза видна ещё до единого tool-call).

# Интервал ротации placeholder-текста, секунды.
RUNNING_INTERVAL = 2.8


class _RunningDisplay:
    """Крутит placeholder, пока модель думает. Реальные статусы от ai_trainer.ask
    (веб-поиск, конкретный tool-call — см. StatusCallback) идут через set_status и
    показывают, что происходит на самом деле; в паузах между ними (или если модель
    отвечает без единого tool-call) cycle_idle крутит случайные фразы-заполнители из
    пула, выбранного под тему вопроса (см. running_texts.pool_for), чтобы сообщение
    не выглядело зависшим и не съезжало с темы. Сам ответ приходит одним куском в
    конце (см. _handle_question) — без построчной печати вживую."""

    def __init__(self, placeholder: Message, initial_text: str, pool: list[str]) -> None:
        self._placeholder = placeholder
        self._last_text = initial_text
        self._pool = pool
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
                self._last_text = running_texts.pick_different(self._pool, self._last_text)
                with suppress(TelegramBadRequest):
                    await self._placeholder.edit_text(self._last_text)


def _draft_tail(text: str, limit: int = MAX_DRAFT_CHARS) -> str:
    """Хвост ответа для черновика, прижатый к началу строки.

    Резать хвост ровно по счётчику символов (`text[-1000:]`) — и есть та самая
    дёрганость: каждая новая пачка букв сдвигает окно на столько же слева, весь
    видимый текст уезжает влево на каждом обновлении, и клиенту нечего
    анимировать — он перерисовывает пузырь целиком. Да ещё и начинается всё с
    середины слова.

    С привязкой к строке начало окна стоит на месте, пока эта строка из него не
    выпадет: между сдвигами текст только дописывается снизу — ровно то, что
    клиент умеет анимировать плавно, — а дёргается раз в строку, а не раз в
    пачку букв. Если строк в окне нет вовсе (сплошной абзац), отступаем к
    границе слова: это хотя бы не полслова.
    """
    if len(text) <= limit:
        return text
    window = text[-limit:]
    newline = window.find("\n")
    if newline != -1:
        return window[newline + 1 :]
    space = window.find(" ")
    return window[space + 1 :] if space != -1 else window


def _draft_html(text: str) -> str:
    """Готовый текст черновика: хвост, разобранная разметка, длина в пределах
    лимита Telegram.

    Резать приходится ДО разбора разметки (иначе в куске окажется незакрытый
    тег), а лимит считается ПОСЛЕ — и одно в другое не переводится: таблица,
    разложенная в строки, видимый текст заметно удлиняет. Поэтому не считаем, а
    проверяем и ужимаем окно, пока не влезет. Обычно цикл не проходит и одной
    итерации; упереться в пол он может только на чём-то совсем неожиданном, но
    и тогда лучше короткий черновик, чем ошибка, которая выключит стриминг до
    конца ответа.
    """
    limit = MAX_DRAFT_CHARS
    while True:
        html = formatting.ai_markdown_to_html(_draft_tail(text, limit))
        if formatting.telegram_length(html) <= DRAFT_TEXT_LIMIT or limit <= 400:
            return html
        limit //= 2


class _DraftStreamer:
    """Печатает ответ тренера в пузыре-черновике, пока модель его генерирует.

    Стриминг у бота уже был и был выпилен: он строился на редактировании
    сообщения, а это лимиты на частоту правок и мигающий сырой markdown, из
    которого текст «пересобирался» на глазах. sendMessageDraft — нативный
    механизм ровно под это: черновик живёт вне ленты сообщений, анимируется
    клиентом и не оставляет следа, а готовый ответ приходит одним обычным
    сообщением, как и раньше.

    Метод появился в Bot API 9.3, поэтому отсутствие метода на сервере просто
    выключает стриминг до конца ответа — на старых клиентах и серверах всё
    работает как прежде, только без анимации.

    Отправка живёт в отдельной задаче, а `push` только запоминает последний
    текст и мгновенно возвращает управление. Раньше `push` слал черновик прямо
    из цикла чтения стрима модели: один медленный (или флудвейтнутый) запрос в
    Telegram останавливал вычитывание дельт, они копились в сокете — черновик
    замирал на полуслове, а потом весь ответ «пробегал» разом. Теперь сеть
    Telegram и чтение стрима не ждут друг друга: подтормаживает отправка —
    просто пропускаем промежуточные состояния и показываем самое свежее.
    """

    def __init__(self, message: Message, on_start: Optional[Callable] = None) -> None:
        self._message = message
        # Идентификатор черновика произвольный, но постоянный в пределах ответа:
        # им же черновик потом гасится пустым текстом.
        self._draft_id = message.message_id
        self._enabled = True
        self._started = False
        # Вызывается ровно один раз, когда черновик реально появился на экране —
        # чтобы вызывающая сторона убрала "думаю..." placeholder: два индикатора
        # ожидания разом (статичный текст сообщением выше и печатающийся черновик
        # снизу) читаются как баг, а не как прогресс.
        self._on_start = on_start
        # Последний непоказанный текст: писатель всегда берёт свежайший, а не
        # очередь — промежуточные состояния черновика никому не нужны.
        self._pending: Optional[str] = None
        self._wake = asyncio.Event()
        self._writer: Optional[asyncio.Task] = None

    async def push(self, text: str) -> None:
        """Отдать черновику новый текст. Не ждёт сети — только будит писателя."""
        if not self._enabled or not text:
            return
        # Копим сырой текст целиком: во что именно он превратится на экране,
        # решает _draft_html уже в момент отправки — промежуточные состояния
        # всё равно выбрасываются, и обрезать их незачем.
        self._pending = text
        if self._writer is None:
            self._writer = asyncio.create_task(self._run())
        self._wake.set()

    async def _run(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            text, self._pending = self._pending, None
            if text is None:
                continue
            if not await self._send(text):
                self._enabled = False
                self._pending = None
                return
            if not self._started:
                self._started = True
                if self._on_start:
                    await self._on_start()
            # Пауза между черновиками: Telegram не любит частых правок, а
            # человек всё равно не читает быстрее.
            await asyncio.sleep(DRAFT_MIN_INTERVAL)

    async def _send(self, text: str) -> bool:
        """True — черновик ушёл (или стоит попробовать ещё раз), False — сдаёмся."""
        try:
            await self._message.bot.send_message_draft(
                chat_id=self._message.chat.id,
                draft_id=self._draft_id,
                # Модель печатает markdown, а черновик — обычное сообщение:
                # без разбора разметки в пузыре мигали сырые «**» ровно там,
                # где тренер называет упражнение, то есть в каждой второй
                # строке.
                text=_draft_html(text) if text else text,
                parse_mode="HTML",
            )
            return True
        except TelegramRetryAfter as e:
            # Слишком часто — это не «сервер не умеет», а «подожди»: раньше
            # флудвейт насовсем гасил черновик, и он замирал до самого ответа.
            await asyncio.sleep(min(e.retry_after, MAX_DRAFT_RETRY_WAIT))
            self._pending = text
            self._wake.set()
            return True
        except (TelegramBadRequest, TelegramAPIError):
            # Не тот сервер/клиент, что угодно — молча живём дальше без
            # черновика: ответ всё равно придёт целиком.
            return False

    async def close(self) -> None:
        """Погасить черновик, чтобы он не остался висеть рядом с ответом."""
        if self._writer is not None:
            self._writer.cancel()
            with suppress(asyncio.CancelledError):
                await self._writer
            self._writer = None
        self._enabled = False
        if not self._started:
            return
        with suppress(TelegramBadRequest, TelegramAPIError):
            await self._message.bot.send_message_draft(
                chat_id=self._message.chat.id, draft_id=self._draft_id, text=""
            )


async def ai_keyboard(
    user_id: int,
    answer: Optional[str] = None,
    program_name: Optional[str] = None,
    draft_id: Optional[str] = None,
    actions: Sequence[dict] = (),
) -> InlineKeyboardMarkup:
    """AI-trainer reply keyboard: 'К тренировке' instead of 'Меню' while a workout is active.

    `answer` — текст ответа тренера, если он есть: упомянутые в нём упражнения
    пользователя становятся кнопками-ссылками на свои карточки.

    `program_name` / `draft_id` — название и id черновика программы, которую
    тренер собрал этим ответом (см. ai_trainer.propose_program и 5.2): вместе
    дают кнопку с превью, а id в её callback_data — то, чем ai_program_view
    отличает актуальный черновик от более старого, показанного под прошлым
    ответом (см. _program_draft).

    `actions` — то, что тренер предложил сделать, но не сделал (удалить
    программу, объединить две, поделиться, заархивировать упражнение — см.
    ai_trainer.ActionCallback): каждое становится кнопкой над списком.
    """
    active = await db.get_active_workout(user_id)
    mentioned = await exercise_mentions.find_in_text(
        user_id, answer, limit=exercise_mentions.MAX_MENTIONS_TOTAL
    )
    # Программы, названные в ответе, — ссылками на них же. Лимит общий с
    # упражнениями: под ответом место одно и то же.
    programs = await program_mentions.find_in_text(user_id, answer)
    return keyboards.ai_trainer_keyboard(
        has_active_workout=bool(active),
        exercises=mentioned,
        programs=programs,
        program_name=program_name,
        draft_id=draft_id,
        actions=actions,
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


BUILD_PROGRAM_INTRO = (
    "🤖 <b>ОКЕЙ, СОБИРАЕМ ПРОГРАММУ.</b>\n\n"
    "Сейчас задам пару вопросов — отвечай, и заберёшь готовый план."
)

# Уходит тренеру от лица пользователя, чтобы не заставлять его печатать этот же
# запрос вручную (см. ai_build_program). Про вопросы сказано явно: системный
# промпт разрешает тренеру собрать программу сразу на дефолтах, если человек
# отмахивается, а вся суть этой кнопки — наоборот, провести через уточнения.
BUILD_PROGRAM_SEED = (
    "Хочу собрать программу тренировок. Сначала задай мне уточняющие вопросы "
    "по вводным, которых не видно из моей истории, и только после моих ответов "
    "собирай программу."
)


@router.callback_query(F.data == "ai:buildprog")
async def ai_build_program(callback: CallbackQuery, state: FSMContext):
    """Кнопка «Составить с AI-тренером» в 🗂 Программы.

    Не просто открывает чат тренера, а сразу запускает сценарий сбора программы
    (уточняющие вопросы → propose_program → превью с сохранением): иначе, чтобы
    получить план, надо было самому догадаться попросить об этом словами.
    """
    if not ai_trainer.is_configured():
        await callback.answer(
            "AI-тренер не настроен: администратору нужно задать XAI_API_KEY.",
            show_alert=True,
        )
        return
    user_id = callback.from_user.id
    if not _try_claim_busy(user_id):
        await callback.answer("Секунду, ещё думаю над прошлым вопросом 😅", show_alert=True)
        return
    try:
        # Was outside this try: a stale-button tap makes Telegram reject this
        # answer with "query is too old", and the finally below never runs —
        # the reservation leaks forever, and every future message/photo/voice
        # from this user reports "busy" for the life of the process (A5).
        with suppress(TelegramBadRequest):
            await callback.answer()
        # Лимит — до интро, а не после: бодрое «ОКЕЙ, СОБИРАЕМ ПРОГРАММУ» с
        # мгновенным «лимит исчерпан» следом читалось как обещание, которое
        # бот сам тут же забирает назад.
        if await db.get_ai_question_count_today(user_id) >= config.AI_QUESTION_DAILY_LIMIT:
            await ui.safe_edit(
                callback, DAILY_LIMIT_TEXT,
                reply_markup=await ai_keyboard(user_id), parse_mode="HTML",
            )
            return
        await state.set_state(AITrainerFlow.chatting)
        # С клавиатурой, а не голым текстом: пока тренер думает, это единственный
        # экран на месте меню — а ответа может и не быть вовсе (исчерпан дневной
        # лимит вопросов, сбой провайдера), и без кнопок выход остался бы только
        # через нижнее меню.
        screen = await ui.safe_edit(
            callback, BUILD_PROGRAM_INTRO,
            reply_markup=await ai_keyboard(user_id), parse_mode="HTML",
        )
        await _handle_question(
            screen, state, BUILD_PROGRAM_SEED,
            history_question=BUILD_PROGRAM_SEED, user_id=user_id,
        )
    finally:
        _busy.discard(user_id)


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


@router.callback_query(F.data.startswith("ai:pgmmergeask:"))
async def ai_program_merge_confirm(callback: CallbackQuery, state: FSMContext):
    """«Точно объединить?» перед rt:pgmmerge.

    Ручной путь к объединению спрашивает это же (см. handlers/routines,
    rt_program_rename_entered), и пропускать вопрос только потому, что о слиянии
    попросили словами, нельзя: разобрать программы обратно UI не умеет.
    """
    _, _, source_s, target_s = callback.data.split(":")
    source = await db.get_program(int(source_s))
    target = await db.get_program(int(target_s))
    user_id = callback.from_user.id
    if (
        source is None or target is None
        or source["user_id"] != user_id or target["user_id"] != user_id
    ):
        await callback.answer("Программа не найдена", show_alert=True)
        return
    days = await db.list_program_days_by_id(source["id"])
    word = formatting.plural_ru(len(days), ("день", "дня", "дней"))
    await callback.message.answer(
        f"Перенести все {len(days)} {word} из «{escape(source['name'])}» в "
        f"«{escape(target['name'])}»? «{escape(source['name'])}» после этого исчезнет, "
        "а разобрать их обратно уже не получится. История тренировок не пострадает.",
        reply_markup=keyboards.yes_no_keyboard(
            yes_cb=f"rt:pgmmerge:{source['id']}:{target['id']}",
            no_cb="ai:menu",
            yes_text="🔗 Объединить", no_text="❌ Отмена",
        ),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ai:exarchask:"))
async def ai_exercise_archive_confirm(callback: CallbackQuery, state: FSMContext):
    """«Точно в архив?» — тот же вопрос, что и в ⚙️ Упражнения.

    Своя копия, а не exm:archiveask: тот живёт под StateFilter экрана
    упражнений, и из чата с тренером просто не сработал бы.
    """
    ex_id = int(callback.data.split(":")[2])
    ex = await db.get_exercise(ex_id)
    if ex is None or ex["user_id"] != callback.from_user.id or ex["is_template"]:
        await callback.answer("Упражнение не найдено", show_alert=True)
        return
    await callback.message.answer(
        f"Убрать «{escape(ex['display_name'])}» из списка упражнений? История и "
        "рекорды останутся, вернуть можно в ⚙️ Упражнения → 🗄 Архив.",
        reply_markup=keyboards.yes_no_keyboard(
            yes_cb=f"ai:exarchyes:{ex_id}", no_cb="ai:menu",
            yes_text="🗄 В архив", no_text="❌ Отмена",
        ),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ai:exarchyes:"))
async def ai_exercise_archive(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    ex = await db.get_exercise(ex_id)
    if ex is None or ex["user_id"] != callback.from_user.id or ex["is_template"]:
        await callback.answer("Упражнение не найдено", show_alert=True)
        return
    await db.archive_exercise(ex_id)
    await callback.answer("В архиве")
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            f"🗄 «{escape(ex['display_name'])}» в архиве.",
            reply_markup=await ai_keyboard(callback.from_user.id),
            parse_mode="HTML",
        )


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


async def _owned_program_target(user_id: int, prefix: str, item_id: int) -> Optional[dict]:
    """Ссылка на программу из callback_data — обратно в цель для клавиатуры,
    с проверкой владельца: id в callback_data приходит от клиента."""
    if prefix == "p":
        program = await db.get_program(item_id)
        if program is None or program["user_id"] != user_id:
            return None
        return {"kind": "program", "id": program["id"], "name": program["name"]}
    routine = await db.get_routine(item_id)
    if routine is None or routine["user_id"] != user_id:
        return None
    return {"kind": "routine", "id": routine["id"], "name": routine["name"]}


@router.callback_query(F.data.startswith("ai:mpage:"))
async def ai_mentions_page(callback: CallbackQuery, state: FSMContext):
    """Стрелки листания упоминаний под ответом тренера — ссылки на упомянутое
    едут прямо в callback_data (см. keyboards.ai_trainer_keyboard), так что тут
    только перерисовываем клавиатуру, не трогая сам текст ответа.

    Программы отличаются от упражнений префиксом p/r (многодневка / одиночный
    день) — см. keyboards.ai_mention_ref."""
    _, _, page_str, refs_csv = callback.data.split(":", 3)
    page = int(page_str)
    user_id = callback.from_user.id
    exercises = []
    programs = []
    for ref in refs_csv.split(","):
        if not ref:
            continue
        if ref[0] in "pr":
            target = await _owned_program_target(user_id, ref[0], int(ref[1:]))
            if target is not None:
                programs.append(target)
            continue
        ex = await db.get_exercise(int(ref))
        # A page can mix the user's own exercises with not-yet-added catalog
        # templates (see keyboards.ai_trainer_keyboard) — templates have no
        # user_id of their own, so only ownership-check the non-template rows.
        if ex is not None and (ex["is_template"] or ex["user_id"] == user_id):
            exercises.append(ex)
    active = await db.get_active_workout(user_id)
    data = await state.get_data()
    draft = data.get("ai_program_draft")
    program_name = draft.get("name") if draft else None
    draft_id = draft.get("id") if draft else None
    kb = keyboards.ai_trainer_keyboard(
        has_active_workout=bool(active),
        exercises=exercises,
        programs=programs,
        page=page,
        program_name=program_name,
        draft_id=draft_id,
        actions=data.get("ai_actions") or (),
    )
    with suppress(TelegramBadRequest):
        await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer()


# Совет «собрать заново» здесь раньше приводил к дубликатам: алерт показывается
# и после УСПЕШНОГО сохранения (черновик израсходован — кнопка под старым
# превью и должна отвечать так), и человек, следуя совету, просил вторую копию.
_PROGRAM_GONE = (
    "Это предложение уже неактуально. Если ты его сохранял — программа уже в "
    "«🗂 Программы»; если нет — попроси тренера собрать заново."
)


def _draft_id_from(callback_data: str) -> str:
    """Последний сегмент callback_data ("ai:prog:save:1a2b3c4d" → "1a2b3c4d").

    Id черновика — непрозрачный токен (см. _handle_question), поэтому сегмент
    не парсится в число, а сравнивается с id из FSM как строка.
    """
    return callback_data.rsplit(":", 1)[-1]


async def _program_draft(
    callback: CallbackQuery, state: FSMContext, draft_id: str
) -> Optional[dict]:
    """Черновик программы из FSM, только если его id совпал с тем, что в
    callback_data — иначе алерт и None.

    Живёт ровно один черновик на пользователя (см. _handle_question), а кнопки
    под старыми ответами остаются тапабельными (5.2): без сверки по id тап под
    устаревшим ответом тренера мог сохранить/удалить более позднюю программу,
    которую пользователь даже не видел, и ничем не был бы отличим от тапа под
    актуальным ответом.
    """
    data = await state.get_data()
    draft = data.get("ai_program_draft")
    # Сравнение строковое: в callback_data id всегда приезжает текстом, а в FSM
    # он мог быть записан и числом (кнопки со старыми счётчиками живут в чате
    # вечно) — сравнение int с str молча отвергало бы легитимный тап.
    if (
        not draft
        or not draft.get("days")
        or draft.get("id") is None
        or str(draft.get("id")) != draft_id
    ):
        await callback.answer(_PROGRAM_GONE, show_alert=True)
        return None
    return draft


@router.callback_query(F.data.startswith("ai:prog:view:"))
async def ai_program_view(callback: CallbackQuery, state: FSMContext):
    """Превью программы, собранной тренером.

    Отдельным сообщением, а не правкой ответа: сам разбор с логикой сплита
    нужен рядом, пока пользователь решает, брать программу или нет.
    """
    draft_id = _draft_id_from(callback.data)
    draft = await _program_draft(callback, state, draft_id)
    if draft is None:
        return
    replaces = draft.get("replaces")
    text = formatting.build_ai_program_preview(
        draft["name"], draft["days"], replaces=replaces, notes=draft.get("notes")
    )
    await callback.message.answer(
        text,
        parse_mode="HTML",
        reply_markup=keyboards.ai_program_preview_keyboard(replacing=bool(replaces), draft_id=draft_id),
    )
    await callback.answer()


def _name_conflict_keyboard(draft_id: str, program_name: str) -> InlineKeyboardMarkup:
    """A2: у пользователя уже есть программа с этим именем, а предложение —
    НЕ её правка (иначе ai_trainer.propose_program.replaces_program было бы
    заполнено). Раньше в этом случае дни молча дописывались в существующую
    программу под тем же именем — три таких сохранения подряд давали 18 дней
    в одной программе. Теперь решает пользователь: заменить или завести
    отдельную копию под свободным именем (db.unique_program_name).

    Не через keyboards.py — та функция зарезервирована под ровно другой экран
    (`ai_program_preview_keyboard`), а этот собирается только здесь, в момент
    обнаруженного конфликта.
    """
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Заменить существующую", callback_data=f"ai:prog:replace:{draft_id}")
    b.button(text="➕ Добавить второй копией", callback_data=f"ai:prog:copy:{draft_id}")
    b.button(text="❌ Не надо", callback_data=f"ai:prog:drop:{draft_id}")
    b.adjust(1)
    return b.as_markup()


async def _create_program_day(user_id: int, day: dict, program_id: int) -> int:
    """Создать один день программы и, если тренер задал прогрессию хоть на одно
    упражнение, записать её (5.6/3.2 — db.set_routine_exercise_progression).

    Порядок routine_exercises после create_routine_from_program совпадает с
    порядком day["items"] (тот же список, без пропусков — дубли и нерезолвнутые
    имена уже отфильтрованы в ai_trainer._propose_program), поэтому сверяем по
    display_name, а не по позиции — устойчивее, если это когда-нибудь перестанет
    быть так.
    """
    routine_id = await db.create_routine_from_program(
        user_id, day["name"],
        [(item["name"], item.get("target")) for item in day["items"]],
        program_id=program_id,
    )
    progressions = {
        item["name"]: item["progression"] for item in day["items"] if item.get("progression")
    }
    if progressions:
        for re_row in await db.list_routine_exercises(routine_id):
            progression = progressions.get(re_row["display_name"])
            if progression:
                await db.set_routine_exercise_progression(
                    re_row["id"], json.dumps(progression, ensure_ascii=False)
                )
    return routine_id


async def _announce_saved(
    callback: CallbackQuery, name: str, day_count: int, replacing: bool, program_id: int
) -> None:
    """`program_id` — только что сохранённая программа: кнопка «Открыть
    программу» ведёт прямо в неё (rt:prg: без StateFilter, так что работает и
    из состояния чата с тренером)."""
    day_word = formatting.plural_ru(day_count, ("день", "дня", "дней"))
    if replacing:
        text = (
            f"✅ <b>Обновил программу «{escape(name)}».</b>\n\n"
            f"Теперь в ней {day_count} {day_word} — ищи в «🗂 Программы»."
        )
    elif day_count == 1:
        # «Тренировка по любому из дней» на программе из одного дня читалась
        # как нелепость — дня-то одного и хватает.
        text = (
            f"✅ <b>Добавил программу «{escape(name)}».</b>\n\n"
            "Ищи её в «🗂 Программы» — оттуда и начинается тренировка."
        )
    else:
        # Одна программа с общим именем на все дни, а не «N программ» — «🗂
        # Программы» покажет её одной строкой с числом дней внутри, а не N
        # отдельных (см. A8: старый текст обещал поведение, которого никогда
        # не было).
        text = (
            f"✅ <b>Добавил программу «{escape(name)}» — {day_count} {day_word}.</b>\n\n"
            "Ищи её в «🗂 Программы» — оттуда начинается тренировка по любому из дней."
        )
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=keyboards.ai_program_saved_keyboard(program_id)
        )
    await callback.answer("Готово 💪")


async def _save_into_existing_program(
    callback: CallbackQuery,
    state: FSMContext,
    user_id: int,
    draft: dict,
    program: Any,
    outcome: Optional[dict] = None,
) -> None:
    """A7: правка уже сохранённой программы, резолвится по id заново прямо
    здесь — в момент тапа, а не по снимку, сделанному при предложении.

    Дни программы перечитываются из БД сейчас (а не берутся из
    replaces["routine_ids"], зафиксированных на момент propose_program) — день,
    который пользователь успел добавить руками между предложением и тапом,
    раньше переживал замену и вылезал в списке первым; теперь заменяется весь
    текущий набор дней программы, как и обещает промпт тренера ("что не
    прислал — то из программы пропадёт"). Имя программы меняется, только если
    тренер прислал другое: старое поведение (запись новых дней под draft["name"]
    отдельной строкой) молча откатывало переименование, сделанное пользователем
    между предложением и тапом.
    """
    days = draft["days"]
    old_days = await db.list_program_days_by_id(program["id"])
    budget_msg = await db.routine_budget(user_id, adding=len(days), freeing=len(old_days))
    if budget_msg:
        await state.update_data(ai_program_draft=draft)
        await callback.answer(budget_msg, show_alert=True)
        return

    # Переименование — если draft["name"] отличается от имени, которое тренер
    # РЕЗОЛВИЛ при предложении (replaces["name"] — то, что видела модель,
    # снятое в момент propose_program), а не от текущего живого имени
    # программы: сравнение с live-именем спутало бы «модель хочет
    # переименовать» с «пользователь успел переименовать руками между
    # предложением и тапом» — второе не должно откатываться так, как раньше
    # (новые дни писались под draft["name"] отдельной строкой, стирая ручное
    # переименование молча).
    resolved_name = (draft.get("replaces") or {}).get("name") or program["name"]
    target_name = program["name"]
    renamed_by_trainer = draft["name"].strip().lower() != resolved_name.strip().lower()
    # Если имя занято другой программой, rename вернёт False — просто оставляем
    # текущее: это правка состава, а не переименования, отказывать из-за него незачем.
    if renamed_by_trainer and await db.rename_program_by_id(program["id"], draft["name"]):
        target_name = draft["name"]

    # Дальше начинается запись в ЧУЖУЮ для черновика программу — при падении
    # её нельзя удалять как обрубок (см. _run_program_save), там старые дни
    # пользователя.
    if outcome is not None:
        outcome["into_existing"] = True
    # Сначала новые дни, потом удаление старых (A6): падение посередине
    # оставляет пользователя с лишними новыми днями рядом со старой
    # программой — хуже, чем идеально, но старая версия цела и есть с чем
    # попробовать снова, а не пусто с обеих сторон.
    for day in days:
        await _create_program_day(user_id, day, program_id=program["id"])
    for old in old_days:
        await db.delete_routine(old["id"])

    final_days = await db.list_program_days_by_id(program["id"])
    await _announce_saved(callback, target_name, len(final_days), replacing=True, program_id=program["id"])


async def _save_as_new_program(
    callback: CallbackQuery,
    state: FSMContext,
    user_id: int,
    draft: dict,
    freeing_routine_id: Optional[int] = None,
    outcome: Optional[dict] = None,
) -> None:
    """Новая программа — включая случай, когда предложение заменяло одиночную
    (однодневную) программу: у неё нет program_id, поэтому под неё заводится
    новая программа, а старый день удаляется отдельно (`freeing_routine_id`).

    A2: если имя уже занято другой сохранённой программой пользователя, это
    больше не решается угадыванием (см. db.create_program — коллизия теперь
    None, а не молчаливый merge внутри существующей программы) — пользователь
    выбирает сам через _name_conflict_keyboard.
    """
    days = draft["days"]
    freed = 1 if freeing_routine_id else 0
    budget_msg = await db.routine_budget(user_id, adding=len(days), freeing=freed)
    if budget_msg:
        await state.update_data(ai_program_draft=draft)
        await callback.answer(budget_msg, show_alert=True)
        return

    program_id = await db.create_program(user_id, draft["name"], source="ai")
    if program_id is None:
        await state.update_data(ai_program_draft=draft)
        with suppress(TelegramBadRequest):
            await callback.message.edit_text(
                f"У тебя уже есть программа «{escape(draft['name'])}». Заменить её этой "
                "или добавить как отдельную?",
                parse_mode="HTML",
                reply_markup=_name_conflict_keyboard(draft["id"], draft["name"]),
            )
        await callback.answer()
        return

    # Свежесозданная программа: если запись дней ниже упадёт, её нужно убрать
    # целиком (см. _run_program_save) — иначе в «🗂 Программы» остаётся
    # обрубок с частью дней, а create_program+дни не транзакция.
    if outcome is not None:
        outcome["created_program_id"] = program_id

    for day in days:
        await _create_program_day(user_id, day, program_id=program_id)
    if freeing_routine_id is not None:
        await db.delete_routine(freeing_routine_id)
    await _announce_saved(callback, draft["name"], len(days), replacing=False, program_id=program_id)


async def _finalize_program_save(
    callback: CallbackQuery, state: FSMContext, user_id: int, draft: dict, outcome: Optional[dict] = None
) -> None:
    """Все пути сохранения черновика после того, как он атомарно забран из FSM.

    Правка сохранённой многодневки идёт в _save_into_existing_program; всё
    остальное (новая программа, замена одиночной программы, конфликт имени
    из ai:prog:replace/copy) — в _save_as_new_program.
    """
    replaces = draft.get("replaces")
    if replaces and replaces.get("kind") == "program":
        program = await db.get_program(replaces["id"])
        if program is not None and program["user_id"] == user_id:
            await _save_into_existing_program(callback, state, user_id, draft, program, outcome=outcome)
            return
        # Программу удалили (или это был чужой id) между предложением и тапом —
        # заменять нечего, значит просто добавляем (см. тест A6/A7 fallback).
        replaces = None

    freeing_routine_id = None
    if replaces and replaces.get("kind") == "routine":
        routine = await db.get_routine(replaces["id"])
        if routine is not None and routine["user_id"] == user_id:
            freeing_routine_id = routine["id"]

    await _save_as_new_program(
        callback, state, user_id, draft, freeing_routine_id=freeing_routine_id, outcome=outcome
    )


# Тексты честного отказа при упавшем сохранении: кнопка «ещё раз» рядом, потому
# что черновик к этому моменту уже возвращён в FSM и она снова живая.
_SAVE_FAILED_NEW = (
    "⚠️ Не получилось сохранить программу — ничего не записал.\n"
    "Предложение тренера живо: нажми кнопку ещё раз."
)
_SAVE_FAILED_REPLACE = (
    "⚠️ Не получилось обновить программу: часть новых дней могла успеть добавиться "
    "рядом со старыми — загляни в «🗂 Программы» и проверь.\n"
    "Предложение тренера живо: нажми кнопку ещё раз."
)


async def _run_program_save(
    callback: CallbackQuery, state: FSMContext, user_id: int, draft: dict, action: Callable
) -> None:
    """Предохранитель всех путей сохранения черновика.

    Черновик забирается из FSM ДО записи (атомарность против двойного тапа,
    см. ai_program_save), поэтому необработанное исключение раньше означало
    сразу две потери: кнопка «Добавить себе» навсегда отвечала «уже
    неактуально», а в «🗂 Программы» мог остаться обрубок — программа без части
    дней (create_program и дни пишутся отдельными запросами, не транзакцией).

    Здесь при любом падении: черновик возвращается в FSM (кнопка снова живая),
    свежесозданный обрубок удаляется, человеку — честное сообщение вместо
    тишины. Для replace-пути обрубок не удаляется — там живёт старая программа
    пользователя, и лишние новые дни рядом с ней лучше пустоты; поэтому текст
    у него свой. Re-raise не нужен: наружу падение не скажет ничего, чего не
    скажет лог, а сообщение пользователю уже отправлено.
    """
    outcome = {"created_program_id": None, "into_existing": False}
    try:
        await action(outcome)
    except Exception:
        logger.exception("AI program save failed for user %s", user_id)
        await state.update_data(ai_program_draft=draft)
        if outcome["created_program_id"] is not None:
            # Удаление лучших усилий: если и оно упало (например, лежит БД),
            # обрубок переживёт до ручной чистки — но сообщение ниже всё равно
            # должно дойти.
            with suppress(Exception):
                await db.delete_program_by_id(outcome["created_program_id"])
        text = _SAVE_FAILED_REPLACE if outcome["into_existing"] else _SAVE_FAILED_NEW
        with suppress(TelegramBadRequest, TelegramAPIError):
            await callback.message.answer(
                text,
                reply_markup=keyboards.ai_program_preview_keyboard(
                    replacing=outcome["into_existing"], draft_id=str(draft["id"])
                ),
            )
        with suppress(TelegramBadRequest):
            await callback.answer()


@router.callback_query(F.data.startswith("ai:prog:save:"))
async def ai_program_save(callback: CallbackQuery, state: FSMContext):
    """Сохранение программы.

    Здесь же и происходит единственная запись за всю фичу: до этого тапа
    предложение нигде не материализовалось, в том числе не форкало пользователю
    упражнения из каталога (см. ai_trainer._propose_program)."""
    draft_id = _draft_id_from(callback.data)
    draft = await _program_draft(callback, state, draft_id)
    if draft is None:
        return

    # Забираем черновик атомарно: между этой строкой и .get_data() выше нет ни
    # одного await, который реально отдаёт управление циклу событий (FSMContext
    # поверх MemoryStorage ничего не ждёт по-настоящему) — значит второй
    # параллельный тап уже не увидит этот черновик и получит _PROGRAM_GONE
    # вместо повторного сохранения (см. A3).
    await state.update_data(ai_program_draft=None)
    user_id = callback.from_user.id

    async def action(outcome: dict) -> None:
        await _finalize_program_save(callback, state, user_id, draft, outcome=outcome)

    await _run_program_save(callback, state, user_id, draft, action)


@router.callback_query(F.data.startswith("ai:prog:replace:"))
async def ai_program_replace_conflict(callback: CallbackQuery, state: FSMContext):
    """A2: пользователь выбрал «заменить» на экране конфликта имён."""
    draft_id = _draft_id_from(callback.data)
    draft = await _program_draft(callback, state, draft_id)
    if draft is None:
        return
    await state.update_data(ai_program_draft=None)
    user_id = callback.from_user.id

    async def action(outcome: dict) -> None:
        existing = await db.find_program_by_name(user_id, draft["name"])
        if existing is None:
            # Программу с этим именем успели удалить между вопросом и тапом —
            # заменять уже нечего, просто добавляем как новую.
            await _save_as_new_program(callback, state, user_id, draft, outcome=outcome)
            return
        await _save_into_existing_program(callback, state, user_id, draft, existing, outcome=outcome)

    await _run_program_save(callback, state, user_id, draft, action)


@router.callback_query(F.data.startswith("ai:prog:copy:"))
async def ai_program_copy_conflict(callback: CallbackQuery, state: FSMContext):
    """A2: пользователь выбрал «добавить второй копией» на экране конфликта имён —
    сохраняем под ближайшим свободным именем (db.unique_program_name), а не под
    занятым."""
    draft_id = _draft_id_from(callback.data)
    draft = await _program_draft(callback, state, draft_id)
    if draft is None:
        return
    await state.update_data(ai_program_draft=None)
    user_id = callback.from_user.id

    async def action(outcome: dict) -> None:
        alt_name = await db.unique_program_name(user_id, draft["name"], suffix="2")
        renamed = dict(draft)
        renamed["name"] = alt_name
        await _save_as_new_program(callback, state, user_id, renamed, outcome=outcome)

    # При падении в FSM возвращается исходный черновик (с исходным именем):
    # свободное имя всё равно пересчитывается заново на каждом тапе.
    await _run_program_save(callback, state, user_id, draft, action)


@router.callback_query(F.data.startswith("ai:prog:drop:"))
async def ai_program_drop(callback: CallbackQuery, state: FSMContext):
    """«Не надо»: убираем превью и черновик. Ответ тренера с разбором остаётся —
    попросить переделать программу можно прямо следующей репликой.

    Черновик очищается, только если его id совпал с тем, что в кнопке (5.2) —
    иначе это тап по устаревшей кнопке, и он не должен стирать более новый,
    ещё не показанный черновик."""
    draft_id = _draft_id_from(callback.data)
    data = await state.get_data()
    current_id = (data.get("ai_program_draft") or {}).get("id")
    # Строковое сравнение — по той же причине, что в _program_draft.
    if current_id is not None and str(current_id) == draft_id:
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


def _rich(markdown: str):
    """Ответ тренера как rich-сообщение: markdown разбирает сам Telegram."""
    from aiogram.types import InputRichMessage

    return InputRichMessage(markdown=markdown)


# Rich-сообщений может не быть по трём независимым причинам — сервер Bot API
# ниже 10.1 (ошибка метода), aiogram без InputRichMessage (ImportError внутри
# _rich) и не тот тип аргумента у старого метода. Разбирать их по отдельности
# незачем: любая означает ровно одно — этому пользователю ответ уходит обычным
# сообщением, как и до 10.1.
_NO_RICH = (TelegramAPIError, AttributeError, TypeError, ImportError)


async def _edit_rich(placeholder: Message, markdown: str, markup) -> bool:
    """Переписать «думаю...» готовым ответом прямо на месте (rich_message у
    editMessageText, Bot API 10.1) — placeholder уже висит на экране, и лишнее
    сообщение вместо него читалось бы как ответ дважды."""
    try:
        await placeholder.bot.edit_message_text(
            chat_id=placeholder.chat.id,
            message_id=placeholder.message_id,
            rich_message=_rich(markdown),
            reply_markup=markup,
        )
        return True
    except _NO_RICH:
        return False


async def _answer_rich(message: Message, markdown: str, markup) -> bool:
    try:
        await message.answer_rich(rich_message=_rich(markdown), reply_markup=markup)
        return True
    except _NO_RICH:
        return False


async def _send_rich_answer(
    message: Message,
    placeholder: Message,
    chunks: list[str],
    quota_md: str,
    quota_html: str,
    markup,
) -> bool:
    """Ответ тренера настоящим rich-сообщением (Bot API 10.1).

    Модель отвечает markdown'ом, а обычное сообщение Telegram из всего markdown
    понимает только жирный и курсив: заголовки приезжали решётками, таблицы —
    палками и дефисами (см. ai_markdown_to_html, который это разгребает). В
    rich-сообщении markdown разбирает сам Telegram — и таблица остаётся
    таблицей, а заголовок заголовком.

    False — rich не поддержан и НИЧЕГО не отправлено: вызывающая сторона шлёт
    тот же ответ обычным HTML. Признаком служит первый же кусок: поддержка
    рича — свойство сервера и клиента, а не конкретного сообщения, так что
    отказ на первом означает отказ на всех. Сбой на любом следующем куске —
    это уже частная неудача (ответ наполовину на экране), и хвост дошлём
    обычным сообщением, чтобы он не потерялся вовсе.
    """
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        chunk_markup = markup if is_last else None
        # Заголовки в rich — это статейная вёрстка: крупный кегль и широкие
        # поля, из-за которых ответ разъезжается пустотой на пол-экрана. Шлём
        # рич ради таблиц, поэтому заголовки заранее опускаем до жирной строки.
        text = formatting.markdown_headings_to_bold(chunk) + (quota_md if is_last else "")
        if i == 0:
            # placeholder мог быть уже удалён (черновик его гасит, см.
            # on_draft_start) — тогда правка не пройдёт, и это не повод считать,
            # что rich не поддержан: пробуем тем же ричем отдельным сообщением.
            if not await _edit_rich(placeholder, text, chunk_markup) and not await _answer_rich(
                message, text, chunk_markup
            ):
                return False
            continue
        if not await _answer_rich(message, text, chunk_markup):
            await message.answer(
                formatting.ai_markdown_to_html(chunk) + (quota_html if is_last else ""),
                parse_mode="HTML",
                reply_markup=chunk_markup,
            )
    return True


async def _send_html_answer(
    message: Message, placeholder: Message, chunks: list[str], quota_note: str, markup
) -> None:
    """Тот же ответ обычными сообщениями — путь для всех, у кого нет rich."""
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        chunk_markup = markup if is_last else None
        html_chunk = formatting.ai_markdown_to_html(chunk)
        if is_last:
            html_chunk += quota_note
        # Чанки нарезаны по сырому markdown (~TG_CHUNK), а конверсия в HTML
        # текст удлиняет — таблица, разложенная в строки, разрастается в
        # полтора-два раза, и «влезавший» чанк превышал 4096: Telegram отвергал
        # сообщение целиком, и кусок ответа пропадал уже после списания квоты.
        # Меряем так, как меряет Telegram, и ужимаем с честной пометкой.
        html_chunk = ui.fit_to_limit(html_chunk, ui.TEXT_LIMIT, "HTML")
        if i == 0:
            try:
                await placeholder.edit_text(html_chunk, parse_mode="HTML", reply_markup=chunk_markup)
                continue
            except TelegramBadRequest:
                pass  # разошлось с ротацией (например текст не изменился) — просто шлём отдельным сообщением
        try:
            await message.answer(html_chunk, parse_mode="HTML", reply_markup=chunk_markup)
        except (TelegramBadRequest, TelegramAPIError):
            # Падение одного чанка не должно съедать остальные: ответ уже
            # оплачен квотой, и дыра в середине лучше оборванного хвоста.
            logger.exception("AI answer chunk %s failed to send for user chat %s", i, message.chat.id)


async def _handle_question(
    message: Message,
    state: FSMContext,
    question: str,
    history_question: str,
    image_data_url: Optional[str] = None,
    user_id: Optional[int] = None,
) -> None:
    """Общая логика для текстовых и фото-вопросов: запрос к модели, история, отправка ответа.

    question — то, что реально уходит модели на этот ход (текст +, если есть, фото).
    history_question — облегчённая версия для ai_history/БД: фото туда не попадают
    (не пересылать же их каждый следующий ход), только текст/подпись или заглушка.

    user_id — по умолчанию берётся из message.from_user.id (обычное сообщение
    от пользователя); передаётся явно там, где message — это экран бота, а не
    реплика пользователя (см. ai_build_program: message.from_user там был бы ботом).

    Caller owns the `_busy` reservation end-to-end (claimed atomically before
    any await, released in the caller's `finally`) — this function assumes the
    reservation is already held and never touches `_busy` itself.
    """
    user_id = user_id if user_id is not None else message.from_user.id
    asked_today = await db.get_ai_question_count_today(user_id)
    if asked_today >= config.AI_QUESTION_DAILY_LIMIT:
        # С той же клавиатурой, что у обычных ответов чата: сообщение о лимите
        # становится нижним экраном переписки, и без кнопок из него оставался
        # только выход через нижнее меню.
        await message.reply(DAILY_LIMIT_TEXT, reply_markup=await ai_keyboard(user_id))
        return

    data = await state.get_data()
    history = data.get("ai_history", [])

    # The daily counter is charged only once there's an answer to show for it —
    # a provider outage shouldn't cost the user one of their questions.
    # Пул фраз подбирается по теме вопроса (питание, программа, конкретное
    # упражнение и т.д. — см. running_texts.py), чтобы даже самый первый
    # placeholder до единого tool-call звучал в тему, а не наугад.
    running_pool = running_texts.pool_for(question)
    running_text = running_texts.pick(running_pool)
    placeholder = await message.answer(running_text)
    display = _RunningDisplay(placeholder, running_text, running_pool)
    running_task = asyncio.create_task(display.cycle_idle())

    async def on_draft_start() -> None:
        # The native draft is now live and telling the same story ("тренер
        # печатает..."), so the static "думаю..." bubble above it is a second,
        # stale copy of the same signal — drop it rather than leave both on
        # screen. edit_text on a deleted message just falls back to a fresh
        # send later (see the final-answer loop below), so this is safe.
        running_task.cancel()
        with suppress(asyncio.CancelledError):
            await running_task
        with suppress(TelegramBadRequest):
            await placeholder.delete()

    streamer = _DraftStreamer(message, on_start=on_draft_start)

    # Программа, если тренер собрал её этим ответом (см. propose_program). Держим
    # в ячейке, а не в возврате ask(): текст ответа и черновик — разные вещи, и
    # черновик может прийти в любом раунде tool-calls, в том числе не последнем.
    program_draft: dict = {}

    async def collect_program(draft: dict) -> None:
        program_draft.clear()
        program_draft.update(draft)

    # То же для действий, которые тренер предложил, но не выполнил (удалить
    # программу, объединить две, поделиться, заархивировать упражнение). Их за
    # ход бывает несколько — «почисти дубликаты» это два удаления, — поэтому
    # список, а не ячейка; сколько из них влезет кнопками, решает клавиатура.
    actions: list[dict] = []

    async def collect_action(action: dict) -> None:
        if action not in actions:
            actions.append(action)

    try:
        answer = await ai_trainer.ask(
            user_id, question, history, image_data_url=image_data_url,
            on_status=display.set_status, on_program=collect_program,
            on_action=collect_action, on_chunk=streamer.push,
        )
    except Exception:
        logger.exception("AI trainer request failed for user %s", user_id)
        error_text = "⚠️ Не получилось получить ответ, попробуй ещё раз чуть позже."
        error_kb = await ai_keyboard(user_id)
        try:
            await placeholder.edit_text(error_text, reply_markup=error_kb)
        except TelegramBadRequest:
            # Placeholder уже удалён (черновик его гасит, см. on_draft_start) —
            # значит, обрыв стрима случился ПОСЛЕ начала «печати», и правка
            # молча проваливалась: человек оставался в полной тишине без
            # единого сообщения. Шлём ошибку новым сообщением.
            await message.answer(error_text, reply_markup=error_kb)
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
    show_quota = 0 < left <= _QUOTA_WARN_AT
    quota_html = f"\n\n<i>Осталось вопросов сегодня: {left}</i>" if show_quota else ""
    quota_md = f"\n\n_Осталось вопросов сегодня: {left}_" if show_quota else ""

    history = (
        history
        + [
            {"role": "user", "content": history_question},
            {"role": "assistant", "content": answer},
        ]
    )[-HISTORY_LIMIT:]
    # Черновик один на пользователя: новое предложение затирает старое. Но
    # обычный ход без предложения не должен его стирать (A9) — иначе вопрос
    # вдогонку («сколько отдыхать между подходами?») тушит ещё живую кнопку
    # «✅ Добавить себе» под прошлым ответом, и она отвечает «уже неактуально».
    # Черновик по-прежнему пропадает — но только явно: по ai:prog:save или
    # ai:prog:drop, либо будучи заменённым новым предложением здесь же.
    if program_draft:
        # 5.2: id в callback_data — то, чем кнопка под ЭТИМ ответом отличается
        # от кнопки под более старым. Случайный токен, а не счётчик в FSM:
        # счётчик не входил в _WORKOUT_SCAFFOLD_KEYS и гиб при каждом походе в
        # меню и при state.clear() после тренировки — обнулившись, он выдавал
        # новой программе id уже существующей кнопки, и та молча сохраняла не
        # ту программу, что показывала. Кнопки живут в чате вечно, поэтому id
        # обязан не повторяться никогда; 8 hex-символов спокойно влезают в
        # 64 байта callback_data.
        program_draft["id"] = secrets.token_hex(4)
        await state.update_data(ai_history=history, ai_program_draft=program_draft)
    else:
        await state.update_data(ai_history=history)

    # Предложенные действия живут до следующего предложения: в отличие от
    # черновика программы, «сделать не то» тут нечего — id стоит прямо в
    # callback_data кнопки, а необратимое всё равно проходит через экран
    # подтверждения. Стирать их на каждом ходе без действий нельзя по той же
    # причине, что и черновик: вопрос вдогонку тушил бы ещё живую кнопку под
    # прошлым ответом.
    if actions:
        await state.update_data(ai_actions=actions)

    # Full, permanent log — separate from the live window above, which is capped
    # (and lost on a restart, unlike this). Lets the model pull it back via the
    # get_full_chat_history tool if a later question references it.
    await db.add_ai_chat_message(user_id, "user", history_question)
    await db.add_ai_chat_message(user_id, "assistant", answer)

    reply_markup = await ai_keyboard(
        user_id,
        answer=answer,
        program_name=program_draft.get("name") if program_draft else None,
        draft_id=program_draft.get("id") if program_draft else None,
        actions=actions,
    )
    chunks = formatting.split_for_telegram(answer, TG_CHUNK)
    # Rich — только ради таблицы, и только когда таблица в ответе есть.
    # Rich-сообщение Telegram рисует статьёй: крупные заголовки, широкие
    # отступы, воздух между абзацами. Для разбора с таблицей это уместно, а
    # обычный ответ тренера на три абзаца в такой вёрстке просто раздувается на
    # пол-экрана — при том что ничего, кроме таблицы, обычное сообщение
    # передать и не мешает (заголовок станет жирной строкой, список останется
    # списком).
    sent_rich = formatting.has_markdown_table(answer) and await _send_rich_answer(
        message, placeholder, chunks, quota_md, quota_html, reply_markup
    )
    if not sent_rich:
        await _send_html_answer(message, placeholder, chunks, quota_html, reply_markup)


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
