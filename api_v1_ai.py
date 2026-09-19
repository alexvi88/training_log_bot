"""REST `/v1` для AI-тренера (в боте — handlers/ai_trainer.py).

Что выставлено наружу:

- `GET /ai/limits` — дневная квота вопросов текущего пользователя и можно ли
  прямо сейчас задать вопрос (см. `_limits_json`). Чистые данные из
  ai_limits/db, ни одного обращения к модели.
- `POST /ai/ask` — один вопрос → один текстовый ответ. Ядро — тот же
  `ai_trainer.ask(user_id, question, history, ...)`, что вызывает бот
  (handlers/ai_trainer.py, `_run_ai_turn`): он и так принимает голые
  `user_id`/`question`/`history` без объекта телеграм-сообщения — aiogram в
  ai_trainer.py не импортируется вовсе, — так что рефакторинг самого
  ai_trainer.py не понадобился (тот же приём уже применён в api_v1_food.py
  для `analyze_food`).

Чего в HTTP-варианте НЕТ по сравнению с ботом, и почему:

- **Истории диалога.** В боте `history` живёт в aiogram FSM (`ai_history` в
  state) — отдельного хранилища в БД для неё нет. Здесь каждый вызов
  `ask()` уходит с пустой историей: это независимые вопросы, а не диалог с
  памятью. Чтобы дать iOS-клиенту многоходовый диалог, историю сначала нужно
  завести как персистентную сущность (таблица вида `ai_conversations`,
  ключ — user_id, значение — тот же wire-формат сообщений, что сохраняет
  `on_wire` в боте) — это выходит за рамки этой задачи и файлов, которые
  можно трогать.
- **Стриминга.** `on_chunk`/`_DraftStreamer` в боте правят уже отправленное
  сообщение по мере генерации — специфика Telegram. Здесь ответ обычный
  JSON, целиком, одним куском.
- **Черновиков программ и предложенных действий.** `on_program`/`on_action`
  — это заготовки под инлайн-кнопки бота (сохранить программу, удалить
  дубликат и т.п.), которые тапом превращаются в запись в БД. Здесь эти
  колбэки не подключены: `ask()` вернёт текст с описанием программы/действия,
  но не запись, доступную для отдельного подтверждения. Если экран
  «Тренер» должен уметь предлагать и применять такие вещи, это отдельный
  эндпоинт с собственным протоколом (see api_v1_programs.py — она уже
  умеет создавать/удалять программы обычным CRUD, которым можно было бы
  подключить такой tap).
- **Опросника перед сборкой программы** (`on_questions`/`ask_setup_questions`)
  — бот показывает вопросы по одному и копит ответы в FSM между сообщениями;
  здесь это единственный обмен запрос-ответ, копить ответы через несколько
  HTTP-вызовов было бы отдельным протоколом поверх той же истории, которой
  сейчас нет (см. выше).
- **Упоминаний своих упражнений/программ как кнопок** (`exercise_mentions`,
  `ai_keyboard`) — это построение inline-клавиатуры бота, транспортная
  деталь Telegram, а не часть ответа тренера.

Лимиты — ровно та же точка входа, что у бота (`ai_limits.check`), и тот же
порядок: проверка ДО вызова модели, инкремент счётчика ПОСЛЕ успешного
ответа (см. `ask_question` — тот же приём, что в handlers/ai_trainer.py и в
api_v1_food.py.parse_food: сорвавшийся у провайдера запрос не должен стоить
человеку вопроса).
"""

from __future__ import annotations

import asyncio
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import ai_limits
import ai_trainer
import api_v1_common as common
import config
import db

ApiError = common.ApiError

# Вопрос через HTTP не режется телеграмным лимитом сообщения (4096 символов,
# см. handlers/ai_trainer.py DRAFT_TEXT_LIMIT) — клиент может прислать что
# угодно. Свой потолок нужен ради того же, ради чего он нужен боту: без него
# один вопрос на десятки тысяч символов стоит как полноценный разговор и в
# токенах, и в деньгах, а отвечать на него всё равно нечем осмысленным.
MAX_QUESTION_LENGTH = 4000


