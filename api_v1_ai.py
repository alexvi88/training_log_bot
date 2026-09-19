"""REST `/v1` для AI-тренера (в боте — handlers/ai_trainer.py).

Что выставлено наружу:

- `GET /ai/limits` — дневная квота вопросов текущего пользователя и можно ли
  прямо сейчас задать вопрос (см. `_limits_json`). Чистые данные из
  ai_limits/db, ни одного обращения к модели.
- `POST /ai/ask` — один вопрос → текст ответа плюс всё, чем в боте под ним
  становится клавиатура (см. `_turn_response`): предложенный черновик
  программы (`program`), опросник перед сборкой (`questions`) и упомянутые в
  ответе свои упражнения/программы (`mentions`). Плюс память: перед вызовом
  модели читаем последний сохранённый wire-снимок разговора
  (`db.get_ai_conversation_wire_history`) и передаём его в `ask(..., history=...)`,
  а после успешного ответа сохраняем новый снимок (`db.add_ai_conversation_turn`,
  колбэк `on_wire`). Ядро — тот же `ai_trainer.ask(user_id, question, history, ...)`,
  что вызывает бот (handlers/ai_trainer.py, `_handle_question`): он и так
  принимает голые `user_id`/`question`/`history` без объекта телеграм-сообщения
  — aiogram в ai_trainer.py не импортируется вовсе, — так что рефакторинг
  самого ai_trainer.py не понадобился (тот же приём уже применён в
  api_v1_food.py для `analyze_food`).
- `POST /ai/voice` — голос → расшифрованный текст вопроса, без ответа модели
  (см. докстринг `transcribe_voice`). Транскрипция и лимиты — `api_v1_voice`,
  общий модуль с `POST /workouts/{id}/sets/voice`.
- `POST /ai/program/save` — забрать предложенный черновик программы
  (`program.draft_id` из ответа `/ai/ask`) точно так же, как кнопка «Забрать»
  в боте: запись идёт через ai_program_actions.finalize_program_save — тот же
  код, тот же create_routine_from_program/прогрессии, что и у кнопки
  `ai:prog:save` (handlers/ai_trainer.ai_program_save).
- `POST /ai/questions/answer` — ответить на текущий вопрос опросника
  (`questions.index` из ответа `/ai/ask`) — HTTP-аналог тапа по варианту
  (`ai:qa:`) или «⏭ Пропустить» (`ai:qskip`) в боте. Отдаёт следующий вопрос
  или, когда опросник закончился, реальный ответ модели с составом программы
  — как и `_finish_setup` в боте.
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

Черновик программы и активный опросник — тем же приёмом персистентности:
`ai_program_drafts`/`ai_setup_states` в db.py, HTTP-аналог того, что бот
держит в aiogram FSM (`ai_program_draft`/`ai_setup`). Один черновик и один
опросник на пользователя одновременно — как в FSM, новый затирает старый.

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
чём говорили в другом канале. По той же причине черновик программы/опросник,
собранные в боте, не видны из /ai/ask и наоборот — они лежат в разных
хранилищах ровно как история.

Чего в HTTP-варианте всё ещё НЕТ по сравнению с ботом, и почему:

- **Стриминга.** `on_chunk`/`_DraftStreamer` в боте правят уже отправленное
  сообщение по мере генерации — специфика Telegram. Здесь ответ обычный
  JSON, целиком, одним куском.
- **Предложенных действий** (`on_action` — удалить программу, объединить
  две, поделиться и т.п., см. ai_trainer.ActionCallback). Каждое такое
  действие в боте необратимо и подтверждается отдельным тапом с кнопкой,
  зашитой в конкретное сообщение Telegram; довести его до HTTP-протокола
  (подтверждение конкретного предложенного действия, а не первого попавшегося)
  — отдельная работа, не входившая в эту.
- **Экрана конфликта имени при сохранении программы** (`ai:prog:replace`/
  `ai:prog:copy` в боте) — `POST /ai/program/save` в этом случае просто
  отдаёт 409 `name_conflict`; отдельных маршрутов «заменить»/«копия» пока нет.

Лимиты — ровно та же точка входа, что у бота (`ai_limits.check`), и тот же
порядок: проверка ДО вызова модели, инкремент счётчика ПОСЛЕ успешного
ответа (см. `_run_turn` — тот же приём, что в handlers/ai_trainer.py и в
api_v1_food.py.parse_food: сорвавшийся у провайдера запрос не должен стоить
человеку вопроса).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import ai_limits
import ai_program_actions
import ai_setup_flow
import ai_trainer
import api_v1_common as common
import api_v1_voice
import config
import db
import exercise_mentions
import i18n
import program_mentions

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


# ---------- один ход к модели: /ai/ask и финал /ai/questions/answer ----------


async def _run_turn(user_id: int, question: str, history: list) -> dict[str, Any]:
    """Один вызов ai_trainer.ask() с полным набором колбэков — общее ядро для
    /ai/ask и для «опросник закончился, идём собирать программу»
    (/ai/questions/answer). Ровно та же точка входа и та же квота, что у
    _handle_question в боте, разница только в экране, которого тут нет.

    Проверка лимита — здесь, а не в вызывающих: это ЕДИНСТВЕННОЕ место, где
    HTTP-слой реально идёт к модели, и `ai_limits.check` обязан стоять перед
    каждым из них, включая ход, которым заканчивается опросник (в боте это
    тоже так — `_handle_question` проверяет лимит на каждый свой вызов, а не
    только на первый вопрос пользователя).
    """
    block = await ai_limits.check(user_id, ai_limits.KIND_QUESTION)
    if block is not None:
        raise ApiError(429, "question_limit_exceeded", "daily question limit reached")

    draft_cell: dict[str, Any] = {}
    actions: list[dict[str, Any]] = []
    questions_cell: list[dict[str, Any]] = []
    wire_cell: dict[str, list] = {}

    async def collect_program(draft: dict) -> None:
        draft_cell.clear()
        draft_cell.update(draft)

    async def collect_action(action: dict) -> None:
        if action not in actions:
            actions.append(action)

    async def collect_questions(questions: list) -> None:
        questions_cell.clear()
        questions_cell.extend(questions)

    async def collect_wire(messages: list) -> None:
        wire_cell["messages"] = messages

    try:
        answer = await asyncio.wait_for(
            ai_trainer.ask(
                user_id, question, history=history,
                on_program=collect_program, on_action=collect_action,
                on_questions=collect_questions, on_wire=collect_wire,
            ),
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

    return {"answer": answer, "draft": dict(draft_cell) if draft_cell else None, "questions": questions_cell}


def _program_json(draft_id: str, draft: dict[str, Any]) -> dict[str, Any]:
    """То, что в боте становится кнопками «Забрать: <имя>» + превью с составом
    (см. keyboards.ai_trainer_keyboard/ai_program_preview_keyboard)."""
    return {
        "draft_id": draft_id,
        "name": draft["name"],
        "description": draft.get("description") or None,
        # Готовая подпись кнопки — локализованная, отдаётся под i18n.use_lang
        # вызывающим (см. _turn_response): "Забрать: <имя>", а не голое имя —
        # иначе название программы читается как навигация, а не предложение.
        "label": i18n.t("btn.claim_program", program_name=draft["name"]),
        "replacing": bool(draft.get("replaces")),
        # Один день — тренировка, а не программа: по ней можно пойти прямо
        # сейчас, ничего себе не заводя (см. keyboards.ai_program_preview_keyboard).
        "can_train_now": len(draft["days"]) == 1,
        "days": [
            {
                "name": day["name"],
                "items": [
                    {
                        "name": item["name"],
                        "sets": item.get("sets"),
                        "reps_min": item.get("reps_min"),
                        "reps_max": item.get("reps_max"),
                        "target": item.get("target"),
                        "progression": item.get("progression"),
                    }
                    for item in day["items"]
                ],
            }
            for day in draft["days"]
        ],
        "notes": draft.get("notes") or [],
    }


def _question_json(state: dict[str, Any]) -> dict[str, Any]:
    """Текущий вопрос опросника — то же, что бот показывает отдельным
    сообщением с клавиатурой ai_setup_question_keyboard."""
    idx = int(state.get("idx") or 0)
    questions = state.get("questions") or []
    question = questions[idx]
    return {
        "index": idx,
        "total": len(questions),
        "question": question["question"],
        "choices": question.get("choices") or [],
        # Подпись кнопки «⏭ Пропустить вопрос» — готовая и локализованная,
        # чтобы клиенту не пришлось заводить свой перевод под POST /ai/questions/answer.
        "skip_label": i18n.t("btn.skip_question"),
    }


async def _mentions_json(
    user_id: int, answer: str, exclude_program_name: Optional[str] = None
) -> dict[str, Any]:
    """Упомянутые в ответе свои упражнения и сохранённые программы — то, что в
    боте превращается в карточки-ссылки под ответом (см. handlers/ai_trainer.ai_keyboard,
    exercise_mentions.find_in_text, program_mentions.find_in_text). Логика
    поиска — та же самая, ничего не пересчитываем по-своему.
    """
    mentioned = await exercise_mentions.find_in_text(
        user_id, answer, limit=exercise_mentions.MAX_MENTIONS_TOTAL
    )
    programs = await program_mentions.find_in_text(user_id, answer)
    if exclude_program_name:
        # Та же программа не должна становиться и «Забрать: X», и обычной
        # ссылкой на уже существующую X одновременно (см. ai_keyboard).
        programs = [p for p in programs if p["display_name"] != exclude_program_name]
    return {
        "exercises": [
            {
                "id": ex["id"],
                "display_name": ex["display_name"],
                "is_template": bool(ex["is_template"]),
            }
            for ex in mentioned
        ],
        "programs": [
            {"kind": p["kind"], "id": p["id"], "name": p["display_name"]}
            for p in programs
        ],
    }


async def _next_setup_step(
    user_id: int, questions: list[dict[str, Any]], goal: str, previous: dict[str, Any]
) -> tuple[str, Any]:
    """Что делать со свежесобранным опросником — то же решение, что
    handlers/ai_trainer._deliver_setup принимает для бота, вынесенное в
    ai_setup_flow.py: показать вопросы, доспросить цель первым вопросом или
    закрыть круг и уйти собирать программу на дефолтах.

    Возвращает ("drop", None) — опросника нет, состояние сброшено;
    ("force", (text, state)) — круги кончились, нужно сразу собрать программу
    текстом `text`; ("ask", state) — показать вопросы из state.
    """
    rounds = int(previous.get("rounds") or 0)
    if rounds > ai_setup_flow.SETUP_MAX_ROUNDS:
        # Уже был принудительный заход (см. ветку "force" ниже), и тренер
        # снова просит уточнений — тихо выбрасываем: ещё один круг ничего не
        # даст, а ответ у клиента на экране уже есть.
        return "drop", None
    if rounds >= ai_setup_flow.SETUP_MAX_ROUNDS:
        state = {
            "rounds": rounds + 1, "goal": previous.get("goal"),
            "goal_asked": bool(previous.get("goal_asked")), "questions": [], "answers": [], "idx": 0,
        }
        return "force", (ai_setup_flow.setup_enough_text(previous.get("goal")), state)
    resolved_questions, goal_asked = await ai_setup_flow.questions_with_goal(user_id, questions, previous)
    state = {
        "questions": resolved_questions, "answers": [], "idx": 0, "goal_asked": goal_asked,
        "goal": previous.get("goal") or goal, "rounds": rounds + 1,
    }
    return "ask", state


async def _turn_response(user_id: int, turn: dict[str, Any], goal: str) -> dict[str, Any]:
    """Форма ответа /ai/ask и финала /ai/questions/answer: текст плюс всё, чем
    в боте под ним становится клавиатура. `goal` — исходная просьба
    пользователя (для опросника, см. ai_setup_flow.questions_with_goal)."""
    user = await db.get_user(user_id)
    lang = user["lang"] if user is not None else "ru"

    program_json: Optional[dict[str, Any]] = None
    questions_json: Optional[dict[str, Any]] = None

    with i18n.use_lang(lang):
        if turn["draft"]:
            # Программа и опросник за один ход взаимоисключающи (ровно как в
            # боте — см. handlers/ai_trainer._handle_question): если тренер уже
            # собрал план, спрашивать вводные поздно и незачем.
            await db.clear_ai_setup_state(user_id)
            draft_id = secrets.token_hex(4)
            await db.set_ai_program_draft(user_id, draft_id, turn["draft"])
            program_json = _program_json(draft_id, turn["draft"])
        elif turn["questions"]:
            previous = await db.get_ai_setup_state(user_id) or {}
            kind, payload = await _next_setup_step(user_id, turn["questions"], goal, previous)
            if kind == "drop":
                await db.clear_ai_setup_state(user_id)
            elif kind == "force":
                text, state = payload
                await db.set_ai_setup_state(user_id, state)
                forced_turn = await _run_turn(user_id, text, await db.get_ai_conversation_wire_history(user_id))
                return await _turn_response(user_id, forced_turn, goal=state.get("goal") or goal)
            else:
                state = payload
                await db.set_ai_setup_state(user_id, state)
                questions_json = _question_json(state)
        else:
            await db.clear_ai_setup_state(user_id)

        mentions = await _mentions_json(
            user_id, turn["answer"],
            exclude_program_name=program_json["name"] if program_json else None,
        )

    return {
        "answer": turn["answer"],
        "program": program_json,
        "questions": questions_json,
        "mentions": mentions,
        "limits": await _limits_json(user_id),
    }


async def ask_question(request: Request) -> JSONResponse:
    """Один вопрос тренеру → ответ плюс черновик программы/опросник/упоминания
    (см. `_turn_response`). Порядок ровно как в handlers/ai_trainer.py
    (`_handle_question`): лимит проверяем ДО вызова модели (внутри `_run_turn`),
    счётчик двигаем ПОСЛЕ успешного ответа. Таймаут на весь ход (а не на один
    вызов модели — внутри `ask()` бывает несколько раундов tool-calls) — тот
    же, что у бота: `config.AI_TOTAL_ANSWER_SECONDS`.
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

    # Последний сохранённый wire-снимок разговора этого пользователя — то же
    # самое, что бот держит в ai_history в FSM, только персистентно (см.
    # db.ai_conversation_turns и докстринг модуля). Пусто у нового разговора
    # или сразу после DELETE /ai/history.
    history = await db.get_ai_conversation_wire_history(user_id)
    turn = await _run_turn(user_id, question, history)
    return JSONResponse(await _turn_response(user_id, turn, goal=question))


