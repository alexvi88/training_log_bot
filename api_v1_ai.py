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
  api_v1_food.py для `analyze_food`). Необязательный `image_data_url` в теле —
  тот же приём, что фото-вопрос в боте (`ai_photo_question`): `question` можно
  не присылать вовсе (как пустую подпись к фото), тогда уходит тот же
  дефолтный вопрос, что и в боте (`ai.screen.default_photo_question`).
- `POST /ai/voice` — голос → расшифрованный текст вопроса, без ответа модели
  (см. докстринг `transcribe_voice`). Транскрипция и лимиты — `api_v1_voice`,
  общий модуль с `POST /workouts/{id}/sets/voice`.
- `POST /ai/video` — ролик подхода → разбор техники (Qwen3-VL,
  `video_analysis.analyze`) и сразу ответ тренера по нему, одним вызовом (см.
  докстринг `ask_video`, чем это отличается от двух платных шагов в боте).
  Упражнение — своим `exercise_id` (владение проверяется, как и везде в
  `/v1`) или угаданное из `caption` тем же `handlers.ai_trainer._exercise_from_caption`,
  что и у бота.
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
- `GET /ai/pending` — незавершённое состояние разговора: черновик программы
  (`program`, тем же JSON, что и в ответе `/ai/ask` — с `draft_id`, чтобы
  «Забрать себе» было чем вызвать) и/или текущий неотвеченный вопрос
  опросника (`questions`, тем же JSON, что и там же). Оба уже лежат в БД
  (`ai_program_drafts`/`ai_setup_states`) ради `/ai/program/save` и
  `/ai/questions/answer` — этой ручки не хватало только чтобы клиент мог их
  ПРОЧИТАТЬ, а не только записать ответ на них. Отдельная ручка, а не поле в
  `GET /ai/history`: у истории свой контракт («роль/текст/время» реплик, см.
  выше) и свои читатели (бесконечная лента чата) — вклинивать в неё разовое
  состояние «что сейчас висит под последней репликой» означало бы менять её
  форму всем, кому нужен только текст, ради потребности одного конкретного
  экрана. Упоминания (`mentions` из `/ai/ask`) сюда сознательно не попадают:
  это ссылки на слова уже видимого текста ответа, а не отдельное состояние
  сервера, и без текста, из которого их искать (find_in_text), взять их
  неоткуда — раз текст уже виден в истории, потерянные при уходе с экрана
  кликабельные ссылки на него не стоят отдельной ручки.
- `DELETE /ai/history` — «начать разговор заново». Без него испорченный
  контекст (модель зацепилась не за то в старом ходу) нечем починить.
- `GET /ai/thinking` — фразы для плейсхолдера «тренер думает», пока идёт
  `/ai/ask`: тот же пул, что крутится в боте, подобранный под тему вопроса
  (`running_texts.pool_for`), плюс период ротации. Классификация темы остаётся
  на сервере — клиенту незачем знать про стемы и темы (см. `get_thinking`).

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

**Двойной платёж от параллельных запросов.** Этот порядок (проверка лимита
ДО ответа модели, счётчик ПОСЛЕ) сам по себе не защищает от гонки: два
одновременных `POST /ai/ask` (двойной тап на клиенте, ретрай при плохой
сети) оба читают ЕЩЁ не увеличенный счётчик, оба проходят `ai_limits.check`
и оба уходят в модель — двойной платёж, а дневная квота вопросов может быть
превышена на число проскочивших так запросов. Ровно этот инцидент уже был у
бота (см. `handlers/ai_trainer._try_claim_busy`), и защита от него — тот же
приём: неблокирующий busy-замок на пользователя (`_busy` в этом модуле,
общий примитив в busy_lock.py), застолбленный ДО обращения к модели и снятый
в `finally` при любом исходе. Второй параллельный запрос получает не 5xx и
не тихий сбой, а тот же самый отказ, что видит атлет в боте при двойном тапе
(`ai.screen.busy`, здесь — 429 `busy`).
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
import busy_lock
import config
import db
import exercise_mentions
import i18n
import program_mentions
import running_texts
import video_analysis
from handlers import ai_trainer as ai_trainer_handlers
from handlers.ai_trainer import MAX_IMAGE_BYTES

