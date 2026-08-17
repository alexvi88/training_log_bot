"""AI-тренер: чат с Grok, у которого есть доступ к данным текущего пользователя."""

import asyncio
import base64
import datetime as dt
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

import ai_limits
import ai_trainer
import config
import db
import exercise_mentions
import formatting
import keyboards
import program_mentions
import progress_ui
import running_texts
import timeutil
import ui
import video_analysis
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

# Сколько текста должно накопиться, прежде чем черновик вообще покажется.
# Модель успевает выдать вступительную фразу за секунду, а потом уходит думать
# и ходить по инструментам на десятки секунд — и всё это время на экране висел
# один застывший огрызок вместо живого «думаю…». Выглядело как зависший бот, а
# не как работа. Ждём абзаца: до него честнее показывать анимированный
# placeholder, а текст пускать уже потоком.
DRAFT_MIN_CHARS = 280

# Лимит на размер голосового (Telegram сам не режет сильнее, но перестрахуемся).
MAX_VOICE_BYTES = 20 * 1024 * 1024

# Длиннее — явно не короткий вопрос, дороже распознавать и дольше ждать ответ.
MAX_VOICE_SECONDS = 300

INTRO_TEXT = (
    "🤖 <b>ПРИВЕТ АТЛЕТ! ТРЕНЕР НА СВЯЗИ.</b>\n\n"
    "У меня есть доступ к истории твоих тренировок и многолетний тренерский опыт. "
    "Спрашивай что угодно — или начни с готового вопроса на кнопках ниже.\n\n"
    "Пиши вопрос 👇 (можно голосом — жми на 🎤)"
)

# Готовые вопросы на стартовом экране: раньше интро перечисляло примеры текстом,
# и их приходилось перепечатывать руками — тап по кнопке задаёт вопрос сразу.
# Ключ едет в callback_data (ai:preset:<key>), поэтому короткий и стабильный:
# кнопка из старого интро, повисшего в чате, должна находить вопрос и после
# рестарта/релиза. Показываются только на свежем интро (см. menu_ai): в идущем
# разговоре стартовые вопросы читались бы как потеря контекста.
PRESET_QUESTIONS: dict[str, tuple[str, str]] = {
    "progress": (
        "📈 Как мой прогресс?",
        "Как мой прогресс за последнее время? Что растёт, а что застряло — и почему?",
    ),
    "weak": (
        "🔍 Найди мои слабые места",
        "Посмотри мою историю тренировок: что я недорабатываю и что стоит добавить?",
    ),
    "protein": (
        "🍗 Сколько мне есть белка?",
        "Сколько белка мне есть в день, чтобы расти?",
    ),
}


async def intro_presets(user_id: int) -> list[tuple[str, str]]:
    """Кнопки готовых вопросов для интро.

    «Составь мне программу» и «Разбери видео подхода» — не вопросы, а сценарии
    (ai:buildprog / ai:videohint), поэтому живут не в PRESET_QUESTIONS, а имеют
    свои обработчики.

    Про видео кнопка показывается всегда: разбор видео — единственная
    возможность бота, о которой невозможно догадаться самому (в чат не
    написано «пришли ролик»), и про неё стоит напоминать при каждом заходе, а
    не только новичку — дневная квота всё равно ограничивает сами разборы
    (см. ai_limits.KIND_VIDEO), кнопка её не обходит.

    Скрыта она только когда разбор не подключён: обещать кнопкой то, что
    ответит «пока не подключил», — худший вид рекламы.

    «Как мой прогресс?» и «Найди мои слабые места» отвечают на историю
    тренировок — без единой тренировки тренеру разбирать нечего, а кнопка
    без данных за спиной обещает то, что тут же обернётся отговоркой вместо
    ответа.
    """
    has_workouts = await db.count_workouts(user_id) > 0
    rows = [
        (label, f"ai:preset:{key}")
        for key, (label, _) in PRESET_QUESTIONS.items()
        if has_workouts or key not in ("progress", "weak")
    ]
    rows.append(("🗂 Составь мне программу", "ai:buildprog"))
    if config.video_analysis_available():
        rows.append(("🎥 Разбери видео подхода", "ai:videohint"))
    return rows

# Как часто интро показывает, что тренер про человека помнит.
#
# Профиль он пишет сам и без спроса (см. ai_trainer._save_athlete_profile):
# кнопок подтверждения там нет нарочно — они вставали рядом с «🗂 Забрать» и
# отодвигали главное действие. Взамен память показывается вслух: неверная
# строчка живёт максимум до следующего захода, а не годами. Неделя — потому что
# профиль меняется медленно, а на каждом заходе это читалось бы как шум.
MEMORY_REMINDER_DAYS = 7


async def _memory_reminder(user_id: int) -> str:
    """Хвост к интро: что записано в профиле и как это поправить.

    Пусто, если тренер про человека ещё ничего не знает (хвалиться нечем) или
    если напоминание уже показывали на этой неделе.

    Дата последнего показа лежит в базе, а не в FSM: /start чистит состояние
    целиком, кроме трёх AI-ключей (см. state_scaffold.AI_STATE_KEYS), — с
    отметкой в FSM напоминание вылезало бы на каждый тап «🏠 Меню» и обратно.
    """
    # Локальный импорт: экран профиля и этот хвост показывают одно и то же, но
    # тянуть друг друга на уровне модуля двум обработчикам незачем.
    from handlers import settings

    user = await db.get_user(user_id)
    if user is None:
        return ""
    known = [
        f"{label.lower()} — {escape(str(value))}"
        for label, value in settings.profile_rows(user)
        if value
    ]
    if not known:
        return ""
    today = timeutil.user_today(user)
    last = user["profile_shown_on"]
    if last:
        try:
            if (today - dt.date.fromisoformat(str(last))).days < MEMORY_REMINDER_DAYS:
                return ""
        except ValueError:
            pass
    await db.update_user(user_id, profile_shown_on=today.isoformat())
    return (
        "\n\n🧠 <b>Что я про тебя помню:</b> "
        + "; ".join(known)
        + ".\nЧто-то не так — скажи, поправлю."
    )


# Одноразовый хвост к ПЕРВОМУ завершённому ответу тренера: кроме вопросов он
# умеет и действия с данными — записать вес, завести упражнение, посчитать еду.
# Один раз за всю жизнь аккаунта (db.claim_ai_actions_hint) и за $0: чистая
# статика, ни одного вызова модели. Дальше про это нарочно молчим: занос
# данных через модель стоит $0.015–0.05 против $0 через дневник и ест квоту
# вопросов, так что подсказка — для тех, кто сам предпочитает разговорный
# ввод, а не воронка в него. Хвост к уже отправляемому ответу, а не отдельное
# сообщение — тот же приём, что у _memory_reminder, только одноразовый. После
# первого ответа, а не на интро: интро и так несёт напоминание памяти и кнопки
# готовых вопросов, а «просто скажи» читается лучше, когда человек уже увидел,
# что тренер отвечает по его данным, — и не показывается тем, кто до первого
# вопроса так и не дошёл.
ACTIONS_HINT_TEXT = (
    "💡 Кстати, я не только отвечаю: могу сам занести твой вес в дневник веса, "
    "завести упражнение или посчитать еду. Просто скажи."
)


# Shown instead of the full intro when returning to a conversation that's already
# going — repeating the whole "привет, вот что я умею" would read as if the
# trainer had forgotten the last few messages.
RESUME_TEXT = "🤖 <b>ТРЕНЕР НА СВЯЗИ.</b> Продолжаем — пиши вопрос 👇"

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
        # Пока текста меньше абзаца, черновик не показываем вовсе (см.
        # DRAFT_MIN_CHARS): один застывший огрызок читается как поломка. Порог
        # только на ПЕРВЫЙ показ — дальше поток идёт как шёл.
        if not self._started and len(text) < DRAFT_MIN_CHARS:
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
    presets: Sequence[tuple[str, str]] = (),
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
    # Кроме упражнения, которое действие тут же предлагает заархивировать.
    # Иначе под ответом вставали две кнопки подряд с одним и тем же именем —
    # «🗄 В архив: Тестовое упражнение» и «📌 Тестовое упражнение», — и
    # разница между ними («одна архивирует, другая просто открывает карточку»)
    # неочевидна ни разу. Та же логика, что ниже у программ.
    #
    # Откаты (create/rename/move — is_undo) сюда не попадают: «↩️ Убрать
    # «X»» и «📌 X» — это не дубли одного и того же, а отмена и просмотр,
    # и сразу после создания упражнения обе кнопки нужны одновременно.
    if actions:
        acted_on = {a["label"] for a in actions if not a.get("is_undo")}
        mentioned = [
            ex for ex in mentioned
            if not any(ex["display_name"] in label for label in acted_on)
        ]
    # Программы, названные в ответе, — ссылками на них же. Лимит общий с
    # упражнениями: под ответом место одно и то же.
    programs = await program_mentions.find_in_text(user_id, answer)
    # Кроме той, которую этот же ответ предлагает забрать. Иначе под ответом
    # вставали две кнопки подряд с одним и тем же названием — «🗂 Забрать:
    # Верх/низ масса 2x» и «🗂 Верх/низ масса 2x», — и понять, чем они
    # отличаются, было невозможно: вторая вела в старую одноимённую программу,
    # сохранённую в прошлый раз.
    if program_name:
        programs = [p for p in programs if p["display_name"] != program_name]
    return keyboards.ai_trainer_keyboard(
        has_active_workout=bool(active),
        exercises=mentioned,
        programs=programs,
        program_name=program_name,
        draft_id=draft_id,
        actions=actions,
        presets=presets,
    )


@router.callback_query(F.data == "menu:ai")
async def menu_ai(callback: CallbackQuery, state: FSMContext):
    if not ai_trainer.is_configured():
        await callback.answer(
            "AI-тренер пока не подключён — загляни позже.",
            show_alert=True,
        )
        return
    await state.set_state(AITrainerFlow.chatting)
    # The conversation is deliberately NOT cleared here: stepping out to look at
    # a workout and coming back used to reset the trainer to its intro with no
    # memory of what was just discussed.
    data = await state.get_data()
    fresh = not data.get("ai_history")
    text = INTRO_TEXT if fresh else RESUME_TEXT
    # И на интро, и на возврате в разговор. Только на свежем нельзя: ai_history
    # переживает и выход в меню, и перезапуск бота, так что свежее интро человек
    # видит ровно один раз в жизни — а напоминание нужно как раз тем, кто
    # тренером пользуется. Это отдельный экран входа, а не вклинивание в ответ,
    # так что «потери контекста» тут не возникает.
    text += await _memory_reminder(callback.from_user.id)
    # Готовые вопросы — на любом заходе через меню, свежем или нет: это
    # отдельный экран входа, а не вклинивание в ответ посреди разговора, так
    # что «тренер забыл, о чём речь» тут не читается — человек сам вернулся на
    # этот экран, а не получил его посреди чужого ответа.
    keyboard = await ai_keyboard(
        callback.from_user.id,
        presets=await intro_presets(callback.from_user.id),
    )
    await ui.safe_edit(callback, text, reply_markup=keyboard, parse_mode="HTML")
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
    # Про «вопросы бесплатные, дневной лимит они не едят» тут было сказано зря:
    # человек, который просто хочет программу, до этой фразы про лимит не думал
    # вовсе — а прочитав, начинал. Опросник и правда не тратит лимит (между
    # шагами модель не вызывается), но говорить об этом на входе значит
    # напоминать о счётчике там, где его никто не считал.
    "Сейчас задам пару вопросов — отвечай, и заберёшь готовый план."
)

# Уходит тренеру от лица пользователя, чтобы не заставлять его печатать этот же
# запрос вручную (см. ai_build_program). Про вопросы сказано явно: системный
# промпт разрешает тренеру собрать программу сразу на дефолтах, если человек
# отмахивается, а вся суть этой кнопки — наоборот, провести через уточнения.
BUILD_PROGRAM_SEED = (
    "Хочу собрать программу тренировок. Сначала задай мне уточняющие вопросы "
    "по вводным, которых не видно из моей истории, — через ask_setup_questions, "
    "чтобы они пришли по одному, — и только после моих ответов собирай программу."
)

