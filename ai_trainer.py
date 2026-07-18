"""AI-тренер: ответы на вопросы пользователя с доступом к его данным в БД.

Grok (xAI) ходит через OpenAI-совместимый endpoint — тот же стек и те же env
переменные (XAI_API_KEY / GROK_MODEL / GROK_BASE_URL), что и в fun_bot, так что
один ключ обслуживает оба бота. Модель получает read-only инструменты
(function calling) поверх данных пользователя, каждый из которых замкнут на
user_id текущего пользователя на уровне executor'а, поэтому модель физически
не может прочитать чужие данные.

Пока у пользователя не исчерпана дневная квота (config.AI_SEARCH_DAILY_LIMIT),
перед основным ответом идёт отдельный шаг через xAI's gRPC "Agent Tools" SDK на
search-модели (config.GROK_SEARCH_MODEL) — ей доступны только server-side
web_search/x_search, без наших DB-инструментов, и она сама решает, нужен ли
вопросу живой поиск. Инструменты приходится разводить по разным вызовам:
смешивание client-side function tools (наши DB-инструменты) с multi-agent
моделью в одном запросе требует xAI beta access, которого у аккаунта нет —
а server-side-only поиск под это ограничение не попадает (так же как в
fun_bot, см. его grok.py, где нет своих function tools вовсе). Реальное
использование поиска считается по citations/server_side_tool_usage в ответе,
а не по факту, что инструмент был просто предложен; если поиск не нужен или
шаг не удался (сеть, rate limit и т.п.), основной ответ просто идёт без
найденного контекста. Основной ответ всегда — обычный REST-вызов с полным
набором DB-инструментов, плюс находки поиска сверху, если они есть.
"""

import asyncio
import datetime as dt
import json
import logging
from typing import Any, Awaitable, Callable, Optional

from openai import AsyncOpenAI
from xai_sdk import AsyncClient as AsyncXAIClient
from xai_sdk.chat import assistant as xai_assistant
from xai_sdk.chat import image as xai_image
from xai_sdk.chat import system as xai_system
from xai_sdk.chat import user as xai_user
from xai_sdk.tools import web_search as xai_web_search
from xai_sdk.tools import x_search as xai_x_search

import analytics
import config
import db
import formatting
import view_builder
from seed_data import EXERCISE_TEMPLATES

logger = logging.getLogger(__name__)

# Сколько раундов tool-calls разрешаем за один вопрос, чтобы цикл не завис.
MAX_TOOL_ROUNDS = 6

# Сколько последних сессий отдаём модели в get_exercise_progress.
PROGRESS_SESSIONS_LIMIT = 10

# Верхняя граница для get_full_workout_history — не по числу тренировок
# пользователя (их может быть сколько угодно), а чтобы один вызов инструмента
# не раздул промпт до неприличия.
FULL_WORKOUT_HISTORY_LIMIT = 200

_client: Optional[AsyncOpenAI] = None


def is_configured() -> bool:
    return bool(config.XAI_API_KEY)


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=config.XAI_API_KEY, base_url=config.GROK_BASE_URL)
    return _client


_audio_client: Optional[AsyncOpenAI] = None


def is_voice_configured() -> bool:
    return bool(config.OPENAI_API_KEY)


def _get_audio_client() -> AsyncOpenAI:
    global _audio_client
    if _audio_client is None:
        _audio_client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
    return _audio_client


async def transcribe_voice(file_obj: Any) -> str:
    """Голосовое сообщение (файл в формате Telegram, OGG/Opus) → распознанный текст.

    file_obj — объект с методом .read() и атрибутом .name (например BytesIO с
    выставленным именем), как того требует OpenAI SDK для audio.transcriptions.
    """
    client = _get_audio_client()
    response = await client.audio.transcriptions.create(
        model=config.OPENAI_TRANSCRIBE_MODEL,
        file=file_obj,
    )
    return (response.text or "").strip()


# AsyncXAIClient owns a grpc.aio channel bound to whatever asyncio loop is
# current at construction time. Building it at import time (before the bot's
# real loop exists) would bind it to the wrong loop, so it's built lazily on
# first use instead — same reasoning as fun_bot's grok.py.
_sdk_client: Optional[AsyncXAIClient] = None
_sdk_client_lock = asyncio.Lock()


async def _get_sdk_client() -> AsyncXAIClient:
    global _sdk_client
    if _sdk_client is None:
        async with _sdk_client_lock:
            if _sdk_client is None:
                _sdk_client = AsyncXAIClient(api_key=config.XAI_API_KEY)
    return _sdk_client