logger = logging.getLogger(__name__)

ApiError = common.ApiError

# Тот же замок, что у бота — один платный AI-шаг на пользователя в этом
# модуле одновременно (см. busy_lock.py и handlers/ai_trainer._busy, у
# которого тот же набор один на весь чат тренера — текст, фото, голос,
# выбор упражнения для видео). Раньше HTTP-слой был единственным местом, где
# `ai_limits.check` читал счётчик, ещё не знающий о текущем запросе, а
# инкремент (`db.try_increment_ai_question_count`/`db.increment_ai_video_count`)
# шёл ПОСЛЕ ответа модели: N параллельных `POST /ai/ask`/`/ai/video`/
# `/ai/questions/answer` (двойной тап, ретрай клиента — без всякого злого
# умысла) означали N настоящих платежей, и дневной лимит их не останавливал,
# ровно как в инциденте у бота.
#
# Один набор на все три маршрута (не отдельный на `/ai/video`) — по той же
# причине, что и у бота: `/ai/video` заканчивается ровно тем же ходом к
# модели, что и `/ai/ask` (см. `_run_turn`), только начинается с ещё одного
# платного шага (Qwen3-VL) перед ним, а `/ai/questions/answer` в своей
# последней реплике — это тот же `_run_turn` без разбора видео. Разводить их
# по разным замкам означало бы, что видео-разбор и текстовый вопрос одного и
# того же человека могут идти параллельно, хотя оба тратят один и тот же
# дневной счётчик вопросов.
#
# Своя, отдельная от бота копия набора (не `ai_trainer_handlers._busy`) — у
# HTTP и Telegram уже разные окна разговора и разные хранилища черновика/
# опросника (см. докстринг модуля выше, «Два разговора одного человека»):
# логично, что и in-flight-бронь у них раздельная, а не блокирует один канал
# из-за активности в другом.
_busy: set[int] = set()

# Вопрос через HTTP не режется телеграмным лимитом сообщения (4096 символов,
# см. handlers/ai_trainer.py DRAFT_TEXT_LIMIT) — клиент может прислать что
# угодно. Свой потолок нужен ради того же, ради чего он нужен боту: без него
# один вопрос на десятки тысяч символов стоит как полноценный разговор и в
# токенах, и в деньгах, а отвечать на него всё равно нечем осмысленным.
MAX_QUESTION_LENGTH = 4000

# Фото к вопросу тренеру: те же MIME, что реально бывают на телефоне (JPEG с
# камеры/из галереи, PNG со скриншота, WebP из некоторых галерей). Бот такого
# списка не заводит — Telegram сам ужимает photo в JPEG до того, как файл
# доедет до бота, — а HTTP-клиент шлёт то, что реально лежит на диске.
IMAGE_EXTENSION_BY_MIME = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}

# Видео разбора техники: то же самое, что реально пишут телефоны — MP4
# (Android, и то, что после экспорта пишет iOS) и QuickTime/.mov (сырой формат
# камеры iPhone, если приложение не перекодирует перед отправкой).
VIDEO_EXTENSION_BY_MIME = {
    "video/mp4": "mp4",
    "video/quicktime": "mov",
    "video/webm": "webm",
}


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


