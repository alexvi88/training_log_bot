import asyncio
import logging
from contextlib import suppress

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import activity_log
import admin_tasks
import announcements
import bot_profile
import chat_bottom
import config
import db
import engagement
import i18n
import keyboards
from fsm_storage import JSONFileStorage
from handlers import (
    admin,
    ai_trainer,
    backfill,
    bodyweight,
    community,
    csv_import,
    donate,
    edit_workout,
    exercise_resolve,
    exercises,
    factcheck,
    fallback,
    feedback,
    food_diary,
    game,
    history,
    mcp_access,
    persistent_menu,
    routines,
    settings,
    sharing,
    workout,
)

logger = logging.getLogger(__name__)

# Substrings (Telegram's error messages, lowercased) that mean "the user's
# screen is already stale/gone" rather than a real bug — safe to swallow.
_BENIGN_BAD_REQUEST_SUBSTRINGS = (
    "query is too old",
    "query id is invalid",
    "message is not modified",
    "message to edit not found",
    "message to delete not found",
    "message can't be deleted",
    "message can't be edited",
)


class SetUserLanguageMiddleware(BaseMiddleware):
    """Ставит язык рендера (i18n.set_lang) до того, как апдейт дойдёт до
    любого хендлера или другой middleware, которая может отправить пользователю
    текст.

    Источник — колонка users.lang, которую пишет только осознанный выбор в
    экране языка (settings.py) или догадка на первом /start (см.
    handlers/workout.py, cmd_start). Если пользователя в базе ещё нет — это
    самый первый /start, запись появится только внутри хендлера — берём
    telegram-овский language_code как временную догадку через i18n.normalize,
    но НИЧЕГО не пишем в БД: закреплять язык в базе — забота хендлера
    /start (или явного выбора в settings), не этой мидлвари.

    Да, это лишний db.get_user на каждый апдейт — и это осознанный размен:
    локальный SQLite и один поиск по первичному ключу стоят дешевле, чем
    прокидывание языка через сигнатуры всех хендлеров, коллбэков и сборщиков
    экранов. Альтернатива меряется не этим запросом, а тысячами строк диффа.
    """

    async def __call__(self, handler, event, data):
        lang = i18n.DEFAULT_LANG
        user = getattr(event, "from_user", None)
        if user is not None:
            try:
                row = await db.get_user(user.id)
            except Exception:
                # Чтение языка не должно ронять апдейт — молча остаёмся на
                # дефолте и логируем, чтобы проблему было видно, но не в чате.
                logger.exception("SetUserLanguageMiddleware: не удалось прочитать пользователя %s", user.id)
                row = None
            lang = row["lang"] if row is not None else i18n.normalize(user.language_code)
        i18n.set_lang(lang)
        return await handler(event, data)


class IgnoreStaleCallbackMiddleware(BaseMiddleware):
    """Swallow Telegram errors for callback queries that expired before we could answer them.

    Handlers do their work (DB calls, message edits) before calling
    callback.answer(), so a slow step can leave the callback query stale by
    the time answer() runs, or the underlying message can vanish (deleted by
    the user, replaced by a newer screen, etc). Telegram then rejects the
    call; this is harmless to the user and shouldn't surface as an
    unhandled exception that leaves their tap spinner stuck forever.
    """

    async def __call__(self, handler, event, data):
        try:
            return await handler(event, data)
        except TelegramBadRequest as e:
            message = e.message.lower()
            if any(s in message for s in _BENIGN_BAD_REQUEST_SUBSTRINGS):
                logger.warning("Swallowed benign TelegramBadRequest: %s", e.message)
                with suppress(TelegramBadRequest):
                    await event.answer()
                return None
            raise


# Захардкоженный русский текст — то, что человек увидит, если ДАЖЕ i18n.t()
# внутри обработчика ошибок сам бросит исключение (битый каталог, регресс ICU-
# парсера, что угодно). Это последний рубеж продукта: обработчик, который сам
# падает на попытке показать текст о падении, оставляет человека с крутящимся
# спиннером и вообще без единой подсказки — см. _generic_error_text ниже.
_FALLBACK_ERROR_TEXT = "⚠️ Что-то пошло не так — бывает даже у чемпионов. Жми «Меню» внизу — и погнали дальше."
_FALLBACK_MENU_BUTTON_TEXT = "🏠 Меню"
_FALLBACK_FEEDBACK_BUTTON_TEXT = "Сообщить о проблеме"