async def _limits_json(user_id: int) -> dict[str, Any]:
    """Квота вопросов + можно ли прямо сейчас спросить.

    `ai_limits.check` — та же функция, что стоит перед вызовом модели в
    /ai/ask и в боте: если она вернула Block, вопрос сейчас будет отклонён
    (в HTTP — 429, см. `ask_question`), причём даже для «своих» аккаунтов в
    режиме предупреждений (`config.limit_preview_ids`) — у API нет экрана,
    куда показать предупреждение и всё равно пропустить шаг, поэтому здесь,
    как и в api_v1_food.parse_food, любой Block значит «заблокировано», без
    preview-исключения.
    """
    used = await db.get_ai_question_count_today(user_id)
    limit = config.AI_QUESTION_DAILY_LIMIT
    remaining = max(0, limit - used) if limit > 0 else None
    block = await ai_limits.check(user_id, ai_limits.KIND_QUESTION)
    return {
        "question": {
            "used": used,
            # 0 или отрицательное значение лимита в конфиге значит «лимита
            # нет» (см. ai_limits._exhausted) — тем же значением отдаём и
            # клиенту, а не выдумываем отдельный признак unlimited.
            "limit": limit if limit > 0 else None,
            "remaining": remaining,
        },
        "blocked": block is not None,
        "block_reason": block.kind if block is not None else None,
        "configured": ai_trainer.is_configured(),
    }


async def get_limits(request: Request) -> JSONResponse:
    user_id = await common.authed_user_id(request)
    return JSONResponse(await _limits_json(user_id))


async def ask_question(request: Request) -> JSONResponse:
    """Один вопрос тренеру → готовый текст ответа.

    Порядок ровно как в handlers/ai_trainer.py._run_ai_turn: лимит проверяем
    ДО вызова модели, счётчик двигаем ПОСЛЕ успешного ответа. Таймаут на весь
    ход (а не на один вызов модели — внутри `ask()` бывает несколько раундов
    tool-calls) берём тот же, что у бота: `config.AI_TOTAL_ANSWER_SECONDS`.
    """
    user_id = await common.authed_user_id(request)
    if not ai_trainer.is_configured():
        raise ApiError(503, "not_configured", "ai trainer is not configured")

    body = await common.json_body(request)
    question = str(common.require(body, "question", str)).strip()
    if not question:
        raise ApiError(400, "bad_request", "question must not be empty")
    if len(question) > MAX_QUESTION_LENGTH:
        raise ApiError(400, "bad_request", f"question must be at most {MAX_QUESTION_LENGTH} characters")

    block = await ai_limits.check(user_id, ai_limits.KIND_QUESTION)
    if block is not None:
        raise ApiError(429, "question_limit_exceeded", "daily question limit reached")

    try:
        answer = await asyncio.wait_for(
            # history=[] — у HTTP-клиента пока нет персистентной истории
            # диалога, см. докстринг модуля. on_program/on_action/on_questions
            # намеренно не подключены — их некуда девать без экрана бота с
            # инлайн-кнопками (см. докстринг модуля).
            ai_trainer.ask(user_id, question, history=[]),
            timeout=config.AI_TOTAL_ANSWER_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise ApiError(504, "timeout", "ai trainer did not answer in time") from exc
    except Exception as exc:
        raise ApiError(502, "answer_failed", "ai trainer failed to answer") from exc

    # Квота тратится за состоявшийся ответ, как и в боте (db.try_increment_ai_question_count
    # уже сам не даёт перескочить лимит атомарным UPDATE ... WHERE count < limit)
    # — сбой провайдера выше уже вернул бы 502/504 и до сюда не дошёл.
    await db.try_increment_ai_question_count(user_id, config.AI_QUESTION_DAILY_LIMIT)

    return JSONResponse({"answer": answer, "limits": await _limits_json(user_id)})


routes = [
    Route("/ai/limits", get_limits, methods=["GET"]),
    Route("/ai/ask", ask_question, methods=["POST"]),
]