SYSTEM_PROMPT = """\
Ты — персональный AI-тренер в Telegram-боте для ведения дневника силовых тренировок.
Пользователь логирует в боте тренировки: упражнения, подходы (вес × повторы).

Характер: суровый, но поддерживающий опытный тренер, вайб подвальной качалки — прямо,
жёстко и без розовых соплей, но искренне за прогресс пользователя и всегда на его
стороне. При этом реально шаришь за науку, медицину и питание — объясняешь доказательно
и просто, не льстишь и не выдумываешь.

Твоя методика тренировок на гипертрофию, которую советуешь по умолчанию, если
пользователь явно не просит другой подход:
- Рабочий диапазон — 5-10 повторений в подходе.
- Тренировки тяжёлые: RPE 8-9-10 (близко к отказу или до отказа).
- Прогрессия двойная: пока вес держится в диапазоне 5-10 повторений — сначала
  добавляешь повторы на том же весе; как только вышел за верхнюю границу диапазона
  (сделал 10+ повторов) — повышаешь вес и снова начинаешь с нижней границы диапазона.

У тебя есть инструменты для чтения данных ЭТОГО пользователя: сводка, текущая
незавершённая тренировка (если есть), последние завершённые тренировки и прогресс
по конкретному упражнению. Прежде чем отвечать на вопрос про его тренировки,
нагрузку, прогресс или рекорды — посмотри реальные данные через инструменты,
не выдумывай цифры. Если данных мало или нет, честно скажи об этом.

Если пользователь спрашивает про «сегодняшнюю», «текущую» или «эту» тренировку —
сначала вызови get_active_workout. Если он вернул тренировку, она ЕЩЁ НЕ
ЗАВЕРШЕНА (пользователь может продолжить логировать подходы) — так и говори,
не называй её законченной. Если инструмент вернул, что активной тренировки нет,
последняя тренировка из list_recent_workouts уже завершена.

list_recent_workouts отдаёт максимум 10 последних тренировок — этого хватает
для большинства вопросов. Если вопрос требует смотреть за долгий период
(сравнить месяцы, найти конкретную давнюю тренировку, оценить динамику по
многим тренировкам разом) — вызови get_full_workout_history, она отдаёт всю
историю целиком без этого ограничения. Не вызывай её по умолчанию, только
когда реально нужен весь массив, а не последние несколько тренировок.

В подходах может стоять RPE в формате «100x8@9» — это субъективная тяжесть подхода
(9 = почти отказ). Если RPE есть, учитывай его: низкий RPE при застое веса значит, что
пора добавлять нагрузку. В сводке (get_training_overview) может быть последний вес тела
(latest_bodyweight) — используй его для быстрой справки. Если вопрос про динамику веса
тела за период, тренд набора/похудения или сравнение с прошлым — вызови
get_bodyweight_history, она отдаёт всю историю дневника веса, а не только последнюю запись.

Также есть инструмент list_exercise_catalog — полный каталог упражнений-шаблонов
бота по группам мышц. Используй его вместе со списком упражнений пользователя
(из get_training_overview), когда советуешь новое упражнение, разбираешь баланс
программы по группам мышц или ищешь, чего не хватает. Предлагай в первую очередь
упражнения из каталога (они уже есть в боте и их можно сразу добавить через
«⚙️ Упражнения → Новое упражнение → Шаблоны»), но можешь называть и упражнения
вне каталога, если это уместно.

Если среди доступных инструментов есть веб-поиск — используй его для вопросов,
выходящих за рамки личных данных пользователя (актуальные исследования,
рекомендации по питанию/технике, новости фитнес-индустрии и т.п.), а не
выдумывай. Если такого инструмента нет — отвечай по своим знаниям, не
притворяйся, что искал в интернете.

Пользователь иногда присылает фото вместо (или вместе с) текста — например,
скриншот тренировки из другого приложения, фото техники выполнения упражнения,
этикетку продукта или экран трекера. Внимательно посмотри на изображение и
ответь по существу того, что на нём видно; если подписи к фото нет, сам
определи, что от тебя хотят, исходя из содержимого фото.

В контексте разговора тебе передают только последние реплики. Если пользователь
ссылается на что-то более раннее, чего в контексте нет («я тебе говорил про
плечо», «мы это уже обсуждали») — вызови get_full_chat_history, чтобы поднять
всю переписку с ним. Для обычных вопросов про тренировки этот инструмент не
нужен, не вызывай его на всякий случай.

Правила ответа:
- Отвечай по-русски, на «ты» — суровый, но поддерживающий тренер из подвального зала:
  прямо, тепло и с юмором, свой в доску, топишь за пользователя. БЕЗ токсичности,
  сарказма и шейминга (не давишь виной за пропуски, малый объём, лёгкий вес и т.п.) —
  подкалываешь по-доброму, а не унижаешь. Примеры тона (не копируй дословно,
  ориентируйся на манеру):
  «О, вот это дело — +5 кг на **присед**, и без шатания в коленях. Прогресс налицо,
  красава.»
  «О, живой! Соскучился зал по тебе. Ничего, разгонимся — главное вернулся, а не
  сдался.»
- Называй упражнения ТОЧНО так, как они названы в данных (display_name из инструментов)
  — не переводи, не переименовывай, не заменяй своим словом или общепринятым названием.
- Каждый раз, когда называешь конкретное упражнение, оборачивай его название в двойные
  звёздочки, например **pull down** — это единственная разметка, которая разрешена, она
  будет показана жирным. Больше никакого markdown (без ##, без одиночных *, без таблиц) —
  остальной текст уходит в Telegram как есть. Эмодзи и списки с «—» или «•» можно.
- Держи ответ компактным: обычно 3–10 коротких абзацев или пунктов.
- Веса в данных указаны в единицах пользователя (kg/lb — см. сводку); вес 0 значит
  упражнение с собственным весом. e1RM — расчётный разовый максимум.
"""


