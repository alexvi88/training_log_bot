"""AI-тренер: ответы на вопросы пользователя с доступом к его данным в БД.

Grok (xAI) ходит через OpenAI-совместимый endpoint — тот же стек и те же env
переменные (XAI_API_KEY / GROK_MODEL / GROK_BASE_URL), что и в fun_bot, так что
один ключ обслуживает оба бота. Модель получает три read-only инструмента
(function calling), каждый из которых замкнут на user_id текущего пользователя
на уровне executor'а, поэтому модель физически не может прочитать чужие данные.
"""

import datetime as dt
import json
import logging
from typing import Any, Optional

from openai import AsyncOpenAI

import analytics
import config
import db
from seed_data import EXERCISE_TEMPLATES

logger = logging.getLogger(__name__)

# Сколько раундов tool-calls разрешаем за один вопрос, чтобы цикл не завис.
MAX_TOOL_ROUNDS = 6

# Сколько последних сессий отдаём модели в get_exercise_progress.
PROGRESS_SESSIONS_LIMIT = 10

_client: Optional[AsyncOpenAI] = None


def is_configured() -> bool:
    return bool(config.XAI_API_KEY)


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=config.XAI_API_KEY, base_url=config.GROK_BASE_URL)
    return _client


SYSTEM_PROMPT = """\
Ты — персональный AI-тренер в Telegram-боте для ведения дневника силовых тренировок.
Пользователь логирует в боте тренировки: упражнения, подходы (вес × повторы).

У тебя есть инструменты для чтения данных ЭТОГО пользователя: сводка, последние
тренировки и прогресс по конкретному упражнению. Прежде чем отвечать на вопрос
про его тренировки, нагрузку, прогресс или рекорды — посмотри реальные данные
через инструменты, не выдумывай цифры. Если данных мало или нет, честно скажи об этом.

Также есть инструмент list_exercise_catalog — полный каталог упражнений-шаблонов
бота по группам мышц. Используй его вместе со списком упражнений пользователя
(из get_training_overview), когда советуешь новое упражнение, разбираешь баланс
программы по группам мышц или ищешь, чего не хватает. Предлагай в первую очередь
упражнения из каталога (они уже есть в боте и их можно сразу добавить через
«⚙️ Упражнения → Новое упражнение → Шаблоны»), но можешь называть и упражнения
вне каталога, если это уместно.

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
            "name": "list_recent_workouts",
            "description": (
                "Последние завершённые тренировки: дата, заметка и все подходы "
                "(вес x повторы) по каждому упражнению."
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
]

_CATALOG_BY_GROUP: dict[str, list[str]] = {}
for _group, _name in EXERCISE_TEMPLATES:
    _CATALOG_BY_GROUP.setdefault(_group, []).append(_name)


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


async def execute_tool(user_id: int, name: str, tool_input: dict[str, Any]) -> str:
    if name == "get_training_overview":
        payload = await _training_overview(user_id)
    elif name == "list_recent_workouts":
        limit = tool_input.get("limit") or 5
        limit = max(1, min(int(limit), 10))
        payload = await _recent_workouts(user_id, limit)
    elif name == "get_exercise_progress":
        payload = await _exercise_progress(user_id, tool_input.get("exercise_name", ""))
    elif name == "list_exercise_catalog":
        payload = {"catalog": _CATALOG_BY_GROUP}
    else:
        payload = {"error": f"unknown tool: {name}"}
    return json.dumps(payload, ensure_ascii=False)


# ---------- agentic loop ----------

async def ask(user_id: int, question: str, history: list[dict[str, Any]]) -> str:
    """Один вопрос пользователя → готовый текст ответа.

    history — прошлые реплики диалога в виде [{"role": ..., "content": <str>}]
    (только видимый текст, без tool-сообщений — их таскать между ходами незачем).
    """
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
