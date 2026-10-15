"""Журнал ходов AI-тренера — чтобы потом перечитать разговоры и оценить
качество ответов (админская `/aichats`, handlers/admin.py).

Пишут оба входа в тренера: бот (handlers/ai_trainer.py, source='bot') и
приложение (api_v1_ai.py, source='app'). Таблица — `db.ai_turn_log`: вопрос,
финальный ответ, модель, имена инструментов и цена хода. Не wire-формат и не
окно контекста: модель отсюда ничего не читает, у бота разговор по-прежнему
живёт в FSM `ai_history`, у приложения — в `ai_conversation_turns`.

Запись — после состоявшегося ответа и никогда не роняет ход: журнал вторичен,
человек свой ответ получить обязан, даже если запись в журнал не удалась.

Разговоры тренера «программа атлету» (coach_program_ai) сюда не пишутся: они
про чужой дневник, и политика обещает, что такой разговор стирается вместе с
доступом или аккаунтом атлета, — копия под id тренера это обещание нарушила бы.

Срок — config.AI_CONVERSATION_RETENTION_DAYS (ночная чистка в
admin_tasks), снос с аккаунтом — сам, по колонке telegram_id.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

import db

logger = logging.getLogger(__name__)

SOURCE_BOT = "bot"
SOURCE_APP = "app"


def tool_names(wire: Optional[Iterable[dict[str, Any]]]) -> list[str]:
    """Имена инструментов, которые модель звала за ПОСЛЕДНИЙ ход wire-снимка —
    всё после последней реплики человека. Без аргументов и результатов: для
    оценки ответа достаточно знать, куда тренер смотрел. Порядок — первого
    вызова, без повторов."""
    messages = list(wire or [])
    start = 0
    for index, message in enumerate(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            start = index + 1
    names: list[str] = []
    for message in messages[start:]:
        if not isinstance(message, dict):
            continue
        for call in message.get("tool_calls") or []:
            function = call.get("function") if isinstance(call, dict) else None
            name = function.get("name") if isinstance(function, dict) else None
            if name and name not in names:
                names.append(str(name))
    return names


async def record_turn(
    user_id: int,
    *,
    source: str,
    question: str,
    answer: str,
    wire: Optional[Iterable[dict[str, Any]]],
    started_at: str,
) -> None:
    """Записать состоявшийся ход. started_at — db.now_iso() перед вызовом
    модели: по нему из cost_events берутся модели и цена этого хода."""
    try:
        costs = await db.ai_turn_costs_since(user_id, started_at)
        tools = tool_names(wire)
        for name in costs["server_tools"]:
            if name not in tools:
                tools.append(name)
        await db.add_ai_turn_log(
            user_id, source, question or "", answer or "",
            model=", ".join(costs["models"]) or None,
            tools=tools,
            cost_usd=round(costs["cost_usd"], 6) if costs["models"] or costs["server_tools"] else None,
        )
    except Exception:
        logger.exception("ai_chat_log: failed to record a %s turn for user %s", source, user_id)
