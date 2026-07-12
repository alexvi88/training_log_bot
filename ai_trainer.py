"""AI-тренер: ответы на вопросы пользователя с доступом к его данным в БД.

Claude получает три read-only инструмента, каждый из которых замкнут на
user_id текущего пользователя на уровне executor'а, поэтому модель физически
не может прочитать чужие данные. Агентный цикл (запрос → tool_use →
tool_result → …) написан вручную поверх Messages API, чтобы не тянуть
бета-зависимость tool_runner и полностью контролировать историю сообщений.
"""

import datetime as dt
import json
import logging
from typing import Any, Optional

import anthropic

import analytics
import config
import db

logger = logging.getLogger(__name__)

# Сколько раундов tool_use разрешаем за один вопрос, чтобы цикл не завис.
MAX_TOOL_ROUNDS = 6

# Сколько последних сессий отдаём модели в get_exercise_progress.
PROGRESS_SESSIONS_LIMIT = 10

_client: Optional[anthropic.AsyncAnthropic] = None


def is_configured() -> bool:
    return bool(config.ANTHROPIC_API_KEY)


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


SYSTEM_PROMPT = """\
Ты — персональный AI-тренер в Telegram-боте для ведения дневника силовых тренировок.
Пользователь логирует в боте тренировки: упражнения, подходы (вес × повторы).

У тебя есть инструменты для чтения данных ЭТОГО пользователя: сводка, последние
тренировки и прогресс по конкретному упражнению. Прежде чем отвечать на вопрос
про его тренировки, нагрузку, прогресс или рекорды — посмотри реальные данные
через инструменты, не выдумывай цифры. Если данных мало или нет, честно скажи об этом.

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
    # Дата — отдельной строкой в конце, чтобы стабильная часть промпта кэшировалась.
    return SYSTEM_PROMPT + f"\nСегодня {dt.date.today().isoformat()}."


TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_training_overview",
        "description": (
            "Сводка по пользователю: единицы измерения, формула e1RM, статистика "
            "(всего тренировок, за эту неделю, за 30 дней, дней с последней, недельный стрик) "
            "и список его упражнений с числом использований. Вызывай первым, чтобы понять контекст "
            "и узнать точные названия упражнений."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_recent_workouts",
        "description": (
            "Последние завершённые тренировки: дата, заметка и все подходы "
            "(вес x повторы) по каждому упражнению."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Сколько тренировок вернуть, 1-10 (по умолчанию 5)",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_exercise_progress",
        "description": (
            "История одного упражнения: последние сессии (дата, подходы, лучший подход, e1RM, тоннаж), "
            "личные рекорды и тренд e1RM. Название бери точным display_name из get_training_overview."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "exercise_name": {
                    "type": "string",
                    "description": "Точное название упражнения (display_name)",
                }
            },
            "required": ["exercise_name"],
            "additionalProperties": False,
        },
    },
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
    else:
        payload = {"error": f"unknown tool: {name}"}
    return json.dumps(payload, ensure_ascii=False)


# ---------- agentic loop ----------

async def ask(user_id: int, question: str, history: list[dict[str, Any]]) -> str:
    """Один вопрос пользователя → готовый текст ответа.

    history — прошлые реплики диалога в виде [{"role": ..., "content": <str>}]
    (только видимый текст, без tool-блоков — их таскать между ходами незачем).
    """
    client = _get_client()
    messages: list[dict[str, Any]] = [*history, {"role": "user", "content": question}]

    response = None
    for _ in range(MAX_TOOL_ROUNDS + 1):
        response = await client.messages.create(
            model=config.AI_TRAINER_MODEL,
            max_tokens=4096,
            system=_system_prompt(),
            thinking={"type": "adaptive"},
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason != "tool_use":
            break
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            try:
                content = await execute_tool(user_id, block.name, dict(block.input))
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": content}
                )
            except Exception:
                logger.exception("AI trainer tool %s failed", block.name)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "tool failed, answer from what you already have",
                        "is_error": True,
                    }
                )
        messages.append({"role": "user", "content": results})

    if response is None or response.stop_reason == "refusal":
        return "Не могу ответить на этот вопрос. Спроси что-нибудь про твои тренировки 🙂"
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    return text or "Не получилось сформулировать ответ, попробуй переспросить."