def _system_prompt() -> str:
    return SYSTEM_PROMPT + f"\nСегодня {dt.date.today().isoformat()}."


SEARCH_SYSTEM_PROMPT = """\
Ты решаешь, нужен ли живой веб/X-поиск для вопроса пользователя AI-тренеру в
Telegram-боте дневника силовых тренировок, и если нужен — сразу его выполняешь.

Используй поиск только для вопросов, которые выходят за рамки личных данных
пользователя: актуальные исследования, рекомендации по питанию/технике, новости
фитнес-индустрии и т.п. Если нашёл что-то по делу — кратко перескажи находки,
указав источники.

Если вопрос не требует живого поиска (например, он про тренировки, прогресс
или данные самого пользователя, или на него можно ответить и без свежих данных
из интернета) — не используй инструменты поиска вообще и ответь ровно одной
строкой, без пояснений:
NO_SEARCH_NEEDED
"""


def _search_system_prompt() -> str:
    return SEARCH_SYSTEM_PROMPT + f"\nСегодня {dt.date.today().isoformat()}."


WORKOUT_COMMENT_SYSTEM_PROMPT = """\
Ты — тот же персональный AI-тренер из Telegram-бота для дневника силовых тренировок:
суровый, но поддерживающий, вайб подвальной качалки, при этом шаришь за науку,
медицину и питание. Придерживаешься гипертрофийной методики: рабочий диапазон
5-10 повторений, тяжёлые подходы (RPE 8-9-10), недельный объём на мышцу 5-12
подходов, двойная прогрессия (сначала повторы в диапазоне, потом вес).

Тебе показывают текстовую карточку одной конкретной, только что завершённой
тренировки пользователя: упражнения, подходы (вес × повторы), сравнение с прошлым
разом и рекорды, если они есть. Напиши короткий комментарий тренера именно по этой
тренировке: похвали по делу, если есть прогресс или рекорд, отметь, что не так
(например, вышел за диапазон повторений — время добавить вес; мало подходов на
мышцу и т.п.), дай максимум один конкретный совет на следующий раз.

Правила:
- По-русски, на «ты», тепло и с юмором, свой в доску — без токсичности, сарказма
  и шейминга (не давишь виной за пропуски, малый объём, лёгкий вес и т.п.).
- Называй упражнения ТОЧНО так, как они написаны в карточке (то название, что до
  «[ГРУППА]») — не переводи, не переименовывай, не заменяй своим словом или
  общепринятым названием. Если упражнение называется «pull down», пиши «pull down»,
  а не «верхняя тяга» или «тяга блока».
- Каждый раз, когда называешь конкретное упражнение, оборачивай его название в
  двойные звёздочки, например **pull down** — это единственная разметка, которая
  разрешена, она будет показана жирным. Больше никакого markdown (без ##, без
  одиночных *, без списков вида `- текст`) — остальной текст уходит в Telegram как есть.
- Компактно: 2-5 коротких абзацев или пунктов, не пересказывай карточку целиком.
- Не выдумывай цифры и факты, которых нет в карточке.
"""