# Чек-лист для progress_ui.run_progress на самом долгом вызове сценария
# «Составить программу» — том, что уходит СРАЗУ после ответов на опросник (см.
# _finish_setup) и реально дёргает propose_program: опросник сам по себе не
# трогает модель и идёт мгновенно (see test_ai_setup_questions.py), а вот этот
# ход может пройти по истории тренировок, каталогу упражнений и прогрессии —
# десятки секунд. Тексты без канцелярита и от первого лица — как и весь
# TONE_OF_VOICE.md; многоточие на активном этапе дорисовывает сам render().
#
# Этапов семь, а не четыре, и все — в настоящем времени. Четыре галочки при
# прежней скорости кривой проставлялись за первые шесть секунд, после чего
# экран замирал на «Проверяю всё в последний раз — 93%» на полминуты: движение
# кончалось ровно тогда, когда ждать оставалось дольше всего. Чем мельче шаг,
# тем ровнее идёт чек-лист — а темп задаёт progress_ui.GROWTH_TAU, там же
# посчитано, на какой секунде встаёт каждая галочка.
PROGRAM_PROGRESS_STAGES = [
    "Читаю твою историю тренировок",
    "Смотрю недельный объём по группам",
    "Прикидываю сплит по дням",
    "Подбираю упражнения под задачу",
    "Раскидываю их по дням",
    "Расставляю подходы и повторы",
    "Проверяю всё в последний раз",
]

BUILD_WORKOUT_INTRO = (
    "🤖 <b>СОБИРАЕМ ТРЕНИРОВКУ НА СЕГОДНЯ.</b>\n\n"
    "Гляну, что у тебя отдохнуло, и спрошу пару вещей — а дальше по ней и пойдём."
)

# Отдельный сценарий, а не «программа из одного дня»: человек стоит в зале и
# хочет знать, что делать сегодня, а не заводить себе ещё одну программу
# навсегда. Отсюда три отличия от BUILD_PROGRAM_SEED — прямое указание сходить
# за восстановлением (тренер про него не знал вовсе, пока не появился
# get_muscle_recovery), просьба ограничиться одним днём и явное «коротко»:
# уточнять инвентарь и цели на полразговора, когда человек уже разминается,
# — худшее, что тут можно сделать.
BUILD_WORKOUT_SEED = (
    "Собери мне тренировку на сегодня — одну, прямо сейчас пойду по ней "
    "заниматься. Сначала посмотри get_muscle_recovery и реши, что сегодня "
    "логичнее грузить, а что ещё не отдохнуло. Потом задай мне через "
    "ask_setup_questions не больше двух коротких вопросов (сколько есть времени "
    "и как самочувствие) и только после ответов вызывай propose_program ровно "
    "с одним днём."
)


async def _start_ai_scenario(
    callback: CallbackQuery,
    state: FSMContext,
    intro: str,
    seed: str,
    keep_message: bool = False,
    scenario: Optional[str] = None,
) -> None:
    """Кнопка, которая сама начинает разговор с тренером за пользователя.

    Общая половина «Составить программу» и «Тренировка на сегодня»: обе не
    просто открывают чат, а сразу задают тренеру нужный вопрос — иначе, чтобы
    получить план, надо было самому догадаться попросить об этом словами.

    `keep_message` — не съедать сообщение, на котором стояла кнопка. По
    умолчанию сценарий встаёт на его место: кнопка живёт на экране меню, а
    экран — расходник. Но та же кнопка стоит и под релизной рассылкой
    (announcements.py), а рассылка — не экран: человек тапнул «собрать
    программу», и анонс вместе со второй кнопкой, про разбор видео, исчезал из
    чата навсегда.

    Дневной лимит проверяется ДО подмены экрана: раньше бодрое «ОКЕЙ, СОБИРАЕМ»
    успевало встать на место меню, и уже под ним приезжал отказ — человек терял
    экран, с которого пришёл, ради обещания, которое бот тут же забирал назад.

    `scenario` едет в FSM вместе с опросником (см. _deliver_setup) и доживает
    до _finish_setup — там по нему решают, показывать ли анимированный
    progress_ui вместо голого «тренер думает» на самом долгом вызове сценария.
    None — как у «Тренировка на сегодня»: тот вызов сам по себе короче, а
    отдельный чек-лист под него ещё не расписан.
    """
    if not ai_trainer.is_configured():
        await callback.answer(
            "AI-тренер пока не подключён — загляни позже.",
            show_alert=True,
        )
        return
    user_id = callback.from_user.id
    block = await ai_limits.check(user_id, ai_limits.KIND_QUESTION)
    if block is not None:
        logger.info("AI program builder blocked for user %s: %s", user_id, block.log)
        if block.preview:
            # Предупреждение своим — сообщением, а не алертом: в алерт кнопку
            # «Понятно» не положишь. Сам вопрос при этом всё равно уходит —
            # preview показывает, что увидел бы обычный атлет, а не отменяет
            # действие, которое его вызвало (раньше «Понятно» снимало лимит
            # только на будущее, а текущий запрос приходилось повторять).
            await ai_limits.reply(callback.message, block)
            await callback.answer()
        else:
            await callback.answer(
                f"{ai_limits.QUESTION_LIMIT_TEXT} А пока забери готовую в «✨ Готовые программы».",
                show_alert=True,
            )
            return
    if not _try_claim_busy(user_id):
        await callback.answer("Секунду, ещё думаю над прошлым вопросом 😅", show_alert=True)
        return
    try:
        # Внутри try: тап по устаревшей кнопке Telegram отвергает с «query is
        # too old», и без этого finally не отработал бы — бронь протекла бы
        # навсегда, а каждое следующее сообщение пользователя отвечало бы
        # «ещё думаю» до конца жизни процесса (A5).
        with suppress(TelegramBadRequest):
            await callback.answer()
        await state.set_state(AITrainerFlow.chatting)
        # Новая просьба — новый опросник: недоотвеченные вопросы прошлого
        # захода и его счётчик кругов (см. SETUP_MAX_ROUNDS) к ней отношения не
        # имеют, а доживший счётчик упёр бы её в потолок с первого вопроса.
        await state.update_data(ai_setup=None)
        # С клавиатурой, а не голым текстом: пока тренер думает, это единственный
        # экран на месте меню — а ответа может и не быть вовсе (сбой провайдера),
        # и без кнопок выход остался бы только через нижнее меню.
        screen = await ui.safe_edit(
            callback, intro, reply_markup=await ai_keyboard(user_id), parse_mode="HTML",
            delete=not keep_message,
        )
        await _handle_question(
            screen, state, seed, history_question=seed, user_id=user_id, scenario=scenario,
        )
    finally:
        _busy.discard(user_id)


@router.callback_query(F.data == "ai:buildprog")
async def ai_build_program(callback: CallbackQuery, state: FSMContext):
    """«Составить с AI-тренером» в 🗂 Программы — многодневка на будущее."""
    await _start_ai_scenario(callback, state, BUILD_PROGRAM_INTRO, BUILD_PROGRAM_SEED, scenario="program")


@router.callback_query(F.data == "ann:buildprog")
async def announcement_build_program(callback: CallbackQuery, state: FSMContext):
    """То же самое, но из релизной рассылки (announcements.py).

    Отдельный callback, а не флаг в существующем: под рассылкой кнопка обязана
    оставлять сообщение на месте, под экраном программ — обязана его заменять,
    и решает это не пользователь, а то, откуда кнопка приехала.
    """
    await _start_ai_scenario(
        callback, state, BUILD_PROGRAM_INTRO, BUILD_PROGRAM_SEED, keep_message=True, scenario="program"
    )


@router.callback_query(F.data == "ai:buildworkout")
async def ai_build_workout(callback: CallbackQuery, state: FSMContext):
    """«🤖 Собрать тренировку на сегодня» на экране начала тренировки.

    Не то же самое, что собрать программу: программа — это план на недели
    вперёд, который ещё надо сохранить, а тут человек уже в зале и ему нужно
    знать, что делать в ближайший час. Поэтому и сценарий другой (одна тренировка,
    с оглядкой на восстановление, минимум вопросов), и из превью такого
    однодневного черновика можно уйти сразу в тренировку, не сохраняя его
    себе программой — см. keyboards.ai_program_preview_keyboard.
    """
    await _start_ai_scenario(callback, state, BUILD_WORKOUT_INTRO, BUILD_WORKOUT_SEED)


@router.callback_query(F.data.startswith("ai:preset:"))
async def ai_preset_question(callback: CallbackQuery, state: FSMContext):
    """Готовый вопрос со стартового экрана AI-тренера (см. PRESET_QUESTIONS).

    Тот же путь, что у «Составить программу»: интро цитирует вопрос, чтобы в
    чате осталось, о чём вообще спрашивали, — сам вопрос от лица пользователя
    в чат не попадает, и без цитаты ответ висел бы без контекста.
    """
    preset = PRESET_QUESTIONS.get(callback.data.split(":", 2)[2])
    if preset is None:
        # Кнопка из интро прошлой версии, где этот вопрос ещё существовал.
        await callback.answer("Такого вопроса больше нет — напиши его словами 👇", show_alert=True)
        return
    _, question = preset
    intro = f"🤖 <b>ПРИНЯЛ ВОПРОС.</b>\n\n«{question}»"
    await _start_ai_scenario(callback, state, intro, question)


# Кнопка под AI-разбором истории сразу после импорта (см. handlers.csv_import.
# _attach_import_overview) — не в PRESET_QUESTIONS: та живёт только на интро,
# а эта — под конкретным сообщением с разбором, и на обычном интро дублировать
# её незачем.
IMPORT_CTA_QUESTION = "Я стал сильнее за этот год, судя по моей истории?"


@router.callback_query(F.data == "ai:import_cta")
async def ai_import_cta(callback: CallbackQuery, state: FSMContext):
    """Тот же путь, что у готовых вопросов интро (ai_preset_question) — превращает
    разбор истории после импорта из монолога в начало разговора: тап сразу
    входит в чат с тренером и задаёт вопрос про эту же историю, той же
    механикой (лимиты, busy-бронь, история диалога — всё как у обычного вопроса)."""
    intro = f"🤖 <b>ПРИНЯЛ ВОПРОС.</b>\n\n«{IMPORT_CTA_QUESTION}»"
    await _start_ai_scenario(callback, state, intro, IMPORT_CTA_QUESTION)


VIDEO_HINT_TEXT = (
    "🎥 <b>ДАВАЙ ПОСМОТРЮ.</b>\n\n"
    "Пришли видео подхода прямо сюда — гляну технику и скажу, что править первым.\n\n"
    "Как снять, чтобы я реально что-то увидел:\n"
    # Про гриф и колени тут было зря: половина роликов — это тяга блока, махи
    # или подтягивания, и человек читал инструкцию, написанную будто только под
    # становую. Ракурс важен всегда, а вот что именно на нём видно — зависит от
    # упражнения, поэтому и говорим общими словами.
    "• сбоку, а не сзади — так видно траекторию и спину\n"
    "• подход целиком: от первого повтора до последнего\n"
    f"• до {config.MAX_VIDEO_SECONDS} секунд, один подход\n\n"
    "Звук не нужен, я смотрю только картинку."
)


@router.callback_query(F.data == "ai:videohint")
async def ai_video_hint(callback: CallbackQuery, state: FSMContext):
    """Кнопка «Разбери видео подхода» — подсказка, а не вопрос к модели.

    Прислать ролик за человека нельзя, поэтому кнопка объясняет, как снять, и на
    этом заканчивается: ни одного вызова модели, ни рубля, ни списанной квоты.
    Заодно тут единственное место, где можно сказать про ракурс до съёмки, — на
    разборе «сними сбоку» человек читает уже потратив попытку.
    """
    if not config.video_analysis_available():
        await callback.answer("Разбор видео пока не подключён — загляни позже.", show_alert=True)
        return
    await state.set_state(AITrainerFlow.chatting)
    await callback.message.answer(
        VIDEO_HINT_TEXT,
        reply_markup=await ai_keyboard(callback.from_user.id),
        parse_mode="HTML",
    )
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