async def answer_setup_question(request: Request) -> JSONResponse:
    """Ответ на текущий вопрос опросника — HTTP-аналог тапа по варианту
    (`ai:qa:`) или «⏭ Пропустить» (`ai:qskip`) в боте (см.
    handlers/ai_trainer.ai_setup_choice/ai_setup_skip). `answer` — текст
    выбранного варианта или свой текст; отсутствие/пустая строка — пропуск
    вопроса (см. common.optional_str), как «⏭» в боте.

    `question_index` сверяется с текущим индексом опросника, а не берётся на
    веру — та же защита, что и в боте (кнопка под УЖЕ отвеченным вопросом не
    должна записать ответ не туда, см. keyboards.ai_setup_question_keyboard).
    """
    user_id = await common.authed_user_id(request)
    body = await common.json_body(request)
    question_index = common.require(body, "question_index", int)
    answer_text = common.optional_str(body, "answer")

    state = await db.get_ai_setup_state(user_id)
    questions = (state or {}).get("questions") or []
    if state is None or not questions or question_index != int(state.get("idx") or 0):
        raise ApiError(409, "setup_stale", "no active setup question at this index")

    state["answers"] = [*(state.get("answers") or []), answer_text]
    state["idx"] = question_index + 1
    if state["idx"] < len(questions):
        await db.set_ai_setup_state(user_id, state)
        user = await db.get_user(user_id)
        with i18n.use_lang(user["lang"] if user is not None else "ru"):
            questions_json = _question_json(state)
        return JSONResponse({
            "answer": None, "program": None, "questions": questions_json,
            "mentions": {"exercises": [], "programs": []}, "limits": await _limits_json(user_id),
        })

    # Опросник закончился — уходим за программой одним обычным вызовом модели,
    # ровно как _finish_setup в боте.
    user = await db.get_user(user_id)
    with i18n.use_lang(user["lang"] if user is not None else "ru"):
        text = ai_setup_flow.setup_answers_text(state)
    await db.clear_ai_setup_state(user_id)
    history = await db.get_ai_conversation_wire_history(user_id)
    turn = await _run_turn(user_id, text, history)
    return JSONResponse(await _turn_response(user_id, turn, goal=state.get("goal") or text))