async def comment_on_workout(user_id: int, workout_id: int) -> str:
    """Короткий комментарий тренера по одной конкретной завершённой тренировке.

    Данные тренировки уже отрендерены в текст (та же карточка, что видит
    пользователь) и переданы модели напрямую — агентский цикл с инструментами
    тут не нужен, вопрос строго про один известный workout_id.
    """
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != user_id:
        return "Тренировка не найдена."
    user = await db.get_user(user_id)
    started_at = dt.datetime.fromisoformat(workout["started_at"])
    blocks = await view_builder.build_block_views(
        workout_id, user["e1rm_formula"], previous_before=workout["started_at"]
    )
    duration_seconds = await view_builder.workout_duration_seconds(workout)
    card_text = formatting.build_workout_summary(
        started_at, blocks, workout["note"], show_extra_stats=bool(user["show_extra_stats"]),
        duration_seconds=duration_seconds,
    )

    client = _get_client()
    response = await client.chat.completions.create(
        model=config.GROK_MODEL,
        max_tokens=700,
        messages=[
            {
                "role": "system",
                "content": WORKOUT_COMMENT_SYSTEM_PROMPT + f"\nСегодня {dt.date.today().isoformat()}.",
            },
            {"role": "user", "content": card_text},
        ],
    )
    text = (response.choices[0].message.content or "").strip()
    return text or "Не получилось сформулировать комментарий, попробуй ещё раз позже."


async def ensure_workout_comment(user: Any, workout_id: int) -> Optional[str]:
    """Комментарий тренера к тренировке: уже сохранённый, если есть, иначе — сгенерировать
    и сохранить, но только если у пользователя включены комментарии AI-тренера.

    Возвращает None, если показывать пока нечего (комментарии выключены и ещё не
    запрошены вручную, или AI-тренер не настроен на сервере).
    """
    workout = await db.get_workout(workout_id)
    if workout is None:
        return None
    if workout["ai_comment"]:
        return workout["ai_comment"]
    if not user["ai_comments_enabled"] or not is_configured():
        return None
    comment = await comment_on_workout(user["telegram_id"], workout_id)
    await db.set_workout_ai_comment(workout_id, comment)
    return comment


WEEKLY_DIGEST_SYSTEM_PROMPT = """\
Ты — тот же персональный AI-тренер из Telegram-бота дневника силовых тренировок:
суровый, но поддерживающий, вайб подвальной качалки, шаришь за науку. Методика:
рабочий диапазон 5-10 повторений.

Тебе дают короткую сводку за прошедшую неделю: сколько тренировок и суммарный тоннаж.

Напиши короткий еженедельный дайджест-подведение итогов недели. Правила:
- Начни ровно с «ПРИВЕТ АТЛЕТ, ».
- По-русски, на «ты», тепло и с юмором, свой в доску — без токсичности и шейминга.
- Отметь, что зашло, похвали за тоннаж и число тренировок.
- Дай максимум один конкретный совет на следующую неделю.
- Очень компактно: 3-5 коротких предложений. Без markdown, без списков, без таблиц.
- Не выдумывай цифры, которых нет в сводке.
"""