@router.callback_query(F.data.startswith("ai:exarchaskmulti:"))
async def ai_exercise_archive_confirm_multi(callback: CallbackQuery, state: FSMContext):
    """«Точно всё в архив?» для пачки сразу (ai_trainer._archive_exercises).

    Имена перечисляются поимённо, а не только числом — массовое действие не
    должно прятать, что именно уйдёт в архив, за одной цифрой в кнопке.
    """
    key = callback.data.split(":", 2)[2]
    ids = (await state.get_data()).get("ai_archive", {}).get(key)
    if not ids:
        await callback.answer("Список устарел, спроси тренера ещё раз", show_alert=True)
        return
    exercises = [
        ex for ex in [await db.get_exercise(ex_id) for ex_id in ids]
        if ex is not None and ex["user_id"] == callback.from_user.id and not ex["is_template"]
    ]
    if not exercises:
        await callback.answer("Упражнения не найдены", show_alert=True)
        return
    names = "\n".join(f"• {escape(ex['display_name'])}" for ex in exercises)
    await callback.message.answer(
        f"Убрать из списка упражнений ({len(exercises)}) — история и рекорды останутся, "
        f"вернуть можно в ⚙️ Упражнения → 🗄 Архив:\n\n{names}",
        reply_markup=keyboards.yes_no_keyboard(
            yes_cb=f"ai:exarchyesmulti:{key}", no_cb="ai:menu",
            yes_text=f"🗄 В архив всё ({len(exercises)})", no_text="❌ Отмена",
        ),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ai:exarchyesmulti:"))
async def ai_exercise_archive_multi(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 2)[2]
    ids = (await state.get_data()).get("ai_archive", {}).get(key) or []
    archived = []
    for ex_id in ids:
        ex = await db.get_exercise(ex_id)
        if ex is None or ex["user_id"] != callback.from_user.id or ex["is_template"]:
            continue
        if ex["is_archived"]:
            continue
        await db.archive_exercise(ex_id)
        archived.append(ex["display_name"])
    await callback.answer(f"В архиве: {len(archived)}" if archived else "Уже в архиве")
    text = (
        "🗄 В архиве:\n" + "\n".join(f"• {escape(n)}" for n in archived)
        if archived else "Это всё уже было в архиве."
    )
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            text,
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


# ---------- откат того, что тренер сделал сам ----------

# Сколько откатов держим живыми одновременно. Кнопки под старыми ответами
# остаются тапабельными сколько угодно долго, но хранить их описания вечно
# незачем: FSM едет на диск целиком при каждой записи. Восемь — это заметно
# больше, чем успевает накопиться за один разговор, и достаточно мало, чтобы
# не раздувать файл состояния.
_UNDO_SLOTS = 8


def _fold_undo_actions(
    out: list[dict], store: dict, seq: int
) -> tuple[list[dict], dict, int]:
    """Несколько откатов за один ход — одной кнопкой на всё.

    Просьба «удали всё из дневника еды» превращается в двадцать с лишним
    вызовов, у каждого свой откат. Под ответом помещается keyboards.MAX_AI_ACTIONS
    кнопок, то есть первые три: остальные восемнадцать записей вернуть было
    нечем, а тренер при этом писал «отменишь кнопками отката ниже» — обещание,
    которого экран не выполнял. Три кнопки с чужими названиями («Вернуть
    «каша»») ещё и не давали понять, что вообще откатывается.

    Поэтому откаты одного хода складываются в один: тап возвращает всё, что
    этот ход сделал, в обратном порядке. Кнопка встаёт на место первого отката —
    выше действий, которые тренер только предложил.
    """
    undo_slots = [i for i, a in enumerate(out) if a.get("is_undo")]
    if len(undo_slots) < 2:
        return out, store, seq
    keys = [out[i]["callback"].split(":", 2)[2] for i in undo_slots]
    items = [store.pop(k) for k in keys]
    seq += 1
    key = f"u{seq}"
    store[key] = {"kind": "batch", "items": items}
    word = formatting.plural_ru(len(items), ("изменение", "изменения", "изменений"))
    folded = {
        "label": f"↩️ Отменить всё — {len(items)} {word}",
        "callback": f"ai:undo:{key}",
        "is_undo": True,
    }
    rest = [a for i, a in enumerate(out) if i not in set(undo_slots)]
    return rest[: undo_slots[0]] + [folded] + rest[undo_slots[0] :], store, seq


async def _register_actions(state: FSMContext, actions: list[dict]) -> list[dict]:
    """Описания откатов — в FSM, а в кнопку — короткий ключ на них.

    В callback_data 64 байта, а откату нужно то, что туда не влезает: старое
    имя программы (до 48 символов, а в UTF-8 это под сотню байт) или целый
    прежний срез профиля. Поэтому в кнопке едет только ключ.

    Ключ намеренно не числовой («u7», не «7»): состояние FSM лежит в JSON, а
    тот стрингифицирует ключи словарей — fsm_storage._restore_int_keys при
    загрузке честно превращает «7» обратно в int 7, и после перезапуска бота
    поиск по строковому ключу из callback_data не нашёл бы ничего.

    Так же и письмо разработчику (см. ai_trainer.send_feedback_to_admin): в
    кнопке ключ, а сам текст — в FSM, потому что в callback_data он не влез бы
    даже в обрезанном виде.

    Действия, у которых нет ни того, ни другого (они ждут подтверждения по
    готовой callback_data — см. ai_trainer._ACTION_TOOLS), проходят насквозь
    нетронутыми.
    """
    data = await state.get_data()
    store = dict(data.get("ai_undo") or {})
    seq = int(data.get("ai_undo_seq") or 0)
    feedback_store = dict(data.get("ai_feedback") or {})
    feedback_seq = int(data.get("ai_feedback_seq") or 0)
    archive_store = dict(data.get("ai_archive") or {})
    archive_seq = int(data.get("ai_archive_seq") or 0)

    out: list[dict] = []
    for action in actions:
        undo = action.get("undo")
        feedback = action.get("feedback")
        archive_ids = action.get("archive_ids")
        if undo is not None:
            seq += 1
            key = f"u{seq}"
            store[key] = undo
            # is_undo отличает «откатить уже сделанное» от «предложил, но не
            # сделал» — см. ai_keyboard: только вторые исключают дублирующую
            # ссылку 📌 на то же упражнение, у первых отмена и просмотр карточки
            # это две разные полезные вещи, а не одна и та же под двумя ярлыками.
            out.append({"label": action["label"], "callback": f"ai:undo:{key}", "is_undo": True})
        elif feedback is not None:
            feedback_seq += 1
            key = f"f{feedback_seq}"
            feedback_store[key] = feedback
            out.append({"label": action["label"], "callback": f"ai:fb:{key}"})
        elif archive_ids is not None:
            # Список id упражнений на массовую архивацию (ai_trainer._archive_exercises)
            # — как и с фидбеком, в 64 байта callback_data влезает не список
            # (два десятка id это уже под сотню байт), а короткий ключ на него.
            archive_seq += 1
            key = f"a{archive_seq}"
            archive_store[key] = archive_ids
            out.append({"label": action["label"], "callback": f"ai:exarchaskmulti:{key}"})
        else:
            out.append(action)

    out, store, seq = _fold_undo_actions(out, store, seq)

    for stale in sorted(store, key=lambda k: int(k[1:]))[:-_UNDO_SLOTS]:
        del store[stale]
    for stale in sorted(feedback_store, key=lambda k: int(k[1:]))[:-_UNDO_SLOTS]:
        del feedback_store[stale]
    for stale in sorted(archive_store, key=lambda k: int(k[1:]))[:-_UNDO_SLOTS]:
        del archive_store[stale]
    await state.update_data(
        ai_undo=store, ai_undo_seq=seq,
        ai_feedback=feedback_store, ai_feedback_seq=feedback_seq,
        ai_archive=archive_store, ai_archive_seq=archive_seq,
    )
    return out


async def _apply_undo(user_id: int, undo: dict) -> Optional[str]:
    """Вернуть как было. Возвращает текст для пользователя либо None, если не вышло.

    Владельца проверяем на каждом шаге заново: между записью и тапом по кнопке
    проходит сколько угодно времени, а id приезжает из FSM, куда его положил
    прошлый ход — но сама строка в базе к этому моменту могла и смениться.
    """
    kind = undo.get("kind")

    if kind == "batch":
        # В обратном порядке: ход мог сначала создать упражнение, а потом
        # записать в него подход — снимать надо с конца, иначе откат упрётся
        # в то, что ещё на нём висит.
        items = list(undo.get("items") or [])
        done = 0
        for item in reversed(items):
            if await _apply_undo(user_id, item) is not None:
                done += 1
        if done == 0:
            return None
        word = formatting.plural_ru(done, ("изменение", "изменения", "изменений"))
        if done < len(items):
            # Часть уже не откатывалась — молчать об этом нельзя: человек
            # решит, что вернулось всё.
            return f"Вернул {done} {word} из {len(items)} — остальное уже правили руками"
        return f"Вернул {done} {word}"

    if kind == "bodyweight":
        if await db.delete_bodyweight_log(int(undo["id"]), user_id):
            return "Убрал запись веса"
        return None

    if kind == "food":
        entry = await db.get_food_entry(int(undo["id"]))
        if entry is None or entry["telegram_id"] != user_id:
            return None
        await db.delete_food_entry(entry["id"])
        return "Убрал из дневника еды"

    if kind == "food_restore":
        # Откат delete_food_entry: не «отменить запись», а «отменить удаление» —
        # воссоздаём строку с теми же полями, что были у стёртой.
        await db.add_food_entry(
            user_id, undo["eaten_on"], undo["description"],
            details=undo.get("details"), calories=undo.get("calories"),
            protein=undo.get("protein"), fat=undo.get("fat"), carbs=undo.get("carbs"),
            photo_file_id=undo.get("photo_file_id"), source=undo.get("source") or "text",
        )
        return f"Вернул «{undo['description']}» в дневник"

    if kind == "bodyweight_restore":
        await db.add_bodyweight_log(user_id, undo["weight"], logged_at=undo.get("logged_at"))
        return f"Вернул запись веса {undo['weight']:g}"

    if kind == "exercise_new":
        if await db.delete_exercise_if_unused(int(undo["id"]), user_id):
            return f"Убрал «{undo.get('name', '')}»".replace("«»", "упражнение")
        # По нему уже успели что-то записать — сносить нельзя, чужие данные
        # уедут вместе с ним. Честнее сказать, чем сделать вид, что откатили.
        return None

    if kind == "exercise_name":
        exercise = await db.get_exercise(int(undo["id"]))
        if exercise is None or exercise["user_id"] != user_id:
            return None
        if not await db.update_exercise_name(exercise["id"], undo["name"]):
            return None
        return f"Вернул имя «{undo['name']}»"

    if kind == "exercise_group":
        exercise = await db.get_exercise(int(undo["id"]))
        if exercise is None or exercise["user_id"] != user_id:
            return None
        await db.update_exercise_group(exercise["id"], int(undo["group_id"]))
        return f"Вернул в «{undo['name']}»" if undo.get("name") else "Вернул группу"

    if kind == "program_name":
        program = await db.get_program(int(undo["id"]))
        if program is None or program["user_id"] != user_id:
            return None
        if not await db.rename_program_by_id(program["id"], undo["name"]):
            return None
        return f"Вернул имя «{undo['name']}»"

    if kind == "routine_name":
        routine = await db.get_routine(int(undo["id"]))
        if routine is None or routine["user_id"] != user_id:
            return None
        await db.rename_routine(routine["id"], undo["name"])
        return f"Вернул имя «{undo['name']}»"

    if kind == "program_new":
        program = await db.get_program(int(undo["id"]))
        if program is None or program["user_id"] != user_id:
            return None
        await db.delete_program_by_id(program["id"])
        return f"Убрал копию «{undo.get('name') or program['name']}»"

    if kind == "profile":
        before = undo.get("before") or {}
        fields = {k: v for k, v in before.items() if k in ai_trainer.PROFILE_FIELDS}
        if not fields:
            return None
        await db.update_user(user_id, **fields)
        names = ", ".join(ai_trainer.PROFILE_LABELS.get(k, k) for k in fields)
        return f"Вернул как было: {names}"

    return None


@router.callback_query(F.data.startswith("ai:undo:"))
async def ai_undo(callback: CallbackQuery, state: FSMContext):
    """«↩️ Отменить» под ответом тренера.

    Ключ забираем из хранилища ДО самого отката и сразу пишем состояние: между
    чтением и записью нет ни одного await, реально отдающего управление циклу,
    так что второй тап по той же кнопке не найдёт ключа и не снесёт заодно
    запись, которую человек успел сделать после.
    """
    key = callback.data.split(":", 2)[2]
    data = await state.get_data()
    store = dict(data.get("ai_undo") or {})
    undo = store.pop(key, None)
    if undo is None:
        await callback.answer(
            "Это уже отменено — или кнопка от слишком старого ответа.", show_alert=True
        )
        return
    await state.update_data(ai_undo=store)

    done = await _apply_undo(callback.from_user.id, undo)
    if done is None:
        await callback.answer(
            "Откатить не вышло: с тех пор это успели поменять или удалить вручную.",
            show_alert=True,
        )
        return
    await callback.answer(done)
    await _drop_used_button(callback, state, data)


async def _drop_used_button(callback: CallbackQuery, state: FSMContext, data: dict) -> None:
    """Кнопка отработала — убираем её, чтобы она не выглядела всё ещё живой.

    Остальные кнопки под этим ответом (ссылки на упражнения, другие откаты)
    остаются: ответ тренера никуда не делся, по нему ещё ходят.
    """
    actions = [a for a in (data.get("ai_actions") or []) if a.get("callback") != callback.data]
    await state.update_data(ai_actions=actions)
    rows = (callback.message.reply_markup.inline_keyboard if callback.message.reply_markup else [])
    kept = [[b for b in row if b.callback_data != callback.data] for row in rows]
    kept = [row for row in kept if row]
    with suppress(TelegramBadRequest):
        await callback.message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kept) if kept else None
        )