async def save_program(request: Request) -> JSONResponse:
    """Забрать предложенный черновик программы — HTTP-аналог кнопки «Забрать»/
    «Добавить себе» в боте (см. handlers/ai_trainer.ai_program_save). Сама
    запись — ai_program_actions.finalize_program_save, тот же код, что и у
    кнопки: свой второй сохранятель здесь не заводится.

    `draft_id` обязателен и сверяется с тем, что реально лежит у ЭТОГО
    пользователя (db.get_ai_program_draft скопирован по telegram_id из
    токена) — черновик другого пользователя тем самым не виден и не
    сохраняем в принципе, а не только по сверке id.
    """
    user_id = await common.authed_user_id(request)
    body = await common.json_body(request)
    draft_id = common.require(body, "draft_id", str)

    draft = await db.get_ai_program_draft(user_id)
    if draft is None or draft.get("id") != draft_id:
        raise ApiError(404, "draft_not_found", "program draft is gone or belongs to a stale answer")

    # Черновик убираем ДО записи (та же атомарность, что у ai_program_save в
    # боте — см. его докстринг): повторный запрос с тем же draft_id уже не
    # найдёт черновика и получит честный 404 вместо повторного сохранения.
    await db.clear_ai_program_draft(user_id)
    try:
        result = await ai_program_actions.finalize_program_save(user_id, draft)
    except Exception as exc:
        logger.exception("AI program save failed for user %s", user_id)
        # Как и в боте: черновик возвращается, чтобы можно было попробовать
        # сохранить ещё раз, не спрашивая тренера заново.
        await db.set_ai_program_draft(user_id, draft_id, draft)
        raise ApiError(500, "save_failed", "failed to save the program") from exc

    if result.get("error") == "budget":
        await db.set_ai_program_draft(user_id, draft_id, draft)
        raise ApiError(409, "routine_budget_exceeded", result["message"])
    if result.get("error") == "name_conflict":
        await db.set_ai_program_draft(user_id, draft_id, draft)
        raise ApiError(409, "name_conflict", f"program name already exists: {result['name']}")

    return JSONResponse({
        "program_id": result["program_id"],
        "day_count": result["day_count"],
        "replacing": result["replacing"],
    })