async def _run_turn(
    user_id: int,
    question: str,
    history: list,
    *,
    image_data_url: Optional[str] = None,
    video_context: Optional[str] = None,
) -> dict[str, Any]:
    """Один вызов ai_trainer.ask() с полным набором колбэков — общее ядро для
    /ai/ask, /ai/video и для «опросник закончился, идём собирать программу»
    (/ai/questions/answer). Ровно та же точка входа и та же квота, что у
    _handle_question в боте, разница только в экране, которого тут нет.

    image_data_url/video_context — опциональные вложения текущего хода (фото
    к вопросу или наблюдения по видео, см. ai_trainer.ask). Передаются в
    ask() только когда заданы, а не всегда голым None: test_api_v1_ai.py
    проверяет ТОЧНЫЙ набор колбэков в kwargs у обычного текстового вопроса, и
    лишний ключ там — уже другой контракт.

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

    ask_kwargs: dict[str, Any] = {}
    if image_data_url is not None:
        ask_kwargs["image_data_url"] = image_data_url
    if video_context is not None:
        ask_kwargs["video_context"] = video_context

    try:
        answer = await asyncio.wait_for(
            ai_trainer.ask(
                user_id, question, history=history,
                on_program=collect_program, on_action=collect_action,
                on_questions=collect_questions, on_wire=collect_wire,
                **ask_kwargs,
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


def _validate_image_data_url(data_url: str, *, too_big_message: str) -> None:
    """Проверить формат и настоящий размер фото-вложения (см.
    common.decode_data_url и докстринг api_v1_voice.py — тот же приём: JSON +
    data: URL, а не multipart). Саму строку возвращать незачем — она уходит
    ai_trainer.ask() как есть, декодируем только чтобы измерить честные байты
    после base64, а не поверить длине JSON-поля."""
    raw, _mime, _ext = common.decode_data_url(data_url, IMAGE_EXTENSION_BY_MIME, field="image_data_url")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ApiError(400, "photo_too_big", too_big_message)


def _claim_turn_or_429(user_id: int, lang: str) -> None:
    """Застолбить `_busy` для этого хода к модели или честно отказать —
    HTTP-аналог `ai.screen.busy` у бота ("Секунду, ещё думаю над прошлым
    вопросом"), тем же текстом и той же мыслью: второй платный вызов того же
    человека, пришедший, пока первый ещё летит, не должен состояться вообще,
    а не просто не засчитаться в квоту (см. `_busy` выше).

    429, а не 409: с точки зрения HTTP это тот же "слишком часто, попробуй
    чуть позже" смысл, что и у прочих дневных лимитов этого модуля
    (question_limit_exceeded/video_limit_exceeded), а не конфликт состояния.

    Вызывающая сторона обязана снять бронь в `finally`
    (`_busy.discard(user_id)`) при любом исходе — исключении, таймауте или
    обычном успехе, иначе один упавший запрос блокирует человеку весь день
    (см. busy_lock.py)."""
    if not busy_lock.try_claim(_busy, user_id):
        with i18n.use_lang(lang):
            raise ApiError(429, "busy", i18n.t("ai.screen.busy"))


async def ask_question(request: Request) -> JSONResponse:
    """Один вопрос тренеру → ответ плюс черновик программы/опросник/упоминания
    (см. `_turn_response`). Порядок ровно как в handlers/ai_trainer.py
    (`_handle_question`): лимит проверяем ДО вызова модели (внутри `_run_turn`),
    счётчик двигаем ПОСЛЕ успешного ответа. Таймаут на весь ход (а не на один
    вызов модели — внутри `ask()` бывает несколько раундов tool-calls) — тот
    же, что у бота: `config.AI_TOTAL_ANSWER_SECONDS`.

    `image_data_url` — необязательное фото к вопросу, тот же сценарий, что
    `ai_photo_question` в боте («что это за тренажёр», «посмотри на мою
    технику»). При фото без текста `question` можно не присылать вовсе —
    как пустая подпись к фото в Telegram, — тогда уходит тот же дефолтный
    вопрос, что и у бота.
    """
    user_id = await common.authed_user_id(request)
    if not ai_trainer.is_configured():
        raise ApiError(503, "not_configured", "ai trainer is not configured")

    body = await common.json_body(request)
    image_data_url = common.optional_str(body, "image_data_url")
    user = await db.get_user(user_id)
    lang = user["lang"] if user is not None else "ru"

    with i18n.use_lang(lang):
        if image_data_url is not None:
            _validate_image_data_url(
                image_data_url,
                too_big_message=i18n.t("ai.screen.photo_too_big", mb=MAX_IMAGE_BYTES // (1024 * 1024)),
            )
        question = common.optional_str(body, "question") or ""
        if not question:
            if image_data_url is None:
                raise ApiError(400, "bad_request", "question must not be empty")
            # Фото без подписи — ровно как ai_photo_question в боте.
            question = i18n.t("ai.screen.default_photo_question")
    if len(question) > MAX_QUESTION_LENGTH:
        raise ApiError(400, "bad_request", f"question must be at most {MAX_QUESTION_LENGTH} characters")

    _claim_turn_or_429(user_id, lang)
    try:
        # Последний сохранённый wire-снимок разговора этого пользователя — то
        # же самое, что бот держит в ai_history в FSM, только персистентно
        # (см. db.ai_conversation_turns и докстринг модуля). Пусто у нового
        # разговора или сразу после DELETE /ai/history.
        history = await db.get_ai_conversation_wire_history(user_id)
        turn = await _run_turn(user_id, question, history, image_data_url=image_data_url)
        return JSONResponse(await _turn_response(user_id, turn, goal=question))
    finally:
        _busy.discard(user_id)


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
    # ровно как _finish_setup в боте. Тот же платный ход, что у /ai/ask —
    # значит и та же бронь `_busy` вокруг него (см. `_claim_turn_or_429`):
    # опросник заканчивается ровно одним ответом модели, отличается только
    # тем, каким текстом его попросили.
    user = await db.get_user(user_id)
    lang = user["lang"] if user is not None else "ru"
    with i18n.use_lang(lang):
        text = ai_setup_flow.setup_answers_text(state)
    await db.clear_ai_setup_state(user_id)
    _claim_turn_or_429(user_id, lang)
    try:
        history = await db.get_ai_conversation_wire_history(user_id)
        turn = await _run_turn(user_id, text, history)
        return JSONResponse(await _turn_response(user_id, turn, goal=state.get("goal") or text))
    finally:
        _busy.discard(user_id)


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
    зовёт бот. Whisper берёт деньги за саму расшифровку независимо от того,
    дойдёт ли дело до `/ai/ask` — значит и это платный шаг, и на него та же
    бронь `_busy`, что и на сам вопрос: два параллельных `POST /ai/voice`
    одного человека иначе оба уходят в Whisper одновременно.
    """
    user_id = await common.authed_user_id(request)
    user = await db.get_user(user_id)
    lang = user["lang"] if user else "ru"
    body = await common.json_body(request)
    _claim_turn_or_429(user_id, lang)
    try:
        with i18n.use_lang(lang):
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
    finally:
        _busy.discard(user_id)


def _decode_video_data_url(data_url: str, *, too_big_message: str) -> tuple[bytes, str]:
    """Формат и настоящий размер видео-вложения — тот же приём, что у фото
    выше и у голоса (api_v1_voice), см. common.decode_data_url. В отличие от
    фото, video_analysis.analyze() хочет сырые байты и mime отдельно (не
    целую data: URL), поэтому раскодированное и возвращаем."""
    raw, mime, _ext = common.decode_data_url(data_url, VIDEO_EXTENSION_BY_MIME, field="video_data_url")
    if len(raw) > config.MAX_VIDEO_BYTES:
        raise ApiError(400, "video_too_heavy", too_big_message)
    return raw, mime


async def _resolve_video_exercise_hint(user_id: int, body: dict[str, Any]) -> Optional[str]:
    """Какое упражнение подсказать разбору — то же решение, что в боте, двумя
    путями (см. handlers/ai_trainer.py::ai_video_exercise_chosen/_exercise_from_caption):

    - `exercise_id` — явный выбор своим упражнением (HTTP-аналог тапа по
      кнопке `aivid:ex:` в боте). Проверка владения обязательна: id угадывается,
      а разбор чужим упражнением-подсказкой был бы утечкой чужого каталога.
    - `caption` — угадывается из подписи по каталогу ЭТОГО пользователя той же
      функцией, что и у бота, а не своей копией: правило «подпись это не
      обязательно название упражнения» (см. её докстринг) иначе разъедется
      между ботом и API при следующей правке одного из них.
    """
    exercise_id = common.optional_int(body, "exercise_id")
    if exercise_id is not None:
        exercise = await db.get_exercise(exercise_id)
        if exercise is None or exercise["user_id"] != user_id:
            raise ApiError(404, "not_found", "exercise not found")
        return exercise["display_name"]
    caption = common.optional_str(body, "caption")
    if caption:
        return await ai_trainer_handlers._exercise_from_caption(user_id, caption)
    return None


async def ask_video(request: Request) -> JSONResponse:
    """Видео подхода → разбор техники и сразу ответ тренера по нему.

    В боте это два платных шага одного сценария: `video_analysis.analyze`
    (глаза, Qwen3-VL) и затем `ai_trainer.ask` с `video_context` (голос,
    Grok) — см. `handlers/ai_trainer.py::_analyze_video_and_answer`. Здесь оба
    шага за один HTTP-запрос, а не два отдельных маршрута, как у голоса: у
    голоса разделение окупается — расшифровку можно поправить ДО того, как
    потрачен вопрос из квоты (см. докстринг `transcribe_voice`). Результат
    разбора видео клиент не редактирует и вообще не видит как текст, значит
    разделение только развело бы во времени два списания квоты (video и
    question) без единой пользы взамен.

    Квоты — video, потом question, ДО единого байта разбора: тот же порядок,
    что в `ai_video_question` в боте (см. её докстринг) — платный разбор не
    должен стартовать ради ответа, который квота вопросов всё равно не
    пропустит. preview (свои аккаунты в режиме предупреждений) здесь не
    отличается от настоящего блока: у API нет экрана, куда показать
    предупреждение и всё равно пропустить шаг (тот же выбор, что и у
    `_run_turn`/`_limits_json` для вопросов, см. докстринг модуля).
    """
    user_id = await common.authed_user_id(request)
    user = await db.get_user(user_id)
    lang = user["lang"] if user is not None else "ru"

    if not config.video_analysis_available():
        with i18n.use_lang(lang):
            raise ApiError(503, "not_configured", i18n.t("ai.screen.video_not_available_text"))
    if not ai_trainer.is_configured():
        raise ApiError(503, "not_configured", "ai trainer is not configured")

    body = await common.json_body(request)

    with i18n.use_lang(lang):
        duration = body.get("duration_seconds")
        if duration is not None:
            if not isinstance(duration, (int, float)) or isinstance(duration, bool):
                raise ApiError(400, "bad_request", "duration_seconds must be a number")
            if duration > config.MAX_VIDEO_SECONDS:
                raise ApiError(
                    400, "video_too_long",
                    i18n.t("ai.screen.video_too_long", seconds=config.MAX_VIDEO_SECONDS),
                )

        data_url = common.require(body, "video_data_url", str)
        raw, mime = _decode_video_data_url(
            data_url,
            too_big_message=i18n.t(
                "ai.screen.video_too_heavy", mb=config.MAX_VIDEO_BYTES // (1024 * 1024)
            ),
        )

    # Бронь — ДО обеих проверок лимита и уж тем более до самого разбора: два
    # параллельных запроса с одним и тем же видео (двойной тап, ретрай) иначе
    # оба прошли бы `ai_limits.check` (он читает счётчик, ещё не знающий о
    # текущем запросе) и оба заплатили бы за Qwen3-VL и за Grok — ровно та
    # гонка, что описана у `_busy` выше.
    _claim_turn_or_429(user_id, lang)
    try:
        block = await ai_limits.check(user_id, ai_limits.KIND_VIDEO)
        if block is not None:
            raise ApiError(429, "video_limit_exceeded", "daily video analysis limit reached")
        block = await ai_limits.check(user_id, ai_limits.KIND_QUESTION)
        if block is not None:
            raise ApiError(429, "question_limit_exceeded", "daily question limit reached")

        exercise_hint = await _resolve_video_exercise_hint(user_id, body)
        caption = common.optional_str(body, "caption") or ""

        analysis = await video_analysis.analyze(raw, user_id, mime_type=mime, exercise_hint=exercise_hint)
        if analysis is None:
            with i18n.use_lang(lang):
                raise ApiError(502, "video_analysis_failed", i18n.t("ai.screen.video_analysis_failed"))

        # Квота видео тратится за состоявшийся разбор — как и в боте
        # (db.increment_ai_video_count сразу после успешного analyze, до
        # вопроса тренеру, см. _analyze_video_and_answer). Сбой уже вернул бы
        # 502 выше.
        await db.increment_ai_video_count(user_id)

        with i18n.use_lang(lang):
            asked = caption or (
                i18n.t("ai.screen.analyze_technique", hint=exercise_hint) if exercise_hint else ""
            )
            question = asked or i18n.t("ai.screen.default_video_question")

        history = await db.get_ai_conversation_wire_history(user_id)
        turn = await _run_turn(
            user_id, question, history,
            video_context=video_analysis.to_context_block(analysis),
        )
        return JSONResponse(await _turn_response(user_id, turn, goal=question))
    finally:
        _busy.discard(user_id)


async def get_pending_state(request: Request) -> JSONResponse:
    """Незавершённое состояние разговора — то, что должно висеть карточкой под
    последней репликой тренера, но само по себе не текст и потому не попадает
    в `GET /ai/history` (см. докстринг модуля). Ушёл с экрана и вернулся —
    ровно это клиент и должен перерисовать поверх подтянутой истории.

    Черновик и опросник взаимоисключающи и в БД (см. `_turn_response`: конец
    хода с программой чистит `ai_setup_states`, и наоборот) — но здесь это не
    проверяется отдельно, а просто отдаётся оба поля как есть: если оба вдруг
    пусты, ответ — `{"program": None, "questions": None}`, и это не ошибка, а
    нормальное «сейчас ничего не висит» (у бота в этом случае под последним
    сообщением просто нет клавиатуры).
    """
    user_id = await common.authed_user_id(request)
    user = await db.get_user(user_id)
    lang = user["lang"] if user is not None else "ru"

    draft = await db.get_ai_program_draft(user_id)
    setup_state = await db.get_ai_setup_state(user_id)

    with i18n.use_lang(lang):
        program_json = _program_json(draft["id"], draft) if draft else None
        # Пустой список вопросов — теоретически невозможное, но не проверяемое
        # здесь состояние БД (см. _next_setup_step): не рисовать несуществующий
        # вопрос лучше, чем упасть на IndexError из _question_json.
        questions_json = (
            _question_json(setup_state)
            if setup_state and setup_state.get("questions")
            else None
        )

    return JSONResponse({"program": program_json, "questions": questions_json})


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


async def get_thinking(request: Request) -> JSONResponse:
    """Фразы для плейсхолдера «тренер думает», пока клиент ждёт `/ai/ask`.

    В боте эти фразы крутятся в placeholder-сообщении (handlers/ai_trainer.py,
    `_RunningDisplay.cycle_idle`), а приложение показывало статичное «тренер
    печатает…» — и на длинном вопросе с tool-calls это выглядело зависшим.

    Тему угадываем здесь же (`running_texts.classify` через `pool_for`), а не на
    клиенте: стемы тем живут в одном месте и правятся вместе с пулами, дублировать
    их в приложении значило бы разъехаться с ботом на первой же новой теме. Отдаём
    ВЕСЬ пул темы в порядке каталога и интервал — тасует и крутит клиент сам,
    каждый запрос к модели не стоит отдельного похода за фразой.

    Язык — как у остальных ручек модуля: из users.lang под `i18n.use_lang`, а не
    из языка самого вопроса и не из Accept-Language (вопрос может быть на любом
    языке, плейсхолдер должен звучать на языке интерфейса — см. докстринг
    running_texts.pool_for).
    """
    user_id = await common.authed_user_id(request)
    question = request.query_params.get("q") or ""
    user = await db.get_user(user_id)
    lang = user["lang"] if user is not None else "ru"

    # classify язык не смотрит (стемы обоих языков в одном списке), а pool_for —
    # смотрит, отсюда и use_lang вокруг него.
    topic = running_texts.classify(question)
    with i18n.use_lang(lang):
        texts = running_texts.pool_for(question)

    return JSONResponse({
        "topic": topic,
        "texts": texts,
        "interval_seconds": running_texts.RUNNING_INTERVAL,
    })


routes = [
    Route("/ai/limits", get_limits, methods=["GET"]),
    Route("/ai/ask", ask_question, methods=["POST"]),
    Route("/ai/voice", transcribe_voice, methods=["POST"]),
    Route("/ai/video", ask_video, methods=["POST"]),
    Route("/ai/questions/answer", answer_setup_question, methods=["POST"]),
    Route("/ai/program/save", save_program, methods=["POST"]),
    Route("/ai/pending", get_pending_state, methods=["GET"]),
    Route("/ai/history", get_history, methods=["GET"]),
    Route("/ai/history", delete_history, methods=["DELETE"]),
    Route("/ai/thinking", get_thinking, methods=["GET"]),
]