@router.callback_query(F.data.startswith("ai:fb:"))
async def ai_send_feedback(callback: CallbackQuery, state: FSMContext):
    """«📬 Передать разработчику» под ответом тренера.

    Письмо собрал тренер (ai_trainer.send_feedback_to_admin), а отправляет его
    тап человека — как и всё остальное, что уходит наружу. Маршрут тот же, что у
    команды /feedback, только приходит уже разобранным: что человек сказал и что
    тренер из него вытянул.

    Ключ из хранилища забираем ПОСЛЕ удачной отправки: сеть тут отвечает не
    всегда, и вычеркнутое до отправки письмо человек уже ничем бы не повторил.
    Двойной тап поэтому теоретически может уехать дважды — дубль в чате админа
    дешевле потерянного отзыва.
    """
    key = callback.data.split(":", 2)[2]
    data = await state.get_data()
    store = dict(data.get("ai_feedback") or {})
    letter = store.get(key)
    if letter is None:
        await callback.answer(
            "Это уже передал — или кнопка от слишком старого ответа.", show_alert=True
        )
        return
    if config.ADMIN_ID is None:
        await callback.answer(
            "Сейчас передать не выйдет. Попробуй ещё раз через /feedback.",
            show_alert=True,
        )
        return

    who = f"@{callback.from_user.username}" if callback.from_user.username else str(callback.from_user.id)
    header = (
        f"📬 {letter.get('label', 'Фидбек')} от {who} (id {callback.from_user.id}), "
        "через AI-тренера:"
    )
    try:
        await callback.bot.send_message(
            config.ADMIN_ID, f"{header}\n\n{escape(letter['text'])}", parse_mode="HTML"
        )
    except TelegramAPIError:
        logger.exception("feedback relay failed for user %s", callback.from_user.id)
        await callback.answer(
            "Не дошло. Нажми ещё раз или напиши /feedback.", show_alert=True
        )
        return

    del store[key]
    await state.update_data(ai_feedback=store)
    await callback.answer("Передал 🙌 Спасибо!")
    await _drop_used_button(callback, state, data)


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
    # Единицу передаём: без неё превью писало «прибавь 2.5 к весу» вместо
    # «2.5кг» — а у lb-пользователя выдуманные килограммы были бы враньём.
    user = await db.get_user(callback.from_user.id)
    text = formatting.build_ai_program_preview(
        draft["name"], draft["days"], replaces=replaces, notes=draft.get("notes"),
        unit=user["unit"] if user else None,
    )
    await callback.message.answer(
        text,
        parse_mode="HTML",
        reply_markup=keyboards.ai_program_preview_keyboard(
            replacing=bool(replaces),
            draft_id=draft_id,
            # Один день — это тренировка, а не программа: по ней логично пойти
            # прямо сейчас, ничего себе не заводя (см. ai_program_train).
            can_train_now=len(draft["days"]) == 1,
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ai:prog:train:"))
async def ai_program_train(callback: CallbackQuery, state: FSMContext):
    """«▶️ Начать по ней» — тренировка по собранному плану, без сохранения.

    Единственной дорогой от плана к штанге было «Добавить себе»: чтобы пойти по
    сгенерённому, приходилось сначала завести себе программу навсегда, и разовая
    «тренька на сегодня» оседала в 🗂 Программы рядом с настоящими. Сохранять её
    и не нужно: сессия попадёт в историю, а «🔁 Повторить тренировку» на экране
    старта умеет перезапустить любую прошлую.

    Активную тренировку не трогаем, а до-планируем: на этот экран приходят с
    экрана выбора, который активную тренировку уже создал (menu:start_workout),
    так что она тут почти всегда есть и почти всегда пустая. Уже отработанные
    в ней упражнения из плана вычитаются — тем же приёмом, что и в
    workout._rebuild_planned_blocks_from_routine.

    Упражнения тут форкаются из каталога — в отличие от самого предложения,
    которое не пишет ничего (ai_trainer._propose_program). Это осознанно: пойти
    заниматься по плану и есть согласие им пользоваться.

    У такой тренировки нет routine_id, и это её единственный минус: если
    состояние FSM потеряется на середине, восстановить план будет неоткуда —
    workout._rebuild_planned_blocks_from_routine опирается как раз на него.
    Заводить ради страховки скрытую программу значило бы вернуть ровно тот
    мусор в списке, из-за которого всё и затевалось.
    """
    from handlers.workout import (
        _delete_message,
        _reset_new_workout_scaffold,
        start_planned_workout,
    )

    draft_id = _draft_id_from(callback.data)
    draft = await _program_draft(callback, state, draft_id)
    if draft is None:
        return
    day = draft["days"][0]

    user_id = callback.from_user.id
    workout_id, created = await db.get_or_create_active_workout(user_id)
    done_ids = set() if created else set(await db.list_exercise_ids_for_workout(workout_id))

    planned = []
    for item in day["items"]:
        ex_id = await db.get_or_create_user_exercise_by_name(user_id, item["name"])
        if ex_id is None or ex_id in done_ids:
            continue
        done_ids.add(ex_id)
        planned.append({"exercise_ids": [ex_id], "targets": {ex_id: item.get("target")}})

    if not planned:
        await callback.answer(
            "Всё из этого плана ты уже сделал 💪 Добери что-нибудь сам или "
            "попроси меня собрать ещё.",
            show_alert=True,
        )
        return

    # Черновик израсходован: он больше не «предложение, которое ждёт решения».
    # Кнопка «Добавить себе» под этим же превью после старта ответит, что
    # предложение неактуально, — и это правда, по нему уже занимаются.
    await state.update_data(ai_program_draft=None)

    if created:
        await _reset_new_workout_scaffold(state)
    await _delete_message(callback.message)
    sent = await callback.message.answer(f"🏋️ Тренировка: {escape(day['name'])}")
    await state.update_data(
        workout_id=workout_id, live_chat_id=sent.chat.id, live_message_id=sent.message_id,
        last_by_exercise={}, planned_blocks=planned,
    )
    await start_planned_workout(callback, state)
    await callback.answer("Погнали 💪")


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
    # Правка меняет и описание — но только если тренер его прислал: пустое поле
    # в новом предложении значит «не сказал», а не «сотри то, что было».
    if draft.get("description"):
        await db.set_program_description(program["id"], draft["description"])

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

    program_id = await db.create_program(
        user_id, draft["name"], source="ai", description=draft.get("description")
    )
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
    "⚠️ Не смог сохранить программу — ничего не записал.\n"
    "Предложение тренера живо: нажми кнопку ещё раз."
)
_SAVE_FAILED_REPLACE = (
    "⚠️ Не смог обновить программу: часть новых дней могла успеть добавиться "
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
    """«Не надо»: закрываем превью. Черновик при этом ОСТАЁТСЯ.

    Новые превью этой кнопки уже не показывают (см.
    keyboards.ai_program_preview_keyboard), но она висит под всеми прошлыми и
    остаётся тапабельной вечно — поэтому обработчик живёт.

    Раньше он заодно стирал черновик, и это ломало ровно то, ради чего человек
    сюда и шёл: посмотрел превью, передумал сохранять сейчас, вернулся к ответу
    тренера — а кнопка «🗂 Забрать: <программа>» под ним отвечает «это
    предложение уже неактуально». Ничего неактуального не случилось: он просто
    закрыл экран."""
    with suppress(TelegramBadRequest):
        await callback.message.delete()
    await callback.answer("Закрыл")


@router.callback_query(F.data.startswith("ai:comment:"))
async def ai_comment_workout(callback: CallbackQuery, state: FSMContext):
    """Ручной запрос комментария к тренировке — кнопка на карточке завершённой тренировки.

    Работает и на свежезавершённой карточке, и на карточке из истории: правит то же
    сообщение на месте, убирая из клавиатуры только саму эту кнопку.

    Когда комментарий ещё не сгенерирован, тап — платный вызов
    (ai_trainer.comment_on_workout), и его накрывают те же замки, что и у любого
    другого AI-входа: `_try_claim_busy` от двойного тапа (без него два быстрых
    тапа уходят в модель оба) и `ai_limits.hard_stop_block` от HARD-стопа по
    деньгам (личной квоты под эту кнопку нет и заводить новую ради одной кнопки
    не стоит — см. её докстринг). Уже сохранённый комментарий ничего не стоит и
    оба замка не трогает.
    """
    workout_id = int(callback.data.split(":")[2])
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != callback.from_user.id:
        await callback.answer("Тренировка не найдена", show_alert=True)
        return
    if not ai_trainer.is_configured():
        await callback.answer("AI-тренер не настроен.", show_alert=True)
        return

    user_id = callback.from_user.id
    comment = workout["ai_comment"]
    if not comment:
        if not _try_claim_busy(user_id):
            await callback.answer("Секунду, ещё думаю над прошлым вопросом 😅", show_alert=True)
            return
        try:
            block = await ai_limits.hard_stop_block()
            if block is not None:
                logger.info("AI workout comment blocked for user %s: %s", user_id, block.log)
                await callback.answer()
                await ai_limits.reply(callback.message, block, reply_markup=await ai_keyboard(user_id))
                return
            await callback.answer()
            try:
                comment = await ai_trainer.comment_on_workout(user_id, workout_id)
            except Exception:
                logger.exception("AI trainer workout comment failed for workout %s", workout_id)
                await callback.message.answer("⚠️ Не смог получить комментарий — попробуй ещё раз позже.")
                return
            await db.set_workout_ai_comment(workout_id, comment)
        finally:
            _busy.discard(user_id)
    else:
        await callback.answer()

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
        # Заголовки идут как есть — настоящим rich-заголовком, а не жирной
        # строкой: ответ и без того густо заполнен жирным текстом, и жирный
        # «заголовок» в нём просто терялся, не выделяясь среди остального.
        text = chunk + (quota_md if is_last else "")
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
    video_context: Optional[str] = None,
    scenario: Optional[str] = None,
    progress_stages: Optional[Sequence[str]] = None,
) -> None:
    """Общая логика для текстовых и фото-вопросов: запрос к модели, история, отправка ответа.

    question — то, что реально уходит модели на этот ход (текст +, если есть, фото).
    history_question — облегчённая версия для ai_history/БД: фото туда не попадают
    (не пересылать же их каждый следующий ход), только текст/подпись или заглушка.

    user_id — по умолчанию берётся из message.from_user.id (обычное сообщение
    от пользователя); передаётся явно там, где message — это экран бота, а не
    реплика пользователя (см. ai_build_program: message.from_user там был бы ботом).

    scenario — метка сценария («program» и т.п.), которая едет дальше в
    _deliver_setup и живёт в FSM опросника до _finish_setup: только там она и
    нужна, чтобы решить, показывать ли progress_ui на самом долгом вызове. Этот
    конкретный ход её не использует вовсе.

    progress_stages — если задан, вместо ротации фраз из running_texts.py
    placeholder крутит анимированный чек-лист progress_ui (см. модуль): растущий
    фейковый процент и галочки по этапам. Задаёт его только _finish_setup —
    тот самый ход, что реально уходит за составом программы; на всех остальных
    (включая опросник и обычный чат) placeholder остаётся прежним.

    Caller owns the `_busy` reservation end-to-end (claimed atomically before
    any await, released in the caller's `finally`) — this function assumes the
    reservation is already held and never touches `_busy` itself.
    """
    user_id = user_id if user_id is not None else message.from_user.id
    asked_today = await db.get_ai_question_count_today(user_id)
    # Дневная квота вопросов и суточный стоп по деньгам — одной проверкой (см.
    # ai_limits.check). С той же клавиатурой, что у обычных ответов чата:
    # сообщение о лимите становится нижним экраном переписки, и без кнопок из
    # него оставался только выход через нижнее меню.
    block = await ai_limits.check(user_id, ai_limits.KIND_QUESTION)
    if block is not None:
        logger.info("AI question blocked for user %s: %s", user_id, block.log)
        await ai_limits.reply(message, block, reply_markup=await ai_keyboard(user_id))
        # preview — свой аккаунт, ещё не нажавший «Понятно» сегодня: вопрос
        # всё равно уходит, предупреждение не отменяет действие, которое его
        # вызвало (см. ai_limits.py и такую же развилку в других вызовах check).
        if not block.preview:
            return

    data = await state.get_data()
    history = data.get("ai_history", [])

    # The daily counter is charged only once there's an answer to show for it —
    # a provider outage shouldn't cost the user one of their questions.
    if progress_stages:
        # Сборка программы (см. _finish_setup): вместо ротации фраз — фейковый
        # процент и чек-лист этапов, см. progress_ui. Реальные статусы ask()
        # (веб-поиск и т.п.) тут не показываем нарочно — на этом ходу они
        # спорили бы с чек-листом за один и тот же placeholder.
        placeholder = await message.answer(progress_ui.initial_text(progress_stages))
        display = None
    else:
        # Пул фраз подбирается по теме вопроса (питание, программа, конкретное
        # упражнение и т.д. — см. running_texts.py), чтобы даже самый первый
        # placeholder до единого tool-call звучал в тему, а не наугад.
        running_pool = running_texts.pool_for(question)
        running_text = running_texts.pick(running_pool)
        placeholder = await message.answer(running_text)
        display = _RunningDisplay(placeholder, running_text, running_pool)

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

    # Опросник перед сборкой программы (см. ai_trainer.ask_setup_questions).
    # Приезжает целиком одним вызовом — дальше бот крутит его сам, по одному
    # вопросу на сообщение, ни разу не сходив к модели между ними.
    setup_questions: list[dict] = []

    async def collect_questions(questions: list[dict]) -> None:
        # Новый опросник за тот же ход затирает предыдущий — как и черновик
        # программы: показать человеку два опросника подряд нельзя.
        setup_questions.clear()
        setup_questions.extend(questions)

    # Размышления финального раунда. Пользователю они не показываются — они
    # нужны, чтобы уехать назад вместе с ответом в истории: по документации xAI
    # отсутствие reasoning_content в отправленной истории это причина промахов
    # кэша номер один у ризонинговых моделей, а промах по нашей шапке в
    # одиннадцать тысяч токенов стоит вчетверо дороже попадания.
    reasoning_cell: dict[str, str] = {}

    async def collect_reasoning(reasoning: str) -> None:
        reasoning_cell["text"] = reasoning

    # Ровно тот список сообщений, что уехал модели — включая её обращения к
    # инструментам и их результаты. Его и сохраняем как историю: тогда следующий
    # вопрос ДОПИСЫВАЕТ к неизменному префиксу, а не отдаёт переписанный, и кэш
    # xAI попадает. Собранная руками пара «вопрос-ответ» этого не даёт: по их
    # документации любое изменение ранних сообщений — промах.
    wire_cell: dict[str, list] = {}

    async def collect_wire(messages: list) -> None:
        wire_cell["messages"] = messages

    # Поисковый потолок сработал — обычный атлет об этом не узнаёт (ответ просто
    # идёт без свежести), а свой аккаунт получает предупреждение с кнопкой. Само
    # решение принимается внутри ask(), где ни бота, ни экрана нет.
    async def warn_about_limit(block) -> None:
        logger.info("AI search blocked for user %s: %s", user_id, block.log)
        with suppress(TelegramAPIError):
            await ai_limits.reply(message, block)

    # Определена заранее — на случай, если создание задачи ниже упадёт ДО
    # присвоения (не должно, но тогда finally не свалился бы с NameError).
    running_task: Optional[asyncio.Task] = None
    try:
        ask_task = asyncio.create_task(
            ai_trainer.ask(
                user_id, question, history, image_data_url=image_data_url,
                on_status=(display.set_status if display else None), on_program=collect_program,
                on_action=collect_action, on_questions=collect_questions,
                on_chunk=streamer.push,
                video_context=video_context, on_reasoning=collect_reasoning,
                on_wire=collect_wire, on_limit=warn_about_limit,
            )
        )
        running_task = asyncio.create_task(
            display.cycle_idle() if display else progress_ui.run_progress(placeholder, ask_task, progress_stages)
        )
        answer = await ask_task
    except Exception:
        logger.exception("AI trainer request failed for user %s", user_id)
        error_text = "⚠️ Не смог получить ответ — попробуй ещё раз чуть позже."
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
        if running_task is not None:
            running_task.cancel()
            with suppress(asyncio.CancelledError):
                await running_task
        await streamer.close()

    # Атомарно: UPDATE ... WHERE count < limit одним выражением, а не
    # «прочитал (asked_today выше) → посчитал → увеличил» — тот же счётчик,
    # который раньше двигался обычным инкрементом, теперь не может перескочить
    # свой потолок (см. db.try_increment_ai_question_count). Списание
    # по-прежнему ПОСЛЕ ответа: сорвавшийся у провайдера запрос не должен
    # стоить человеку вопроса.
    await db.try_increment_ai_question_count(user_id, config.AI_QUESTION_DAILY_LIMIT)
    # Warn before the wall, not at it — the old behaviour only ever mentioned the
    # limit by refusing.
    left = config.AI_QUESTION_DAILY_LIMIT - (asked_today + 1)
    show_quota = 0 < left <= _QUOTA_WARN_AT
    quota_html = f"\n\n<i>Осталось вопросов сегодня: {left}</i>" if show_quota else ""
    quota_md = f"\n\n_Осталось вопросов сегодня: {left}_" if show_quota else ""

    # Одноразовая подсказка про действия (см. ACTIONS_HINT_TEXT) — хвостом к
    # этому же ответу, тем же курсивом, что и счётчик квоты: отдельное
    # сообщение ради статической строчки читалось бы как пуш. Отметка ставится
    # до отправки (как profile_shown_on у _memory_reminder): в худшем случае
    # хвост потеряется вместе с упавшим чанком, но дважды не покажется. Сюда
    # доходят только состоявшиеся ответы — сбой провайдера выше вернул return,
    # и подсказка дождётся первого настоящего.
    if await db.claim_ai_actions_hint(user_id):
        quota_html += f"\n\n<i>{ACTIONS_HINT_TEXT}</i>"
        quota_md += f"\n\n_{ACTIONS_HINT_TEXT}_"

    # reasoning_content едет в истории рядом с ответом — иначе следующий вопрос
    # отдаёт Гроку не тот префикс, что был закэширован, и платим по полной за все
    # одиннадцать тысяч токенов шапки. Ключа нет, когда модель размышлений не
    # вернула: пустое поле — это тоже изменение сообщения, и оно ломает префикс
    # так же, как отсутствующее.
    if wire_cell.get("messages"):
        # Фактическая история запроса — она уже обрезана по размеру в ai_trainer
        # (_trim_wire_history) и по границам ходов, чтобы tool-сообщение не
        # осталось без своего assistant(tool_calls). HISTORY_LIMIT тут не
        # применяем: он считает реплики, а здесь сообщений на ход бывает с
        # десяток, и обрезка по их числу рвала бы префикс на каждом ходе.
        history = wire_cell["messages"]
    else:
        # Колбэк не сработал — значит ход упал где-то до конца. Собираем пару
        # руками, как раньше: кэш на следующем вопросе промахнётся, зато
        # разговор не потеряется.
        assistant_entry: dict[str, Any] = {"role": "assistant", "content": answer}
        if reasoning_cell.get("text"):
            assistant_entry["reasoning_content"] = reasoning_cell["text"]
        history = (
            history
            + [
                {"role": "user", "content": history_question},
                assistant_entry,
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
        actions = await _register_actions(state, actions)
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

    # Опросник — последним, уже под ответом: сначала человек читает, что тренер
    # понял из истории, и только потом получает первый вопрос отдельным
    # сообщением. Программа и опросник за один ход взаимоисключающи: если
    # тренер уже собрал план, спрашивать вводные поздно и незачем.
    await _deliver_setup(
        message, state, user_id,
        [] if program_draft else setup_questions,
        goal=history_question,
        scenario=scenario,
    )


# ---------- опросник перед сборкой программы (ask_setup_questions) ----------

# Сколько кругов уточнений подряд разрешаем одной просьбе. Второй круг нужен по
# делу: увидев в ответах встречный вопрос или «хз», тренер вправе ответить и
# переспросить то, что осталось открытым. А вот без потолка он способен гонять
# уточнения по кругу, и человек не увидит программу никогда — поэтому на третий
# заход опросник уже не показывается, а тренеру уходит прямое «собирай на
# дефолтах» (см. _deliver_setup).
SETUP_MAX_ROUNDS = 2

# Вопрос про цель бот задаёт сам, а не полагается на модель. Цель была одним
# пунктом промпта среди пяти, а слотов в опроснике меньше, чем тем, — и она
# регулярно проигрывала дням, времени, травмам и сплиту: человек отвечал на
# четыре вопроса и получал программу, ни разу не сказав, ЗАЧЕМ он тренируется.
# Это единственная вводная, без которой программа собирается наугад, поэтому
# она идёт первой и не зависит от того, вспомнит ли о ней модель.
#
# Варианты — четыре ходовые цели (потолок SETUP_MAX_CHOICES тоже четыре).
# Кнопками ответ не запирается: под вопросом с вариантами стоит
# SETUP_HINT_WITH_CHOICES — «жми вариант или напиши свой», и текстовый ответ
# обрабатывается ровно так же (см. _record_setup_answer).
SETUP_GOAL_QUESTION = {
    "question": "Чего хочешь от тренировок?",
    "choices": ["Набрать массу", "Стать сильнее", "Похудеть", "Вернуться в форму"],
}

# По этим кускам узнаём вопрос про цель, который модель всё-таки задала сама
# (промпт запрещает, но запрет — не гарантия). Совпало — свой не подставляем:
# два вопроса про одно подряд читаются как поломка.
SETUP_GOAL_MARKERS = ("цел", "чего хочешь", "чего ждёшь", "зачем тренир", "какой результат")

# Подсказка под вопросом. Про «не знаю» сказано прямо и намеренно: без этого
# человек, который не может ответить, либо выдумывает число, либо застревает —
# а разобраться с «хз» и встречным вопросом умеет финальная сборка, ей эти
# ответы уедут как есть.
SETUP_HINT_WITH_CHOICES = "Жми вариант или напиши свой. Не знаешь — так и скажи, разберёмся."
SETUP_HINT_TEXT_ONLY = "Ответь словами. Не знаешь — так и скажи, разберёмся."

# Рамка, в которой ответы уезжают модели. Она же — единственное место, где
# решается «это ответ или встречный вопрос»: локально таких детекторов нет и не
# будет (в переписке люди не ставят вопросительных знаков, а «как получится» в
# ответ на «сколько времени» — нормальный ответ), а модель в финальном вызове
# видит и вопрос, и ответ, и всю историю разом.
SETUP_ANSWERS_FRAME = (
    "Это ответы человека на твои уточняющие вопросы. Если вместо ответа он задал "
    "встречный вопрос или написал, что не знает, — ответь ему на это в тексте и, "
    "если без этих данных программу собирать нельзя, вызови ask_setup_questions "
    "заново ТОЛЬКО с теми вопросами, что остались открытыми. Если данных хватает — "
    "не переспрашивай: возьми разумный дефолт и назови его вслух. "
    # Без этой строки шаг закрывался пересказом: модель послушно называла дефолты
    # («ставлю 60 мин на сессию, травм нет…») и на этом останавливалась. Человек
    # отвечал на четыре вопроса и не получал ни состава, ни кнопки сохранения —
    # весь опросник оказывался впустую.
    "И в любом случае, кроме переспрашивания, на этом же шаге СОБЕРИ программу "
    "вызовом propose_program: без вызова инструмента человек не увидит ни состава, "
    "ни кнопки сохранения — один твой текст программой не является."
)

# Уходит модели вместо третьего круга уточнений подряд.
SETUP_ENOUGH_FRAME = (
    "Уточнения закончились — больше вопросов человеку я не задам. Собирай программу "
    "на разумных дефолтах и назови их вслух: из чего исходил по дням, времени, "
    "опыту и оборудованию. «Собирай» здесь буквально: вызови propose_program, "
    "иначе человек останется с текстом вместо программы."
)


def _active_setup(data: dict) -> Optional[dict]:
    """Живой опросник из FSM — или None, если отвечать сейчас не на что.

    Между концом опросника и ответом тренера в `ai_setup` остаётся один только
    счётчик кругов (см. _finish_setup): вопросов там уже нет, и перехватывать
    сообщения пользователя он не должен — иначе следующая реплика уехала бы
    ответом в опросник, которого нет.
    """
    setup = data.get("ai_setup")
    if not isinstance(setup, dict):
        return None
    questions = setup.get("questions") or []
    if not questions or int(setup.get("idx") or 0) >= len(questions):
        return None
    return dict(setup)


def _setup_question_html(
    idx: int, total: int, question: str, has_choices: bool, answer: Optional[str] = None
) -> str:
    """Один вопрос отдельным сообщением: где мы в опроснике, сам вопрос и что дальше.

    Номер и «из скольки» — не украшение: человек должен видеть, что это не
    бесконечный допрос, а три-четыре тапа до программы.
    """
    head = f"🤖 <b>ВОПРОС {idx + 1} ИЗ {total}</b>"
    if answer is not None:
        return f"{head}\n\n{escape(question)}\n\n{escape(answer)}"
    hint = SETUP_HINT_WITH_CHOICES if has_choices else SETUP_HINT_TEXT_ONLY
    return f"{head}\n\n{escape(question)}\n\n<i>{hint}</i>"


async def _show_setup_question(target: Message, state: FSMContext) -> None:
    """Отправить текущий вопрос опросника отдельным сообщением."""
    data = await state.get_data()
    setup = _active_setup(data)
    if setup is None:
        return
    idx = int(setup.get("idx") or 0)
    questions = setup["questions"]
    question = questions[idx]
    choices = question.get("choices") or []
    sent = await target.answer(
        _setup_question_html(idx, len(questions), question["question"], bool(choices)),
        parse_mode="HTML",
        reply_markup=keyboards.ai_setup_question_keyboard(idx, choices),
    )
    # id сообщения нужен, чтобы погасить его кнопки, когда на вопрос ответят
    # (в том числе текстом, а не тапом) — см. _close_setup_question.
    setup["msg_id"] = getattr(sent, "message_id", None)
    await state.update_data(ai_setup=setup)


async def _close_setup_question(bot, chat_id: int, setup: dict, tail: str) -> None:
    """Дописать в сообщение с вопросом то, что на него ответили, и снять кнопки.

    Без этого в истории чата остаётся ряд одинаковых живых кнопок под каждым
    отвеченным вопросом — а они в Telegram живут вечно и приглашают тапнуть
    ещё раз.
    """
    msg_id = setup.get("msg_id")
    if not msg_id:
        return
    idx = int(setup.get("idx") or 0)
    questions = setup.get("questions") or []
    if idx >= len(questions):
        return
    html = _setup_question_html(
        idx, len(questions), questions[idx]["question"], has_choices=False, answer=tail
    )
    with suppress(TelegramBadRequest, TelegramAPIError):
        await bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id, text=html, parse_mode="HTML", reply_markup=None
        )


def _setup_answers_text(setup: dict) -> str:
    """Одно сообщение модели со всеми ответами разом — и исходной задачей.

    Пропущенные («⏭ Собирай так» на середине) названы прямо: иначе тренер
    решит, что вопрос просто потерялся, и переспросит его ещё раз.
    """
    questions = setup.get("questions") or []
    answers = setup.get("answers") or []
    lines = ["Вот ответы:"]
    skipped = False
    for idx, question in enumerate(questions):
        if idx < len(answers) and answers[idx] is not None:
            lines.append(f"— {question['question']} — {answers[idx]}")
        else:
            skipped = True
            lines.append(f"— {question['question']} — пропустил, не ответил")
    goal = setup.get("goal")
    if goal:
        lines.append(f"\nИсходная задача: {goal}")
    if skipped:
        lines.append(
            "На пропущенные отвечать не стал — возьми по ним разумные дефолты и "
            "назови их вслух."
        )
    lines.append(SETUP_ANSWERS_FRAME)
    return "\n".join(lines)


async def _questions_with_goal(
    user_id: int, questions: list[dict], previous: dict
) -> tuple[list[dict], bool]:
    """Поставить вопрос про цель первым — если её ещё никто не спросил.

    Возвращает (вопросы, «цель закрыта»). Второе переживает круги уточнений в
    `ai_setup`: профиль между кругами не меняется (модель сохранит цель только
    в финальной сборке), так что без флага второй круг задал бы тот же вопрос
    ещё раз.
    """
    if previous.get("goal_asked"):
        return questions, True
    asked_by_model = any(
        marker in (question.get("question") or "").lower()
        for question in questions
        for marker in SETUP_GOAL_MARKERS
    )
    if asked_by_model:
        return questions, True
    user = await db.get_user(user_id)
    if user is not None and (user["goal"] or "").strip():
        # Цель уже записана с его слов — переспрашивать то, что бот показывает
        # на экране «Обо мне», значит признаваться, что он этого не помнит.
        return questions, True
    goal_question = {
        "question": SETUP_GOAL_QUESTION["question"],
        "choices": list(SETUP_GOAL_QUESTION["choices"]),
    }
    # Срезаем с хвоста: свой вопрос идёт первым, а лишним оказывается последний
    # вопрос модели — он же и наименее важный, вопросы она ставит по убыванию.
    return ([goal_question] + questions)[: ai_trainer.SETUP_MAX_QUESTIONS], True


async def _deliver_setup(
    message: Message,
    state: FSMContext,
    user_id: int,
    questions: list[dict],
    goal: str,
    scenario: Optional[str] = None,
) -> None:
    """Разложить собранный моделью опросник в FSM и показать первый вопрос.

    Сюда же стекаются все три исхода хода: опросника нет (круг закрыт), опросник
    есть (показываем), опросник есть, но круги кончились (уходим в сборку).

    `scenario` переживает круги ровно как `goal` — тот же приём (see ниже
    `previous_scenario or scenario`): он взят с самого первого хода
    (_start_ai_scenario), а на втором-третьем круге history_question — уже не
    исходная просьба, и передавать сюда None значило бы потерять метку сценария
    посередине опросника.
    """
    data = await state.get_data()
    stored = data.get("ai_setup")
    previous: dict = stored if isinstance(stored, dict) else {}
    rounds = int(previous.get("rounds") or 0)
    previous_goal = previous.get("goal")
    resolved_scenario = scenario or previous.get("scenario")

    if not questions:
        # Тренер ответил без нового опросника — цикл уточнений закрыт, и
        # счётчик кругов больше ничего не сторожит. Стереть его обязательно:
        # иначе следующая, уже совсем другая просьба собрать программу
        # упёрлась бы в потолок с первого же вопроса.
        if previous:
            await state.update_data(ai_setup=None)
        return

    if rounds > SETUP_MAX_ROUNDS:
        # Мы внутри принудительной сборки (см. ниже) — и тренер опять просит
        # уточнений. Тихо выбрасываем: ещё один заход по кругу человеку уже
        # ничего не даст, а текст ответа у него на экране есть.
        await state.update_data(ai_setup=None)
        return

    if rounds >= SETUP_MAX_ROUNDS:
        # Круги кончились. Счётчик уводим ЗА потолок до вызова модели — это и
        # есть тормоз рекурсии: опросник, который тренер соберёт в ответ на это
        # сообщение, попадёт в ветку выше и никого не спросит.
        await state.update_data(
            ai_setup={
                "rounds": rounds + 1,
                "goal": previous_goal,
                "goal_asked": bool(previous.get("goal_asked")),
                "scenario": resolved_scenario,
            }
        )
        text = SETUP_ENOUGH_FRAME
        if previous_goal:
            text = f"{text}\nИсходная задача: {previous_goal}"
        # Этот ход тоже реально уходит собирать программу (SETUP_ENOUGH_FRAME
        # прямо требует вызвать propose_program) — поэтому тот же чек-лист, что
        # и у обычного _finish_setup, а не голое «тренер думает».
        await _handle_question(
            message, state, text, history_question=text, user_id=user_id,
            scenario=resolved_scenario,
            progress_stages=PROGRAM_PROGRESS_STAGES if resolved_scenario == "program" else None,
        )
        return

    # Прошлый опросник мог остаться брошенным на середине — его вопрос висит в
    # чате с живыми кнопками. Сам тап по ним теперь безвреден (сверяется msg_id),
    # но кнопка, которая ничего не делает, — приглашение потыкать и решить, что
    # бот сломался. Гасим, дописав, чем всё кончилось. На втором круге сюда не
    # попадаем: там вопросы уже отвечены и в состоянии остался один счётчик.
    stale_questions = previous.get("questions") or []
    if stale_questions and int(previous.get("idx") or 0) < len(stale_questions):
        await _close_setup_question(
            message.bot, message.chat.id, previous, "⏹ Опросник отменён — начали заново"
        )
    questions, goal_asked = await _questions_with_goal(user_id, questions, previous)
    await state.update_data(
        ai_setup={
            "questions": questions,
            "goal_asked": goal_asked,
            "answers": [],
            "idx": 0,
            # Исходная цель переживает круги: на втором заходе history_question
            # — это уже простыня с ответами первого, и подставлять её целью
            # значило бы вкладывать её саму в себя.
            "goal": previous_goal or goal,
            "rounds": rounds + 1,
            "scenario": resolved_scenario,
        }
    )
    await _show_setup_question(message, state)


async def _finish_setup(target: Message, state: FSMContext, user_id: int, setup: dict) -> None:
    """Опросник закончился — уходим за программой одним обычным вызовом модели.

    Это и есть самый долгий вызов всего сценария «Составить программу» — тот,
    что реально дёргает propose_program (опросник до этого момента модель ни
    разу не трогал, см. модульный докстринг файла с тестами опросника). Если
    сценарий помечен как «program» (см. _start_ai_scenario), placeholder крутит
    progress_ui вместо голого «тренер думает»; для остальных (в т.ч. «Тренировка
    на сегодня») поведение не меняется.
    """
    text = _setup_answers_text(setup)
    scenario = setup.get("scenario")
    # Гасим опросник ДО вызова модели: тренер думает десятки секунд, и всё это
    # время человек может дописать ещё реплику — она обязана уехать вопросом, а
    # не ответом в опросник, которого уже нет. Счётчик кругов переживает: он и
    # есть потолок (см. SETUP_MAX_ROUNDS), а вопросов в нём не остаётся, так что
    # перехватывать сообщения он не будет (см. _active_setup).
    await state.update_data(
        ai_setup={
            "rounds": int(setup.get("rounds") or 1),
            "goal": setup.get("goal"),
            # Профиль обновится только после сборки, поэтому «цель уже спросили»
            # хранится флагом — иначе второй круг задал бы тот же вопрос снова.
            "goal_asked": bool(setup.get("goal_asked")),
            "scenario": scenario,
        }
    )
    # В ai_history и в дневник переписки это уезжает как есть — человеческими
    # строчками «вопрос — ответ», а не служебным JSON: get_full_chat_history
    # читают и модель, и мы.
    await _handle_question(
        target, state, text, history_question=text, user_id=user_id,
        scenario=scenario,
        progress_stages=PROGRAM_PROGRESS_STAGES if scenario == "program" else None,
    )


async def _record_setup_answer(
    target: Message, state: FSMContext, user_id: int, setup: dict, answer: str
) -> None:
    """Записать ответ на текущий вопрос и шагнуть дальше — или уйти в сборку.

    Модель тут не вызывается вовсе: весь опросник уже лежит в FSM, а квоту и
    ожидание «печатает…» стоит тратить один раз — на программу.
    """
    # None — «пропустил этот вопрос»: место в списке занимает, но ответом не
    # притворяется (см. _setup_answers_text).
    setup["answers"] = [*(setup.get("answers") or []), answer]
    setup["idx"] = int(setup.get("idx") or 0) + 1
    await state.update_data(ai_setup=setup)
    if setup["idx"] < len(setup["questions"]):
        await _show_setup_question(target, state)
        return
    await _finish_setup(target, state, user_id, setup)


@router.callback_query(F.data.startswith("ai:qa:"))
async def ai_setup_choice(callback: CallbackQuery, state: FSMContext):
    """Тап по варианту ответа в опроснике перед сборкой программы.

    Индекс вопроса в callback_data сверяется с текущим: кнопки под прошлыми
    вопросами остаются в чате живыми, и тап по ним не должен записывать ответ
    не туда (см. keyboards.ai_setup_question_keyboard).
    """
    parts = callback.data.split(":")
    data = await state.get_data()
    setup = _active_setup(data)
    if setup is None:
        await callback.answer(
            "Этот вопрос уже позади — спрашивай что хочешь словами 👇", show_alert=True
        )
        return
    # Индекса мало: он совпадает и у вопроса из ПРОШЛОГО, брошенного опросника,
    # если тот остановился на том же шаге. Тап по такой кнопке (а она живёт в чате
    # вечно) записывался ответом в текущий опросник — молча и не тем вариантом,
    # который человек видел под пальцем. msg_id привязывает кнопки к конкретному
    # сообщению, а его мы и так храним, чтобы их гасить.
    if (
        len(parts) != 4
        or parts[2] != str(setup["idx"])
        or getattr(callback.message, "message_id", None) != setup.get("msg_id")
    ):
        await callback.answer("Этот вопрос уже позади — отвечай на нижний 👇", show_alert=True)
        return
    choices = setup["questions"][setup["idx"]].get("choices") or []
    choice_index = int(parts[3]) if parts[3].isdigit() else -1
    if not 0 <= choice_index < len(choices):
        await callback.answer("Не нашёл такой вариант — напиши ответ словами 👇", show_alert=True)
        return
    answer = choices[choice_index]

    user_id = callback.from_user.id
    last = setup["idx"] + 1 >= len(setup["questions"])
    # Бронь берём ДО записи ответа и только на последнем вопросе: за ним сразу
    # идёт настоящий вызов модели, а промежуточные шаги её не трогают вовсе.
    if last and not _try_claim_busy(user_id):
        await callback.answer("Секунду, ещё думаю над прошлым вопросом 😅", show_alert=True)
        return
    try:
        with suppress(TelegramBadRequest):
            await callback.answer()
        await _close_setup_question(callback.bot, callback.message.chat.id, setup, f"✅ {answer}")
        await _record_setup_answer(callback.message, state, user_id, setup, answer)
    finally:
        if last:
            _busy.discard(user_id)


@router.callback_query(F.data == "ai:qskip")
async def ai_setup_skip(callback: CallbackQuery, state: FSMContext):
    """«⏭ Пропустить» — пропустить ЭТОТ вопрос и шагнуть к следующему.

    Раньше кнопка обрывала опросник целиком, и пропустить один неудобный вопрос
    было нельзя — только все сразу. Пропустить всё по-прежнему можно, просто
    тапов столько же, сколько вопросов; зато выбор перестал быть «или отвечай на
    всё, или ни на что».

    Индекса вопроса в callback_data нет намеренно: кнопка означает одно и то же
    на любом шаге, а какой шаг текущий — знает состояние.
    """
    data = await state.get_data()
    setup = _active_setup(data)
    if setup is None:
        await callback.answer(
            "Уточнения уже позади — что поправить, пиши словами 👇", show_alert=True
        )
        return
    user_id = callback.from_user.id
    last = int(setup.get("idx") or 0) + 1 >= len(setup.get("questions") or [])
    # Бронь — только на последнем шаге: за ним сразу идёт вызов модели, а
    # промежуточные пропуски её не трогают (как и промежуточные ответы).
    if last and not _try_claim_busy(user_id):
        await callback.answer("Секунду, ещё думаю над прошлым вопросом 😅", show_alert=True)
        return
    try:
        with suppress(TelegramBadRequest):
            await callback.answer("Пропустил" if not last else "Понял, собираю")
        await _close_setup_question(
            callback.bot, callback.message.chat.id, setup, "⏭ Пропустил"
        )
        await _record_setup_answer(callback.message, state, user_id, setup, None)
    finally:
        if last:
            _busy.discard(user_id)


@router.message(AITrainerFlow.chatting, F.text)
async def ai_question(message: Message, state: FSMContext):
    question = (message.text or "").strip()
    if not question:
        return
    user_id = message.from_user.id

    # Диагностика к handlers/factcheck.py: пересланный пост должен уходить в
    # разбор, а не сюда, и фильтр там опирается ровно на forward_origin. На
    # проде пост из канала всё-таки попал в чат тренера — по логу не отличить,
    # не проставил ли Telegram признак пересылки или человек скопировал текст
    # руками. Одна строка в WARNING отвечает на это в следующий же раз.
    if message.forward_origin is not None:
        logger.warning(
            "Пересланное сообщение дошло до чата тренера мимо фактчека "
            "(origin=%s, длина %s) — проверь handlers.factcheck фильтр",
            type(message.forward_origin).__name__, len(question),
        )

    # Опросник — ПЕРВЫМ делом, до брони и до квоты: пока он идёт, любой текст
    # человека это ответ на текущий вопрос, а не вопрос тренеру. Даже встречное
    # «а сколько вообще надо?» — гадать об этом локально мы не беремся, оно
    # уедет модели вместе с самим вопросом, и разберётся с ним финальная сборка
    # (см. SETUP_ANSWERS_FRAME).
    data = await state.get_data()

    # Ждём название упражнения к присланному ролику — значит этот текст ответ на
    # него, а не новый вопрос тренеру. Тот же приём, что и с опросником ниже:
    # перехват внутри хендлера по состоянию, а не отдельным фильтром.
    pending = data.get("aivid_pending")
    if pending:
        if not _try_claim_busy(user_id):
            await message.reply("Секунду, ещё думаю над прошлым вопросом 😅")
            return
        try:
            await state.update_data(aivid_pending=None)
            await _analyze_video_and_answer(
                message, state, user_id,
                file_id=pending["file_id"],
                mime_type=pending.get("mime_type") or "video/mp4",
                duration=pending.get("duration"),
                exercise_hint=question,
                caption=pending.get("caption") or "",
            )
        finally:
            _busy.discard(user_id)
        return

    setup = _active_setup(data)
    if setup is not None:
        last = setup["idx"] + 1 >= len(setup["questions"])
        if last and not _try_claim_busy(user_id):
            await message.reply("Секунду, ещё думаю над прошлым вопросом 😅")
            return
        try:
            await _close_setup_question(message.bot, message.chat.id, setup, f"✅ {question}")
            # Ответ уже вписан в сам вопрос — исходную реплику убираем, иначе в
            # чате он стоит дважды подряд. У ответа кнопкой такого дубля нет, и
            # тексту незачем выглядеть иначе.
            with suppress(TelegramBadRequest):
                await message.delete()
            await _record_setup_answer(message, state, user_id, setup, question)
        finally:
            if last:
                _busy.discard(user_id)
        return

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
            await message.reply(
                f"Фото слишком большое — уложись в {MAX_IMAGE_BYTES // (1024 * 1024)} МБ."
            )
            return

        history_question = f"[фото] {caption}" if caption else "[прислал фото]"
        await _handle_question(
            message, state, question, history_question=history_question, image_data_url=image_data_url
        )
    finally:
        _busy.discard(user_id)


DEFAULT_VIDEO_QUESTION = (
    "Разбери мою технику по этому видео: что там видно и что мне поправить первым делом."
)


# Сколько своих упражнений показать кнопками, когда спрашиваем «что это было».
# Список из db.list_user_exercises отсортирован по частоте использования, так что
# нужное почти всегда в первых строках, а длинная простыня в зале только мешает.
VIDEO_EXERCISE_CHOICES = 6


def _video_exercise_keyboard(names: list[str], page: int, total: int) -> InlineKeyboardMarkup:
    """Страница каталога + листалка.

    Имена уезжают не в callback_data, а в FSM: там кириллица, а у Telegram на
    callback_data 64 БАЙТА, и «Разгибания на трицепс в кроссовере» в них не
    влезет. По индексу же всегда влезает.
    """
    kb = InlineKeyboardBuilder()
    for i, name in enumerate(names):
        kb.button(text=name, callback_data=f"aivid:ex:{i}")
    kb.adjust(1)

    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="← назад", callback_data=f"aivid:page:{page - 1}")
    if (page + 1) * VIDEO_EXERCISE_CHOICES < total:
        nav.button(text="ещё →", callback_data=f"aivid:page:{page + 1}")
    if nav.buttons:
        kb.attach(nav)
        nav.adjust(2)

    tail = InlineKeyboardBuilder()
    tail.button(text="Разбери так, без названия", callback_data="aivid:skip")
    tail.adjust(1)
    kb.attach(tail)
    return kb.as_markup()


async def _video_exercise_page(user_id: int, page: int) -> tuple[list[str], int]:
    rows = await db.list_user_exercises(
        user_id, limit=VIDEO_EXERCISE_CHOICES, offset=page * VIDEO_EXERCISE_CHOICES
    )
    total = await db.count_user_exercises(user_id)
    return [r["display_name"] for r in rows], total


async def _exercise_from_caption(user_id: int, caption: str) -> Optional[str]:
    """Подпись к ролику → название упражнения, если оно там вообще есть.

    Живой провал: подпись «оцени технику» принимали ЗА НАЗВАНИЕ и уходили прямо
    в разбор мимо вопроса. Модель, получив «оцени технику» вместо движения,
    честно не нашла там упражнения, угадала становую и вернула среднюю
    уверенность — то есть ровно тот дорогой путь, ради отмены которого вопрос и
    заводили: разбор оплачен, полторы минуты потрачены, вопрос списан с квоты.

    Проверяем по каталогу самого атлета, а не по словарю ключевых слов: там
    лежат его собственные названия, включая переименованные и заведённые
    руками. Не нашли — значит подписи про упражнение не было, и надо спросить.
    """
    low = caption.casefold()
    best: Optional[str] = None
    for row in await db.list_user_exercises(user_id):
        name = row["display_name"]
        folded = name.casefold()
        # В обе стороны: подпись «румынская тяга» при упражнении «Румынская тяга
        # со штангой» и наоборот, короткое имя внутри длинной подписи.
        # Из нескольких совпадений берём самое длинное: «жим лёжа узким хватом»
        # точнее, чем «жим лёжа», а короткое совпадает всегда.
        matched = folded in low or low in folded
        if matched and (best is None or len(name) > len(best)):
            best = name
    return best


async def _analyze_video_and_answer(
    message: Message,
    state: FSMContext,
    user_id: int,
    *,
    file_id: str,
    mime_type: str,
    duration: Optional[int],
    exercise_hint: Optional[str],
    caption: str = "",
) -> None:
    """Скачать ролик, разобрать и ответить голосом тренера.

    Вынесено из хендлера, потому что путей сюда два: ролик с подписью разбирается
    сразу, а ролик без подписи — только после того, как атлет назовёт упражнение
    кнопкой. Скачиваем по file_id, а не по объекту из апдейта: между вопросом и
    ответом проходит время, и держать всё это в памяти незачем.
    """
    status = await message.answer("🎥 Смотрю видео...")
    try:
        buf = await message.bot.download(file_id)
        # Упражнение из подписи или из кнопки — источник надёжнее глаз модели.
        # Живой провал: тягу штанги к поясу она приняла за становую и выдала три
        # классические ошибки становой, которых в кадре не было.
        analysis = await video_analysis.analyze(
            buf.read(), user_id,
            mime_type=mime_type,
            exercise_hint=exercise_hint,
        )
    except Exception:
        logger.exception("video download/analysis failed for user %s", user_id)
        analysis = None
    finally:
        with suppress(TelegramBadRequest):
            await status.delete()

    if analysis is None:
        await message.reply(
            "Не смог разобрать это видео. Попробуй ещё раз или сними сбоку, "
            "чтобы попал весь подход."
        )
        return

    # Квота тратится только за разбор, который получился, — как и дневной
    # счётчик вопросов, который списывается лишь при готовом ответе.
    await db.increment_ai_video_count(user_id)

    asked = caption or (f"Разбери технику: {exercise_hint}." if exercise_hint else "")
    question = asked or DEFAULT_VIDEO_QUESTION
    history_question = f"[видео] {asked}" if asked else "[прислал видео подхода]"
    await _handle_question(
        message, state, question,
        history_question=history_question,
        video_context=video_analysis.to_context_block(analysis),
    )


@router.message(AITrainerFlow.chatting, F.video | F.video_note | F.animation)
async def ai_video_question(message: Message, state: FSMContext):
    """Ролик подхода → наблюдения от Qwen3-VL → ответ голосом тренера.

    Порядок проверок — от самой дешёвой к самой дорогой: сначала настройка, потом
    длина (её Telegram сообщает в апдейте, качать не нужно), потом дневная квота,
    и только под конец скачивание с разбором. Иначе за отказ платили бы трафиком.

    Упражнение спрашивается ДО разбора, а не после. Раньше модель угадывала его
    сама, и на неуверенной догадке тренер переспрашивал — но ролик к тому моменту
    был уже посмотрен и оплачен, а наблюдения собраны под чужое движение и
    переиспользовать их нельзя. Полторы минуты и три цента впустую, плюс списанный
    вопрос из дневной квоты за то, что бот не разобрался. Вопрос кнопками не стоит
    ни одного вызова модели.

    animation в фильтре обязателен: ролик БЕЗ АУДИОДОРОЖКИ Telegram отдаёт не как
    video, а как animation (в клиенте он подписан «GIF»), и снятое в зале видео
    сплошь такое — телефон часто пишет молча, да и пересланное без звука
    конвертируется. Без этой ветки такой ролик проваливался в общий fallback
    «Не понял», то есть фича молча не работала на самом частом входе.
    """
    user_id = message.from_user.id
    # Как и в фото-хендлере: занимаем до первого await, иначе два быстрых ролика
    # проедут вдвоём через окно скачивания и оба уйдут в модель.
    if not _try_claim_busy(user_id):
        await message.reply("Секунду, ещё думаю над прошлым вопросом 😅")
        return
    try:
        if not config.video_analysis_available():
            await message.reply("Разбор видео пока не подключил. Напиши вопрос текстом.")
            return

        video = message.video or message.video_note or message.animation
        if video.duration and video.duration > config.MAX_VIDEO_SECONDS:
            await message.reply(
                f"Ролик длинный. Пришли до {config.MAX_VIDEO_SECONDS} секунд — "
                "мне хватит одного подхода."
            )
            return

        # Дневная квота роликов и суточный потолок по деньгам — до скачивания:
        # ролик тянется из Telegram мегабайтами, и делать это ради отказа глупо.
        block = await ai_limits.check(user_id, ai_limits.KIND_VIDEO)
        if block is not None:
            logger.info("AI video blocked for user %s: %s", user_id, block.log)
            await ai_limits.reply(message, block, reply_markup=await ai_keyboard(user_id))
            # preview — свой аккаунт, ещё не нажавший «Понятно» сегодня: разбор
            # всё равно идёт, предупреждение не отменяет ролик, который его
            # вызвал (иначе «Понятно» снимало бы лимит только на будущее, а
            # присланное видео пришлось бы слать заново).
            if not block.preview:
                return

        # Разбор ролика сам по себе платный (video_analysis.analyze), а ответ на
        # него дальше идёт через _handle_question — которая накрыта своей же
        # квотой вопросов и списывает её ТОЛЬКО при готовом ответе. Раньше эта
        # квота проверялась именно там, то есть уже ПОСЛЕ скачивания и разбора:
        # видео с исчерпанной квотой вопросов всё равно оплачивалось целиком, а
        # честный отказ приходил только на последнем шаге. Проверяем обе квоты
        # здесь, до единого байта трафика.
        block = await ai_limits.check(user_id, ai_limits.KIND_QUESTION)
        if block is not None and not block.preview:
            # preview (свой аккаунт) не роняет разбор — предупреждение о квоте
            # вопросов покажет _handle_question, который этой квотой и владеет;
            # здесь держим только настоящий блок, чтобы не платить за разбор
            # ролика, ответ на который всё равно не уйдёт.
            logger.info("AI video blocked (question quota) for user %s: %s", user_id, block.log)
            await ai_limits.reply(message, block, reply_markup=await ai_keyboard(user_id))
            return

        if video.file_size and video.file_size > config.MAX_VIDEO_BYTES:
            await message.reply(
                f"Файл тяжёлый, я такой не вытяну — уложись в "
                f"{config.MAX_VIDEO_BYTES // (1024 * 1024)} МБ. Сними покороче или полегче."
            )
            return

        caption = (message.caption or "").strip()
        mime_type = getattr(video, "mime_type", None) or "video/mp4"

        # Подпись — это не обязательно название. «Оцени технику» раньше уезжало
        # в разбор как упражнение и отменяло весь вопрос; название засчитываем
        # только когда оно и правда есть в каталоге атлета.
        named = await _exercise_from_caption(user_id, caption) if caption else None
        if named:
            await _analyze_video_and_answer(
                message, state, user_id,
                file_id=video.file_id, mime_type=mime_type, duration=video.duration,
                exercise_hint=named, caption=caption,
            )
            return

        names, total = await _video_exercise_page(user_id, 0)
        await state.update_data(
            aivid_pending={
                "file_id": video.file_id,
                "mime_type": mime_type,
                "duration": video.duration,
                "names": names,
                "page": 0,
                # Подпись не теряем: «оцени технику» это вопрос человека, и он
                # должен доехать до тренера вместе с разбором.
                "caption": caption,
            }
        )
        await message.reply(
            "Принял ролик. Что за упражнение?\n\n"
            "Напиши название ответом или выбери из своих ниже. Спрашиваю до того, "
            "как смотреть: угадаю неверно — разберу по чужим меркам, а это хуже, "
            "чем не разобрать вовсе.",
            reply_markup=_video_exercise_keyboard(names, 0, total),
        )
    finally:
        _busy.discard(user_id)


@router.callback_query(AITrainerFlow.chatting, F.data.startswith("aivid:"))
async def ai_video_exercise_chosen(callback: CallbackQuery, state: FSMContext):
    """Атлет назвал упражнение — теперь можно смотреть ролик."""
    user_id = callback.from_user.id
    data = await state.get_data()
    pending = data.get("aivid_pending")
    if not pending:
        await callback.answer("Ролик потерялся, пришли заново", show_alert=True)
        return
    choice = callback.data.split(":", 2)[-1]

    # Листание каталога ролик не трогает и замок не занимает — это ещё вопрос,
    # а не ответ.
    if callback.data.startswith("aivid:page:"):
        await callback.answer()
        page = max(0, int(choice) if choice.isdigit() else 0)
        names, total = await _video_exercise_page(user_id, page)
        pending["names"] = names
        pending["page"] = page
        await state.update_data(aivid_pending=pending)
        with suppress(TelegramBadRequest):
            await callback.message.edit_reply_markup(
                reply_markup=_video_exercise_keyboard(names, page, total)
            )
        return

    if not _try_claim_busy(user_id):
        await callback.answer("Секунду, ещё думаю над прошлым вопросом")
        return
    try:
        await callback.answer()
        names = pending.get("names") or []
        exercise = None
        if choice != "skip":
            try:
                exercise = names[int(choice)]
            except (ValueError, IndexError):
                exercise = None
        await state.update_data(aivid_pending=None)
        with suppress(TelegramBadRequest):
            await callback.message.edit_reply_markup(reply_markup=None)
        await _analyze_video_and_answer(
            callback.message, state, user_id,
            file_id=pending["file_id"],
            mime_type=pending.get("mime_type") or "video/mp4",
            duration=pending.get("duration"),
            exercise_hint=exercise,
            caption=pending.get("caption") or "",
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

        # Квота — ДО расшифровки, а не после. Раньше проверка стояла глубже, уже
        # в _handle_question: человек с выбранной квотой ответа не получал, но
        # каждое голосовое всё равно уезжало в распознавание и стоило нам своих
        # $0.006 — платный вызов без единого шанса дойти до ответа.
        block = await ai_limits.check(user_id, ai_limits.KIND_QUESTION)
        if block is not None:
            logger.info("AI voice blocked for user %s: %s", user_id, block.log)
            await ai_limits.reply(message, block, reply_markup=await ai_keyboard(user_id))
            # preview — свой аккаунт, ещё не нажавший «Понятно» сегодня:
            # расшифровка и ответ всё равно идут, см. тот же комментарий у
            # video-хендлера чуть выше по файлу.
            if not block.preview:
                return

        voice_file = await _download_voice_as_file(message)
        if voice_file is None:
            await message.reply(
                f"Голосовое слишком большое — уложись в "
                f"{MAX_VOICE_BYTES // (1024 * 1024)} МБ, запиши покороче."
            )
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