async def transcribe_voice(request: Request) -> JSONResponse:
    """Голосовой вопрос тренеру → расшифрованный текст, БЕЗ самого ответа.

    Отдельно от `POST /ai/ask`, а не «голос → сразу готовый ответ» одним
    вызовом: бот тоже сперва показывает, что расслышал («🎙 <i>{question}</i>»,
    handlers/ai_trainer.py::ai_voice_question), и лишь потом задаёт вопрос
    модели — так неверно распознанное слово видно и поправимо ДО того, как на
    него потрачен вопрос из дневной квоты. В HTTP-варианте это разделение
    получается бесплатно: клиент показывает расшифровку, даёт её поправить и
    только тогда шлёт обычный `POST /ai/ask` с готовым текстом — без второго
    протокола памяти разговора и без права входа в квоту вопросов мимо
    `/ai/ask` (она проверяется и тратится там же, где и для текстовых
    вопросов, а не здесь).

    Транскрипция, лимиты размера/длительности и формат данных — все в
    `api_v1_voice.transcribe` (общей и с `POST /workouts/{id}/sets/voice»),
    сама расшифровка — `ai_trainer.transcribe_voice`, та же функция, что
    зовёт бот.
    """
    user_id = await common.authed_user_id(request)
    user = await db.get_user(user_id)
    body = await common.json_body(request)
    with i18n.use_lang(user["lang"] if user else "ru"):
        transcript = await api_v1_voice.transcribe(
            body,
            user_id,
            not_configured_message=i18n.t("ai.screen.voice_not_configured"),
            too_long_message=i18n.t("ai.screen.voice_too_long"),
            too_big_message=i18n.t(
                "ai.screen.voice_too_big", mb=api_v1_voice.MAX_VOICE_BYTES // (1024 * 1024)
            ),
            transcribe_failed_message=i18n.t("ai.screen.voice_transcribe_failed"),
        )
        if not transcript:
            raise ApiError(422, "voice_empty", i18n.t("ai.screen.voice_empty"))
    return JSONResponse({"question": transcript})


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
    """«Начать разговор заново» — без него испорченный контекст (модель
    зацепилась не за то в старом ходу) нечем починить. Заодно сбрасывает
    черновик программы и активный опросник — они разговору не переживают
    (тот же приём, что и в боте, где state.clear() после «в меню» убирает и
    ai_history, и ai_program_draft/ai_setup разом)."""
    user_id = await common.authed_user_id(request)
    await db.clear_ai_conversation_history(user_id)
    await db.clear_ai_program_draft(user_id)
    await db.clear_ai_setup_state(user_id)
    return JSONResponse({"cleared": True})


routes = [
    Route("/ai/limits", get_limits, methods=["GET"]),
    Route("/ai/ask", ask_question, methods=["POST"]),
    Route("/ai/voice", transcribe_voice, methods=["POST"]),
    Route("/ai/questions/answer", answer_setup_question, methods=["POST"]),
    Route("/ai/program/save", save_program, methods=["POST"]),
    Route("/ai/history", get_history, methods=["GET"]),
    Route("/ai/history", delete_history, methods=["DELETE"]),
]
