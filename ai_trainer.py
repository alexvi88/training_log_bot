"""AI-тренер: ответы на вопросы пользователя с доступом к его данным в БД.

Grok (xAI) ходит через OpenAI-совместимый endpoint — тот же стек и те же env
переменные (XAI_API_KEY / GROK_MODEL / GROK_BASE_URL), что и в fun_bot, так что
один ключ обслуживает оба бота. Модель получает read-only инструменты
(function calling) поверх данных пользователя, каждый из которых замкнут на
user_id текущего пользователя на уровне executor'а, поэтому модель физически
не может прочитать чужие данные.

Пока у пользователя не исчерпана дневная квота (config.AI_SEARCH_DAILY_LIMIT),
вопрос идёт через xAI's gRPC "Agent Tools" SDK на search-модели
(config.GROK_SEARCH_MODEL) с тем же набором функций плюс server-side
web_search/x_search — модель сама решает по ходу ответа, нужен ли ей живой
поиск (например, по актуальным исследованиям/рекомендациям), так же как в
fun_bot (см. его grok.py). Реальное использование поиска считается по
citations/server_side_tool_usage в ответе, а не по факту, что инструмент был
просто предложен. После исчерпания квоты бот тихо возвращается к обычному
REST-вызову без поиска — вопрос про его собственные тренировки эту разницу
не почувствует, инструменты для БД доступны в обоих режимах.
"""

import asyncio
import datetime as dt
import json
import logging
from typing import Any, Optional

from openai import AsyncOpenAI
from xai_sdk import AsyncClient as AsyncXAIClient
from xai_sdk.chat import assistant as xai_assistant
from xai_sdk.chat import system as xai_system
from xai_sdk.chat import tool as xai_tool
from xai_sdk.chat import tool_result as xai_tool_result
from xai_sdk.chat import user as xai_user
from xai_sdk.tools import web_search as xai_web_search
from xai_sdk.tools import x_search as xai_x_search

import analytics
import config
import db
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

В контексте разговора тебе передают только последние реплики. Если пользователь
ссылается на что-то более раннее, чего в контексте нет («я тебе говорил про
плечо», «мы это уже обсуждали») — вызови get_full_chat_history, чтобы поднять
всю переписку с ним. Для обычных вопросов про тренировки этот инструмент не
нужен, не вызывай его на всякий случай.

Правила ответа:
- Отвечай по-русски, на «ты», дружелюбно и по делу, как тренер в зале.
- Пиши обычным текстом без markdown-разметки (без **, ##, таблиц) — ответ уходит
  в Telegram как есть. Эмодзи и списки с «—» или «•» можно.
- Держи ответ компактным: обычно 3–10 коротких абзацев или пунктов.
- Вопросы про здоровье/боль: дай общий совет и напомни, что с болью — к врачу.
- Веса в данных указаны в единицах пользователя (kg/lb — см. сводку); вес 0 значит
  упражнение с собственным весом. e1RM — расчётный разовый максимум.
"""


def _system_prompt() -> str:
    return SYSTEM_PROMPT + f"\nСегодня {dt.date.today().isoformat()}."


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

_CATALOG_BY_GROUP: dict[str, list[str]] = {}
for _group, _name in EXERCISE_TEMPLATES:
    _CATALOG_BY_GROUP.setdefault(_group, []).append(_name)

# Same tool set as TOOLS above, expressed in xai_sdk's function-tool format
# (used when the question gets web-search access — see ask()/_ask_with_search).
_XAI_TOOLS = [
    xai_tool(name=t["function"]["name"], description=t["function"]["description"], parameters=t["function"]["parameters"])
    for t in TOOLS
]


# ---------- tool executors (все данные строго по user_id) ----------

async def _training_overview(user_id: int) -> dict[str, Any]:
    user = await db.get_user(user_id)
    if user is None:
        return {"error": "user not found"}
    dates = [dt.date.fromisoformat(d) for d in await db.list_finished_workout_dates(user_id)]
    dash = analytics.compute_dashboard(dates, dt.date.today())
    exercises = await db.list_user_exercises(user_id)
    return {
        "unit": user["unit"],
        "e1rm_formula": user["e1rm_formula"],
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
                "sets": [f"{s['weight']:g}x{s['reps']}" for s in sets],
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
                    "sets": [f"{s['weight']:g}x{s['reps']}" for s in sets],
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
    elif name == "get_full_chat_history":
        payload = await _full_chat_history(user_id)
    else:
        payload = {"error": f"unknown tool: {name}"}
    return json.dumps(payload, ensure_ascii=False)


# ---------- agentic loop ----------

async def ask(user_id: int, question: str, history: list[dict[str, Any]]) -> str:
    """Один вопрос пользователя → готовый текст ответа.

    history — прошлые реплики диалога в виде [{"role": ..., "content": <str>}]
    (только видимый текст, без tool-сообщений — их таскать между ходами незачем).

    Пока не исчерпана дневная квота поисковых ответов (config.AI_SEARCH_DAILY_LIMIT),
    вопрос идёт через search-модель с доступом к web/X-поиску впридачу к обычным
    инструментам — модель сама решает, нужен ли ей живой поиск. Иначе — обычный
    REST-вызов с теми же инструментами, но без поиска.
    """
    search_allowed = await db.get_ai_search_count_today(user_id) < config.AI_SEARCH_DAILY_LIMIT
    if search_allowed:
        return await _ask_with_search(user_id, question, history)
    return await _ask_plain(user_id, question, history)


async def _ask_plain(user_id: int, question: str, history: list[dict[str, Any]]) -> str:
    client = _get_client()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _system_prompt()},
        *history,
        {"role": "user", "content": question},
    ]

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


def _to_xai_messages(history: list[dict[str, Any]], question: str) -> list:
    out = [xai_system(_system_prompt())]
    for msg in history:
        content = msg.get("content", "")
        if msg.get("role") == "assistant":
            out.append(xai_assistant(content))
        else:
            out.append(xai_user(content))
    out.append(xai_user(question))
    return out


async def _ask_with_search(user_id: int, question: str, history: list[dict[str, Any]]) -> str:
    sdk_client = await _get_sdk_client()
    chat_session = sdk_client.chat.create(
        model=config.GROK_SEARCH_MODEL,
        messages=_to_xai_messages(history, question),
        tools=[xai_web_search(), xai_x_search(), *_XAI_TOOLS],
        max_tokens=2048,
    )

    response = None
    for _ in range(MAX_TOOL_ROUNDS + 1):
        response = await chat_session.sample()
        chat_session.append(response)
        if not response.tool_calls:
            break
        for tc in response.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
                content = await execute_tool(user_id, tc.function.name, args)
            except Exception:
                logger.exception("AI trainer tool %s failed", tc.function.name)
                content = json.dumps(
                    {"error": "tool failed, answer from what you already have"}
                )
            chat_session.append(xai_tool_result(content, tool_call_id=tc.id))

    # web_search/x_search are server-side: the model calls them and gets
    # results within a single sample() round, never surfacing as a tool_call
    # we need to fulfill — so whether search actually ran is only visible
    # after the fact, via citations/server_side_tool_usage on the final answer.
    used_search = response is not None and (bool(response.citations) or bool(response.server_side_tool_usage))
    if used_search:
        await db.increment_ai_search_count(user_id)

    text = (response.content or "").strip() if response is not None else ""
    return text or "Не получилось сформулировать ответ, попробуй переспросить."