async def weekly_digest(user_id: int) -> Optional[str]:
    """A short, personalized weekly wrap-up in the coach voice, or None if unavailable.

    One plain completion (no tools) over a compact summary of the week's volume,
    tonnage, and workout count. Used by the engagement job's Sunday digest slot.
    """
    if not is_configured():
        return None
    user = await db.get_user(user_id)
    if user is None:
        return None

    today = dt.date.today()
    dates = [dt.date.fromisoformat(d) for d in await db.list_finished_workout_dates(user_id)]
    dash = analytics.compute_dashboard(dates, today)
    since = (today - dt.timedelta(days=7)).isoformat()
    tonnage = await db.tonnage_since(user_id, since)

    summary = (
        f"Тренировок на этой неделе: {dash.this_week}.\n"
        f"Суммарный тоннаж за 7 дней: {tonnage:.0f} {user['unit']}."
    )

    client = _get_client()
    try:
        response = await client.chat.completions.create(
            model=config.GROK_MODEL,
            max_tokens=500,
            messages=[
                {"role": "system", "content": WEEKLY_DIGEST_SYSTEM_PROMPT},
                {"role": "user", "content": summary},
            ],
        )
    except Exception:
        logger.exception("AI weekly digest generation failed for user %s", user_id)
        return None
    text = (response.choices[0].message.content or "").strip()
    return text or None


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_training_overview",
            "description": (
                "Сводка по пользователю: единицы измерения, формула e1RM, статистика "
                "(всего тренировок, за эту неделю, за 30 дней, дней с последней, недельный стрик) "
                "и список его упражнений с числом использований. Вызывай первым, чтобы понять контекст "
                "и узнать точные названия упражнений."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_active_workout",
            "description": (
                "Текущая незавершённая тренировка пользователя (если он начал её и ещё не "
                "нажал «Завершить»): дата начала и все подходы, уже залогированные по каждому "
                "упражнению. Вызывай, когда пользователь спрашивает про сегодняшнюю/текущую "
                "тренировку. Если активной тренировки нет — вернётся пустой результат, "
                "значит пользователь сейчас не тренируется."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_recent_workouts",
            "description": (
                "Последние завершённые тренировки: дата, заметка и все подходы "
                "(вес x повторы) по каждому упражнению. Для быстрой проверки "
                "последних тренировок — не для вопросов про долгий период "
                "(тут максимум 10 штук), для этого есть get_full_workout_history."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Сколько тренировок вернуть, 1-10 (по умолчанию 5)",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_full_workout_history",
            "description": (
                "Вся история завершённых тренировок пользователя без ограничения "
                "в 10, которое есть у list_recent_workouts: дата, заметка и все "
                "подходы по каждому упражнению для каждой тренировки. Вызывай, "
                "когда вопрос требует смотреть за долгий период — сравнить месяцы, "
                "найти когда что-то было, оценить динамику по многим тренировкам "
                "и т.п. Для быстрых вопросов про последние тренировки достаточно "
                "list_recent_workouts, а для истории одного упражнения — "
                "get_exercise_progress; не вызывай этот инструмент по умолчанию, "
                "ответ может быть большим."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_exercise_progress",
            "description": (
                "История одного упражнения: последние сессии (дата, подходы, лучший подход, e1RM, тоннаж), "
                "личные рекорды и тренд e1RM. Название бери точным display_name из get_training_overview."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "exercise_name": {
                        "type": "string",
                        "description": "Точное название упражнения (display_name)",
                    }
                },
                "required": ["exercise_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_exercise_catalog",
            "description": (
                "Полный каталог упражнений-шаблонов бота, сгруппированный по группам мышц. "
                "Не зависит от пользователя — это готовые упражнения, которые можно добавить "
                "через «⚙️ Упражнения → Новое упражнение → Шаблоны». Используй, чтобы советовать "
                "новые упражнения или находить пробелы по группам мышц, сверяясь со списком "
                "упражнений пользователя из get_training_overview."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_bodyweight_history",
            "description": (
                "Вся история записей веса тела пользователя из дневника веса (⚖️ Дневник веса): "
                "дата и вес каждой записи, в единицах пользователя (unit). get_training_overview "
                "даёт только последнюю запись (latest_bodyweight) — вызывай этот инструмент, когда "
                "нужна динамика веса тела за период, тренд набора/похудения, или сравнение с "
                "прошлым. Если в один день несколько записей — это не ошибка, значит взвешивались "
                "несколько раз в тот день."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_full_chat_history",
            "description": (
                "Полная история переписки с этим пользователем в AI-тренере за всё время — "
                "не только последние реплики, которые уже есть в контексте этого разговора. "
                "Вызывай, только если пользователь ссылается на что-то из более раннего диалога "
                "(«я тебе говорил про травму», «мы это обсуждали»), чего нет в видимом контексте. "
                "Для обычных вопросов про тренировки эта история не нужна — там нужны инструменты "
                "с данными тренировок. Реплики идут в хронологическом порядке с датой."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

# Опциональный колбэк для показа реального прогресса в running-сообщении (см.
# handlers/ai_trainer.py): что сейчас происходит — вместо/вперемешку со
# случайными фразами-заполнителями. Может не приходить (например, в тестах).
StatusCallback = Optional[Callable[[str], Awaitable[None]]]

# Человеко-читаемый статус для каждого инструмента — во что реально идёт вызов,
# а не абстрактное "думаю".
TOOL_STATUS_TEXTS: dict[str, str] = {
    "get_training_overview": "📋 смотрю общую картину по тренировкам...",
    "get_active_workout": "🏋️ проверяю текущую тренировку...",
    "list_recent_workouts": "📒 поднимаю последние тренировки...",
    "get_full_workout_history": "📚 просматриваю всю историю тренировок...",
    "get_exercise_progress": "📈 смотрю прогресс по упражнению...",
    "list_exercise_catalog": "📋 сверяюсь с каталогом упражнений...",
    "get_bodyweight_history": "⚖️ смотрю дневник веса...",
    "get_full_chat_history": "🗂️ поднимаю историю переписки...",
}

_CATALOG_BY_GROUP: dict[str, list[str]] = {}
for _group, _name in EXERCISE_TEMPLATES:
    _CATALOG_BY_GROUP.setdefault(_group, []).append(_name)


def _fmt_set(row: Any) -> str:
    """'100x8' or, when RPE was logged, '100x8@9' — as the model sees a set."""
    base = f"{row['weight']:g}x{row['reps']}"
    rpe = row["rpe"]
    return f"{base}@{rpe:g}" if rpe is not None else base


# ---------- tool executors (все данные строго по user_id) ----------

async def _training_overview(user_id: int) -> dict[str, Any]:
    user = await db.get_user(user_id)
    if user is None:
        return {"error": "user not found"}
    dates = [dt.date.fromisoformat(d) for d in await db.list_finished_workout_dates(user_id)]
    dash = analytics.compute_dashboard(dates, dt.date.today())
    exercises = await db.list_user_exercises(user_id)
    bodyweight = await db.get_latest_bodyweight(user_id)
    return {
        "unit": user["unit"],
        "e1rm_formula": user["e1rm_formula"],
        "latest_bodyweight": (
            {"weight": bodyweight["weight"], "date": bodyweight["logged_at"][:10]} if bodyweight else None
        ),
        "stats": {
            "total_workouts": dash.total_workouts,
            "this_week": dash.this_week,
            "last_30_days": dash.last_30_days,
            "days_since_last": dash.days_since_last,
            "week_streak": dash.week_streak,
        },
        "exercises": [
            {
                "name": ex["display_name"],
                "times_used": ex["usage_count"],
                "last_used_at": ex["last_used_at"],
            }
            for ex in exercises
        ],
    }


async def _active_workout(user_id: int) -> dict[str, Any]:
    workout = await db.get_active_workout(user_id)
    if workout is None:
        return {"active": False}
    exercises = []
    for block in await db.list_blocks_for_workout(workout["id"]):
        block_exs = await db.get_block_exercises(block["id"])
        sets = await db.list_sets_for_block(block["id"])
        if not block_exs or not sets:
            continue
        ex = await db.get_exercise(block_exs[0]["exercise_id"])
        exercises.append(
            {
                "name": ex["display_name"],
                "sets": [_fmt_set(s) for s in sets],
            }
        )
    return {
        "active": True,
        "status": "не завершена — пользователь может ещё логировать подходы",
        "started_at": workout["started_at"][:10],
        "exercises": exercises,
    }


async def _recent_workouts(user_id: int, limit: int) -> dict[str, Any]:
    workouts = await db.list_workouts(user_id, limit=limit)
    result = []
    for w in workouts:
        exercises = []
        for block in await db.list_blocks_for_workout(w["id"]):
            block_exs = await db.get_block_exercises(block["id"])
            sets = await db.list_sets_for_block(block["id"])
            if not block_exs or not sets:
                continue
            ex = await db.get_exercise(block_exs[0]["exercise_id"])
            exercises.append(
                {
                    "name": ex["display_name"],
                    "sets": [_fmt_set(s) for s in sets],
                }
            )
        result.append(
            {
                "date": w["started_at"][:10],
                "note": w["note"],
                "exercises": exercises,
            }
        )
    return {"workouts": result}


async def _exercise_progress(user_id: int, exercise_name: str) -> dict[str, Any]:
    ex = await db.find_exercise_by_display_name(user_id, exercise_name)
    if ex is None:
        candidates = await db.search_exercises(user_id, exercise_name, limit=5)
        return {
            "error": f"exercise '{exercise_name}' not found",
            "did_you_mean": [c["display_name"] for c in candidates],
        }
    user = await db.get_user(user_id)
    rows = await db.list_sets_for_exercise(ex["id"])
    set_rows = [
        analytics.SetRow(r["weight"], r["reps"], r["workout_id"], r["started_at"]) for r in rows
    ]
    sessions = analytics.group_sets_by_session(set_rows)
    for s in sessions:
        s.formula = user["e1rm_formula"]
    records = analytics.compute_personal_records(sessions)
    points = [
        (dt.datetime.fromisoformat(s.started_at), s.top_e1rm) for s in sessions
    ]
    trend = analytics.linear_trend(points)
    return {
        "exercise": ex["display_name"],
        "total_sessions": len(sessions),
        "sessions": [
            {
                "date": s.started_at[:10],
                "sets": [f"{r.weight:g}x{r.reps}" for r in s.sets],
                "top_e1rm": round(s.top_e1rm, 1),
                "tonnage": round(s.tonnage, 1),
            }
            for s in sessions[-PROGRESS_SESSIONS_LIMIT:]
        ],
        "records": {
            "max_weight": records.max_weight,
            "max_e1rm": round(records.max_e1rm, 1),
            "best_e1rm_set": f"{records.best_e1rm_weight:g}x{records.best_e1rm_reps}",
            "max_session_tonnage": round(records.max_session_tonnage, 1),
        },
        "e1rm_trend_per_week": round(trend.slope_per_week, 2) if trend else None,
    }


async def _bodyweight_history(user_id: int) -> dict[str, Any]:
    user = await db.get_user(user_id)
    logs = await db.list_bodyweight_logs(user_id)
    return {
        "unit": user["unit"] if user else "kg",
        "entries": [{"weight": r["weight"], "date": r["logged_at"][:10]} for r in logs],
    }


async def _full_chat_history(user_id: int) -> dict[str, Any]:
    rows = await db.get_ai_chat_history(user_id)
    return {
        "messages": [
            {"role": r["role"], "content": r["content"], "date": r["created_at"][:10]}
            for r in rows
        ]
    }


async def execute_tool(user_id: int, name: str, tool_input: dict[str, Any]) -> str:
    if name == "get_training_overview":
        payload = await _training_overview(user_id)
    elif name == "get_active_workout":
        payload = await _active_workout(user_id)
    elif name == "list_recent_workouts":
        limit = tool_input.get("limit") or 5
        limit = max(1, min(int(limit), 10))
        payload = await _recent_workouts(user_id, limit)
    elif name == "get_full_workout_history":
        payload = await _recent_workouts(user_id, FULL_WORKOUT_HISTORY_LIMIT)
    elif name == "get_exercise_progress":
        payload = await _exercise_progress(user_id, tool_input.get("exercise_name", ""))
    elif name == "list_exercise_catalog":
        payload = {"catalog": _CATALOG_BY_GROUP}
    elif name == "get_bodyweight_history":
        payload = await _bodyweight_history(user_id)
    elif name == "get_full_chat_history":
        payload = await _full_chat_history(user_id)
    else:
        payload = {"error": f"unknown tool: {name}"}
    return json.dumps(payload, ensure_ascii=False)


# ---------- agentic loop ----------

async def ask(
    user_id: int,
    question: str,
    history: list[dict[str, Any]],
    image_data_url: Optional[str] = None,
    on_status: StatusCallback = None,
) -> str:
    """Один вопрос пользователя → готовый текст ответа.

    history — прошлые реплики диалога в виде [{"role": ..., "content": <str>}]
    (только видимый текст, без tool-сообщений — их таскать между ходами незачем).

    image_data_url — опционально, фото, которое пользователь прислал вместе с этим
    вопросом (data: URL, base64). Передаётся только в текущий ход; в history фото
    не попадают — модель не сможет пересмотреть их позже, только вспомнить по тексту.

    on_status — опциональный колбэк, которому по ходу дела шлём текст того, что
    реально сейчас происходит (веб-поиск, конкретный tool-call), чтобы вызывающая
    сторона могла показать это пользователю вместо голого "думаю" (см.
    handlers/ai_trainer.py).

    Пока не исчерпана дневная квота поисковых ответов (config.AI_SEARCH_DAILY_LIMIT),
    перед основным ответом идёт отдельный шаг живого веб/X-поиска (см.
    _web_search_findings) — модель сама решает, нужен ли он вопросу. Найденное (если
    есть) добавляется контекстом к основному REST-вызову с обычными инструментами.
    """
    search_context = None
    if await db.get_ai_search_count_today(user_id) < config.AI_SEARCH_DAILY_LIMIT:
        search_context = await _web_search_findings(user_id, question, history, image_data_url, on_status)
    logger.info(
        "AI trainer question from user %s: %r (web search used: %s)",
        user_id, question, bool(search_context),
    )
    return await _ask_plain(user_id, question, history, image_data_url, search_context, on_status)


def _plain_user_content(question: str, image_data_url: Optional[str]) -> Any:
    if not image_data_url:
        return question
    return [
        {"type": "text", "text": question},
        {"type": "image_url", "image_url": {"url": image_data_url}},
    ]


async def _ask_plain(
    user_id: int,
    question: str,
    history: list[dict[str, Any]],
    image_data_url: Optional[str] = None,
    search_context: Optional[str] = None,
    on_status: StatusCallback = None,
) -> str:
    client = _get_client()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _system_prompt()},
        *history,
    ]
    if search_context:
        messages.append(
            {
                "role": "system",
                "content": f"Результаты живого веб/X-поиска по текущему вопросу пользователя:\n{search_context}",
            }
        )
    messages.append({"role": "user", "content": _plain_user_content(question, image_data_url)})

    message = None
    for _ in range(MAX_TOOL_ROUNDS + 1):
        response = await client.chat.completions.create(
            model=config.GROK_MODEL,
            max_tokens=2048,
            tools=TOOLS,
            messages=messages,
        )
        message = response.choices[0].message
        if not message.tool_calls:
            break
        if on_status:
            status_text = TOOL_STATUS_TEXTS.get(
                message.tool_calls[0].function.name, "🔍 копаюсь в данных..."
            )
            await on_status(status_text)
        messages.append(
            {
                "role": "assistant",
                "content": message.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in message.tool_calls
                ],
            }
        )
        for tc in message.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
                content = await execute_tool(user_id, tc.function.name, args)
            except Exception:
                logger.exception("AI trainer tool %s failed", tc.function.name)
                content = json.dumps(
                    {"error": "tool failed, answer from what you already have"}
                )
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})

    text = (message.content or "").strip() if message else ""
    return text or "Не получилось сформулировать ответ, попробуй переспросить."


def _to_xai_messages(
    history: list[dict[str, Any]],
    question: str,
    image_data_url: Optional[str] = None,
    system_prompt: Optional[str] = None,
) -> list:
    out = [xai_system(system_prompt if system_prompt is not None else _system_prompt())]
    for msg in history:
        content = msg.get("content", "")
        if msg.get("role") == "assistant":
            out.append(xai_assistant(content))
        else:
            out.append(xai_user(content))
    if image_data_url:
        out.append(xai_user(question, xai_image(image_data_url, detail="high")))
    else:
        out.append(xai_user(question))
    return out


async def _web_search_findings(
    user_id: int,
    question: str,
    history: list[dict[str, Any]],
    image_data_url: Optional[str] = None,
    on_status: StatusCallback = None,
) -> Optional[str]:
    """Отдельный шаг перед основным ответом: чистый server-side веб/X-поиск, без
    наших DB-инструментов — их смешивание с multi-agent моделью в одном вызове
    требует xAI beta access, которого у аккаунта нет (см. модуль docstring).
    Возвращает найденное текстом или None, если поиск не нужен вопросу или сам
    шаг не удался — в обоих случаях основной ответ просто идёт без него.

    web_search/x_search — server-side: модель вызывает их и получает результаты
    в рамках одного sample(), без tool_calls, которые нужно было бы исполнять
    самим — поэтому реальное использование поиска видно только постфактум, по
    citations/server_side_tool_usage в ответе. Сам шаг занимает заметное время
    независимо от исхода, поэтому статус про поиск шлём заранее, а не постфактум.
    """
    if on_status:
        await on_status("🔎 ищу свежую информацию в сети...")
    try:
        sdk_client = await _get_sdk_client()
        chat_session = sdk_client.chat.create(
            model=config.GROK_SEARCH_MODEL,
            messages=_to_xai_messages(history, question, image_data_url, system_prompt=_search_system_prompt()),
            tools=[xai_web_search(), xai_x_search()],
            max_tokens=1024,
        )
        response = await chat_session.sample()
    except Exception:
        logger.exception("AI trainer web search step failed, answering without live search")
        return None

    if not (response.citations or response.server_side_tool_usage):
        return None
    await db.increment_ai_search_count(user_id)
    return (response.content or "").strip() or None