def _generic_error_text() -> str:
    """Текст последнего рубежа, локализованный — но локализация здесь не может
    быть точкой отказа сама по себе.

    Язык на момент вызова обычно уже верный: `SetUserLanguageMiddleware`
    выставляет его в контекст первой из всех outer_middleware, ДО хендлера,
    который и уронил апдейт, — а обработчик ошибок aiogram вызывается в том
    же asyncio-таске, так что contextvar `i18n.current_lang` из него виден. Но
    полагаться на то, что `i18n.t()` НИКОГДА не бросит исключение, здесь
    нельзя: это и есть код, которому подчищать за любой чужой поломкой,
    включая гипотетическую поломку в самом i18n.py. Если он всё же упадёт —
    человек обязан увидеть хоть что-то, а не второе необработанное исключение
    поверх первого, поэтому здесь свой try/except, а не общий на весь
    обработчик (общий на весь `on_unhandled_error` уже стоит ниже вокруг
    каждого шага отдельно — но он гасит ошибку ПОСЛЕ вызова, когда текст для
    отправки уже нужен).
    """
    try:
        return i18n.t("error.generic")
    except Exception:
        logger.exception("i18n.t упал внутри обработчика ошибок — показываю запасной текст без него")
        return _FALLBACK_ERROR_TEXT


