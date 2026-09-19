"""REST `/v1` для AI-тренера (в боте — handlers/ai_trainer.py).

Что выставлено наружу:

- `GET /ai/limits` — дневная квота вопросов текущего пользователя и можно ли
  прямо сейчас задать вопрос (см. `_limits_json`). Чистые данные из
  ai_limits/db, ни одного обращения к модели.
- `POST /ai/ask` — один вопрос → один текстовый ответ, ТЕПЕРЬ с памятью:
  перед вызовом модели читаем последний сохранённый wire-снимок разговора
  (`db.get_ai_conversation_wire_history`) и передаём его в `ask(..., history=...)`,
  а после успешного ответа сохраняем новый снимок (`db.add_ai_conversation_turn`,
  колбэк `on_wire`). Ядро — тот же `ai_trainer.ask(user_id, question, history, ...)`,
  что вызывает бот (handlers/ai_trainer.py, `_run_ai_turn`): он и так принимает
  голые `user_id`/`question`/`history` без объекта телеграм-сообщения —
  aiogram в ai_trainer.py не импортируется вовсе, — так что рефакторинг
  самого ai_trainer.py не понадобился (тот же приём уже применён в
  api_v1_food.py для `analyze_food`).
- `GET /ai/history` — история для отрисовки чата: только видимая часть
  (роль, текст, время), без wire-формата с tool-calls — клиенту нечего с
  ними делать, а тащить внутренности модели в JSON лишним трафиком незачем.
  Сырой wire-формат остаётся только в БД, для самой модели.
- `DELETE /ai/history` — «начать разговор заново». Без него испорченный
  контекст (модель зацепилась не за то в старом ходу) нечем починить.

Персистентная история — отдельная таблица `ai_conversation_turns` (см.
db.py), НЕ переиспользует `ai_chat_messages`: та — вечный лог для
инструмента модели `get_full_chat_history` (текст-в-текст, без tool-calls,
никогда не подрезается), а эта — рабочее окно контекста, которое подаётся
на вход следующего вопроса и поэтому должно быть маленьким и в
wire-формате (см. docstring таблицы в db.py и MAX_AI_CONVERSATION_TURNS).

**Два разговора одного человека.** Бот по-прежнему хранит свою историю в
aiogram FSM (`ai_history` в state) — это отдельное окно контекста, никак не
связанное с `ai_conversation_turns`. Если один и тот же человек одновременно
пишет тренеру в Telegram-боте и в iOS-приложении, у него буквально два
разных разговора с независимой памятью: сообщение, отправленное в боте, не
попадёт в историю, которую увидит приложение, и наоборот. Это осознанный
компромисс на сейчас (не в рамках этой задачи объединять их — то есть либо
переводить бота на ту же таблицу, либо синхронизировать оба хранилища), а
не забытый баг: пока экран тренера в приложении не выпущен, коллизии не
возникает, а когда выпустится — риск в том, что редкий пользователь двух
каналов сразу заметит нестыковку и удивится, почему тренер «не помнит», о
чём говорили в другом канале.

Чего в HTTP-варианте всё ещё НЕТ по сравнению с ботом, и почему:

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
  HTTP-вызовов было бы отдельным протоколом.
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
import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import ai_limits
import ai_trainer
import api_v1_common as common
import config
import db

logger = logging.getLogger(__name__)

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

    # Последний сохранённый wire-снимок разговора этого пользователя — то же
    # самое, что бот держит в ai_history в FSM, только персистентно (см.
    # db.ai_conversation_turns и докстринг модуля). Пусто у нового разговора
    # или сразу после DELETE /ai/history.
    history = await db.get_ai_conversation_wire_history(user_id)

    # on_wire отдаёт ровно то, что реально уехало модели этим ходом (включая
    # переданную history и всё, что дописалось за раунды tool-calls) — его и
    # сохраняем, а не собранную вручную пару «вопрос-ответ»: половина ценности
    # истории в tool-calls, без них следующий ход заново выяснял бы то же
    # самое (см. докстринг ai_trainer.ask и db.ai_conversation_turns).
    wire_cell: dict[str, list] = {}

    async def collect_wire(messages: list) -> None:
        wire_cell["messages"] = messages

    try:
        answer = await asyncio.wait_for(
            # on_program/on_action/on_questions намеренно не подключены — их
            # некуда девать без экрана бота с инлайн-кнопками (см. докстринг
            # модуля).
            ai_trainer.ask(user_id, question, history=history, on_wire=collect_wire),
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

    wire_messages = wire_cell.get("messages")
    if wire_messages is not None:
        await db.add_ai_conversation_turn(user_id, question, answer, wire_messages)
    else:
        # on_wire не сработал (не должно случаться — ask() зовёт его перед
        # каждым успешным возвратом текста, см. ai_trainer.py), но история
        # разговора дороже промаха кэша: сохраняем то, что можем собрать сами,
        # без tool-calls прошлого хода.
        logger.warning("AI trainer: on_wire didn't fire for user %s, falling back to a plain pair", user_id)
        fallback_wire = history + [
            {"role": "user", "content": question},
            {"role": "assistant", "content": answer},
        ]
        await db.add_ai_conversation_turn(user_id, question, answer, fallback_wire)

    return JSONResponse({"answer": answer, "limits": await _limits_json(user_id)})


async def get_history(request: Request) -> JSONResponse:
    """История для отрисовки чата: только видимая часть (роль/текст/время),
    без wire-формата — клиенту нечего делать с tool-calls модели, и тащить их
    в JSON было бы лишним трафиком и утечкой внутренностей (см. докстринг
    модуля). `limit` считает ХОДЫ (вопрос+ответ), а не отдельные сообщения —
    так же, как хранит их db.ai_conversation_turns.
    """
    user_id = await common.authed_user_id(request)
    limit = common.query_int(
        request, "limit", db.MAX_AI_CONVERSATION_TURNS,
        minimum=1, maximum=db.MAX_AI_CONVERSATION_TURNS,
    )
    turns = await db.get_ai_conversation_history(user_id, limit=limit)
    messages: list[dict[str, Any]] = []
    for row in turns:
        messages.append({"role": "user", "text": row["question"], "created_at": row["created_at"]})
        messages.append({"role": "assistant", "text": row["answer"], "created_at": row["created_at"]})
    return JSONResponse({"messages": messages})


async def delete_history(request: Request) -> JSONResponse:
    """«Начать разговор заново» — без этого испорченный контекст (модель
    зацепилась не за то в старом ходу) нечем починить."""
    user_id = await common.authed_user_id(request)
    await db.clear_ai_conversation_history(user_id)
    return JSONResponse({"cleared": True})


routes = [
    Route("/ai/limits", get_limits, methods=["GET"]),
    Route("/ai/ask", ask_question, methods=["POST"]),
    Route("/ai/history", get_history, methods=["GET"]),
    Route("/ai/history", delete_history, methods=["DELETE"]),
]