def _back_to_menu_markup() -> InlineKeyboardMarkup:
    """Та же защита, что и у `_generic_error_text`, для подписи кнопки.

    Колбэк — `live:back_to_menu`, тот же, что и у готового «🏠 Меню» на карточке
    законченной тренировки (handlers/workout.py) — он открывает меню, не
    трогая сообщение, с которого пришли, так что подходит и здесь без своего
    отдельного обработчика.

    Вторая кнопка — «Сообщить о проблеме», тот же вход, что у «💬 Отзыв» в
    настройках (handlers.feedback.feedback_open, колбэк `feedback:open`):
    человек только что увидел ошибку, и написать о ней тут же — короче, чем
    искать /feedback самому. Скрыта без ADMIN_ID (config.feedback_available) —
    без него отзыву некуда лететь.
    """
    try:
        label = i18n.t("btn.home_menu")
    except Exception:
        logger.exception("i18n.t упал при подписи кнопки последнего рубежа — беру запасную подпись")
        label = _FALLBACK_MENU_BUTTON_TEXT
    rows = [[InlineKeyboardButton(text=label, callback_data="live:back_to_menu")]]
    if config.feedback_available():
        try:
            feedback_label = i18n.t("btn.report_problem")
        except Exception:
            logger.exception(
                "i18n.t упал при подписи кнопки отзыва последнего рубежа — беру запасную подпись"
            )
            feedback_label = _FALLBACK_FEEDBACK_BUTTON_TEXT
        rows.append([InlineKeyboardButton(text=feedback_label, callback_data="feedback:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _error_chat_id(update) -> int | None:
    """Чат, в который писать про ошибку.

    Именно chat.id, а не reply на исходное сообщение: ui.safe_edit удаляет старый
    экран прямо перед отправкой нового, так что «сообщение уже удалено, а потом
    случилась ошибка» — самый частый расклад, а не редкий край, и reply в нём
    падает сам. Плюс у InaccessibleMessage никакого reply нет вовсе, а chat есть.
    """
    message = None
    if update.callback_query is not None:
        message = update.callback_query.message
    elif update.message is not None:
        message = update.message
    return getattr(getattr(message, "chat", None), "id", None)


async def on_unhandled_error(event: ErrorEvent, bot: Bot | None = None) -> bool:
    """Last-resort net for anything a handler didn't catch — a DB error, a bad
    assumption about FSM data, matplotlib choking on a chart, etc.

    Without this, dp has no dp.errors() handler registered at all: the
    exception is logged by aiogram and nothing else happens. A tapped button's
    callback never gets answered, so Telegram spins it for ~10s and gives up
    silently — no screen change, no message, nothing pointing at what to do.
    In the gym that reads as "the bot is broken", not as a recoverable error.
    """
    logger.exception(
        "Unhandled error processing update %s", event.update.update_id, exc_info=event.exception
    )
    update = event.update
    # Каждый шаг — в своём try: спиннер на кнопке и сообщение в чат нужны
    # независимо друг от друга (алерт мог устареть, сообщение — исчезнуть), а
    # обработчик ошибок, который сам бросил исключение, не показывает человеку
    # ничего — ни экрана, ни подсказки.
    if update.callback_query is not None:
        with suppress(Exception):
            await update.callback_query.answer(_generic_error_text(), show_alert=True)
    # Алерт — всплывашка, она гаснет; сообщение в чате остаётся, и если экран уже
    # удалён, это единственное, от чего человек может оттолкнуться.
    chat_id = _error_chat_id(update)
    if bot is None:
        bot = getattr(update, "bot", None)
    if chat_id is not None and bot is not None:
        with suppress(Exception):
            await bot.send_message(chat_id, _generic_error_text(), reply_markup=_back_to_menu_markup())
    return True


class RefreshPersistentMenuMiddleware(BaseMiddleware):
    """Catches every user up to the latest persistent-keyboard button set on
    their very next interaction with the bot — any text message or button
    tap — rather than only resyncing when they happen to hit /start or the
    Меню button.

    Runs BEFORE the handler, not after. chat_bottom (see that module) tracks
    every message the bot sends and treats the most recently sent one as the
    bottom of the chat; the live workout tracker relies on staying "at
    bottom" to edit in place instead of flickering through delete+resend. If
    this middleware sent its "⌨️ Обновил меню" notice after the handler's own
    reply, that notice would land below the tracker and silently steal its
    bottom spot — invisible on this tap, but on the user's very next tap the
    tracker would find itself no longer at the bottom and pay for a delete
    +resend that had nothing to do with anything the user just did. Sending
    the notice first keeps the handler's own reply as the last word, exactly
    like on every other tap.

    Once a user is confirmed current, their id is cached in memory so later
    taps skip the db.get_user round-trip entirely — the same instance is
    registered for both messages and callbacks (see main()) so the cache is
    shared across both.
    """

    def __init__(self) -> None:
        super().__init__()
        self._up_to_date_ids: set[int] = set()

    async def __call__(self, handler, event, data):
        target = event.message if isinstance(event, CallbackQuery) else event
        if isinstance(target, Message):
            await self._catch_up(event.from_user.id, target)
        return await handler(event, data)

    async def _catch_up(self, user_id: int, target: Message) -> None:
        if user_id in self._up_to_date_ids:
            return
        user = await db.get_user(user_id)
        if user is None:
            return
        if user["reply_keyboard_version"] >= keyboards.PERSISTENT_MENU_VERSION:
            self._up_to_date_ids.add(user_id)
            return
        with suppress(TelegramBadRequest):
            await target.answer(
                i18n.t("main.persistent_menu_refreshed"),
                reply_markup=keyboards.persistent_menu(),
            )
        await db.update_user(user_id, reply_keyboard_version=keyboards.PERSISTENT_MENU_VERSION)
        self._up_to_date_ids.add(user_id)


def setup_routers(dp: Dispatcher) -> None:
    """Register every handler router. Order matters — see the comments below.

    Split out of main() so the routing itself can be tested without starting
    polling: "which router wins this update" is real behaviour, and the share
    deep link has already been broken by it once.
    """
    dp.include_router(persistent_menu.router)
    # admin.router and feedback.router only match their own Command(...) /
    # "admin:"/"feedback:"-prefixed callback data, so it's safe (and necessary)
    # to register them ahead of the FSM flow routers below — otherwise a
    # state's catch-all message handler (e.g. workout.py's logging_set handler)
    # swallows these commands as plain text whenever the user is mid-flow.
    dp.include_router(admin.router)
    dp.include_router(feedback.router)
    # Same reason: /mcp and its own callbacks must reach this router even when
    # the user is parked in some flow's catch-all message handler.
    dp.include_router(mcp_access.router)
    # Same reason: /game — одна команда без состояний, и она должна долетать
    # из любого сценария.
    dp.include_router(game.router)
    # Та же причина: /community — одна команда без состояний, и она нужна из
    # любого сценария, хоть посреди тренировки.
    dp.include_router(community.router)
    # Та же причина, и вдвойне: pre_checkout_query обязан ответить за 10
    # секунд, а successful_payment — реальные деньги, которым нельзя застрять
    # в чужом catch-all'е, если платёж пришёл посреди тренировки или чата с
    # тренером.
    dp.include_router(donate.router)
    # Форвард — самостоятельное действие, а не ответ на вопрос текущего экрана
    # (см. handlers/factcheck.py): должен перехватываться раньше состояний
    # FSM, иначе посреди тренировки или чата с тренером его съел бы их
    # catch-all текстовый обработчик.
    dp.include_router(factcheck.router)
    # Same reason as admin/feedback above: /food_diary and the fd:* callbacks
    # must reach their router even when the user is mid-workout.
    dp.include_router(food_diary.router)
    # Ahead of workout.router on purpose: the share deep link arrives as
    # "/start sh_<token>", and workout's bare Command("start") matches that too
    # — registered after it, the deep link never reaches sharing and the
    # recipient just lands in the main menu with nothing imported.
    dp.include_router(sharing.router)
    dp.include_router(workout.router)
    dp.include_router(routines.router)
    dp.include_router(backfill.router)
    dp.include_router(exercise_resolve.router)
    dp.include_router(csv_import.router)
    dp.include_router(exercises.router)
    dp.include_router(history.router)
    dp.include_router(edit_workout.router)
    dp.include_router(ai_trainer.router)
    dp.include_router(bodyweight.router)
    dp.include_router(settings.router)
    dp.include_router(fallback.router)


def _public_commands(lang: str) -> list[BotCommand]:
    # Тексты — из каталога (locales/*.json, ключи bot.commands.*), а не
    # литералами: их вычитывают вместе с остальными пользовательскими
    # текстами, и расхождение между ru/en видно глазом прямо в JSON.
    commands = [
        BotCommand(command="start", description=i18n.t_in(lang, "bot.commands.start")),
        BotCommand(command="help", description=i18n.t_in(lang, "bot.commands.help")),
        BotCommand(command="ai_trainer", description=i18n.t_in(lang, "bot.commands.ai_trainer")),
        BotCommand(command="food_diary", description=i18n.t_in(lang, "bot.commands.food_diary")),
        BotCommand(command="feedback", description=i18n.t_in(lang, "bot.commands.feedback")),
    ]
    # Только когда MCP реально куда-то ведёт: команда в «/»-меню обещает
    # работающую функцию, а без публичного адреса обещать нечего. /game
    # раздаёт страница того же сервера (см. handlers/game.game_url), так что
    # условие общее.
    if config.mcp_available():
        commands.append(BotCommand(command="mcp", description=i18n.t_in(lang, "bot.commands.mcp")))
        commands.append(BotCommand(command="game", description=i18n.t_in(lang, "bot.commands.game")))
    # Та же логика: команда обещает работающий вход, а без адреса группы вести
    # некуда (см. handlers/community.py).
    if config.community_available():
        commands.append(BotCommand(command="community", description=i18n.t_in(lang, "bot.commands.community")))
    return commands


async def _setup_commands(bot: Bot) -> None:
    """Whose "/" menu lists what — the default list doubles as the bot's
    advertised feature set, so anything reachable from the main menu belongs
    in it.

    Как и bot_profile.sync_bot_profile: Telegram выбирает «/»-меню по
    системному языку клиента, а не по нашей колонке users.lang, и заливается
    это один раз при старте, отдельно на каждый language_code. Вызов БЕЗ
    language_code — дефолт (русский, видят все языки без своего варианта), а
    с language_code="en" — отдельный вариант для англоязычных.
    """
    await bot.set_my_commands(_public_commands("ru"), scope=BotCommandScopeDefault())
    await bot.set_my_commands(_public_commands("en"), scope=BotCommandScopeDefault(), language_code="en")
    if config.ADMIN_ID is not None:
        # Админский список — навсегда по-русски (аудитория: один человек,
        # разработчик), поэтому language_code тут не нужен.
        await bot.set_my_commands(
            [
                *_public_commands("ru"),
                BotCommand(command="check_users", description="Список пользователей (админ)"),
                BotCommand(command="ai_dialogs", description="Диалоги с AI-тренером (админ)"),
                BotCommand(command="pushes", description="Лог отправленных пушей (админ)"),
                BotCommand(command="activity", description="Что делают пользователи (админ)"),
                BotCommand(command="growth", description="Воронка по источникам (админ)"),
                BotCommand(command="broadcast", description="Рассылка всем пользователям (админ)"),
                BotCommand(command="announce", description="Релизный анонс: проверить и разослать (админ)"),
                BotCommand(command="admin_wipe", description="Снести TEST_USER_ID для проверки онбординга (админ)"),
            ],
            scope=BotCommandScopeChat(chat_id=config.ADMIN_ID),
        )


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    if not config.BOT_TOKEN:
        raise RuntimeError("TG_TOKEN env var is not set")

    await db.init_db()

    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(disable_notification=True))
    # Registered before the first API call so the tracker never misses a message:
    # together these two let screens be edited in place while they're still at
    # the bottom of the chat, instead of always being deleted and resent.
    bot.session.middleware(chat_bottom.TrackOutgoingMessages())
    await _setup_commands(bot)
    await bot_profile.sync_bot_profile(bot)
    dp = Dispatcher(storage=JSONFileStorage(config.FSM_STORAGE_PATH))
    dp.errors.register(on_unhandled_error)
    # Первой из всех outer_middleware: она только выставляет i18n.set_lang и
    # никогда не шлёт текст сама, а все следующие middleware (RefreshPersistent
    # MenuMiddleware шлёт «⌨️ Обновил меню», IgnoreStaleCallbackMiddleware может
    # ответить на колбэк) и все хендлеры должны увидеть уже правильный язык.
    set_lang_middleware = SetUserLanguageMiddleware()
    dp.message.outer_middleware(set_lang_middleware)
    dp.callback_query.outer_middleware(set_lang_middleware)
    dp.message.outer_middleware(chat_bottom.TrackIncomingMessages())
    dp.callback_query.outer_middleware(IgnoreStaleCallbackMiddleware())
    # Лог действий — тоже outer: в него должно попадать и то, чего не поймал ни
    # один хендлер (см. activity_log).
    dp.message.outer_middleware(activity_log.LogIncomingMessages())
    dp.callback_query.outer_middleware(activity_log.LogCallbackQueries())
    refresh_menu_middleware = RefreshPersistentMenuMiddleware()
    dp.message.outer_middleware(refresh_menu_middleware)
    dp.callback_query.outer_middleware(refresh_menu_middleware)
    setup_routers(dp)

    def _log_if_task_dies(task: asyncio.Task) -> None:
        """Фоновая задача, умершая от исключения, до сих пор уходила в тишину:
        `create_task` держит результат в себе, никто его не читает — и «бэкапов
        нет вторые сутки» выглядело так же, как «всё работает». Отмена при
        остановке бота — не авария, её пропускаем."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Фоновая задача %s умерла", task.get_name(), exc_info=exc)

    admin_job = asyncio.create_task(admin_tasks.run_daily_admin_jobs(bot))
    backup_watch_job = asyncio.create_task(admin_tasks.run_backup_staleness_check(bot))
    engagement_job = asyncio.create_task(engagement.run_daily_engagement_job(bot))
    # Ретенш-чистка не зависит ни от ADMIN_ID, ни от того, дошёл ли отчёт (см.
    # admin_tasks.run_retention_cleanup_job) — та же причина, по которой
    # прополка OAuth ниже уже вынесена отдельно.
    retention_job = asyncio.create_task(admin_tasks.run_retention_cleanup_job())
    background = [admin_job, backup_watch_job, engagement_job, retention_job]
    # Разовые релизные рассылки: уходят сами после разворота, один раз на
    # человека (отметка о доставке — в базе, см. announcements.py).
    background.append(asyncio.create_task(announcements.run_pending_announcements(bot)))
    # Прополка OAuth не зависит ни от ADMIN_ID, ни от того, дошёл ли отчёт: она
    # чистит коды и заявки, которые копятся от каждой брошенной попытки
    # подключения (см. admin_tasks.run_oauth_purge_job).
    if config.mcp_available():
        background.append(asyncio.create_task(admin_tasks.run_oauth_purge_job()))
    # MCP живёт в том же процессе и на том же event loop, что и поллинг: это
    # один контейнер с одной SQLite-базой на единственном соединении (см. db.py),
    # и второй процесс к ней просто не подключить. Импорт локальный — mcp тянет
    # за собой starlette/uvicorn, и разворот без MCP не должен на них падать.
    if config.mcp_available():
        import mcp_server

        background.append(asyncio.create_task(mcp_server.serve()))
    for task in background:
        task.add_done_callback(_log_if_task_dies)
    try:
        await dp.start_polling(bot)
    finally:
        for task in background:
            task.cancel()
        await db.close_db()


if __name__ == "__main__":
    asyncio.run(main())
