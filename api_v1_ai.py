"""REST `/v1` для AI-тренера (в боте — handlers/ai_trainer.py).

Что выставлено наружу:

- `GET /ai/limits` — дневная квота вопросов текущего пользователя и можно ли
  прямо сейчас задать вопрос (см. `_limits_json`). Чистые данные из
  ai_limits/db, ни одного обращения к модели.
- `POST /ai/ask` — один вопрос → текст ответа плюс всё, чем в боте под ним
  становится клавиатура (см. `_turn_response`): предложенный черновик
  программы (`program`), опросник перед сборкой (`questions`), кнопки отката
  того, что тренер записал этим ходом (`actions`, см. `POST /ai/undo`), и
  упомянутые в ответе свои упражнения/программы (`mentions`). Плюс память: перед вызовом
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
  `ai:prog:save` (handlers/ai_trainer.ai_program_save). Необязательный
  `on_conflict` ("replace"/"copy") — HTTP-аналог экрана конфликта имени
  (`ai:prog:replace`/`ai:prog:copy` в боте, см. `_resolve_conflict`); «отклонить»
  — это просто не звать ручку повторно, отдельного маршрута под неё не завели.
- `POST /ai/undo` — «↩️ Отменить» то, что тренер сделал сам: вес, запись еды,
  созданное или переименованное упражнение, копию программы, правку профиля.
  В теле — `key` из `actions[]` ответа `/ai/ask` (или `/ai/pending`), сам
  откат применяет общий с ботом `ai_undo.apply`. Ручка появилась потому, что
  тренер про эту кнопку ПИШЕТ в тексте ответа («Если мимо — отменишь кнопкой
  ниже», `_UNDO_NOTE` в ai_trainer.py) независимо от того, кто спросил: в
  Telegram кнопка была, а в приложении обещание было пустым.
- `POST /ai/program/train` — «▶️ Начать тренировку» по несохранённому
  черновику из одного дня (`ai:prog:train` в боте, см. `train_from_draft`):
  тренировка стартует прямо из плана, без создания программы.
- `POST /ai/questions/answer` — ответить на текущий вопрос опросника
  (`questions.index` из ответа `/ai/ask`) — HTTP-аналог тапа по варианту
  (`ai:qa:`) или «⏭ Пропустить» (`ai:qskip`) в боте. Отдаёт следующий вопрос
  или, когда опросник закончился, реальный ответ модели с составом программы
  — как и `_finish_setup` в боте.
- `GET /ai/history` — история ТЕКУЩЕГО разговора для отрисовки чата: только
  видимая часть (роль, текст, время), без wire-формата с tool-calls — клиенту
  нечего с ними делать, а тащить внутренности модели в JSON лишним трафиком
  незачем. Сырой wire-формат остаётся только в БД, для самой модели.
- `DELETE /ai/history` — «начать новый разговор». Ходы больше не стираются:
  разговор уезжает в архив на чтение, а новый начинается пустым (см.
  db.start_new_ai_conversation).
- `GET /ai/conversations` и `GET /ai/conversations/{id}` — список прошлых
  разговоров (заголовок = первый вопрос, даты, число ходов) и один разговор
  целиком, той же формой, что `GET /ai/history`. Только чтение: продолжить
  архивный разговор нельзя, `POST /ai/ask` всегда пишет в текущий. Архив
  живёт config.AI_CONVERSATION_RETENTION_DAYS (чистит ночной джоб
  admin_tasks._run_retention_cleanup), текущий разговор чистка не трогает.
- `GET /ai/pending` — незавершённое состояние разговора: черновик программы
  (`program`, тем же JSON, что и в ответе `/ai/ask` — с `draft_id`, чтобы
  «Забрать себе» было чем вызвать), текущий неотвеченный вопрос опросника
  (`questions`, тем же JSON, что и там же) и живые кнопки отката под
  последним ходом (`actions`, чтобы «Отменить» пережило уход с экрана). Оба уже лежат в БД
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
import os
import secrets
from typing import Any, Optional

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

import ai_limits
import ai_program_actions
import ai_setup_flow
import ai_trainer
import ai_undo
import api_v1_common as common
import api_v1_voice
import busy_lock
import chat_attachments
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

async def _require_ai_consent(request: Request, user_id: int) -> None:
    """403 `ai_consent_required`, если человек не разрешил передавать данные
    стороннему AI (App Store 5.1.2(i), users.ai_consent_at).

    Зовут только ручки, которые реально отдают что-то модели: вопрос (текст и
    фото, xAI), ответ на опросник (последний ответ — ход xAI), голос (OpenAI,
    расшифровка) и видео (Novita, потом xAI). «Забрать»/«Начать тренировку» по
    черновику, откат, история и лимиты ничего наружу не шлют — закрывать их
    незачем, а человек, отозвавший согласие, должен иметь возможность забрать
    уже предложенную программу.

    Проверка работает, только если клиент прислал `X-AI-Consent-Flow: 1` (то
    есть сам показывает лист) или включён config.AI_CONSENT_REQUIRED. Выпущенные
    сборки 1.0 (3)/(4) листа не знают, и 403 превратил бы им тренера в
    непонятную ошибку — см. комментарий у флага. Главная проверка — в
    приложении, до отправки; эта ловит расхождение (согласие отозвано, а экран
    ещё помнит старое) и сборку, в которой какую-то точку отправки забыли.
    Бот в Telegram сюда не ходит вовсе.
    """
    if not (
        config.AI_CONSENT_REQUIRED
        or request.headers.get(config.AI_CONSENT_CLIENT_HEADER) == "1"
    ):
        return
    user = await db.get_user(user_id)
    if user is not None and user["ai_consent_at"]:
        return
    raise ApiError(403, "ai_consent_required", "consent to share data with the AI provider is required")


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

# Content-Type для вложений истории чата (chat_attachments.py) по
# расширению файла на диске — свой маленький словарь, а не общесистемный
# mimetypes.guess_type, ровно как у api_v1_media._CONTENT_TYPES: набор
# расширений тут фиксирован (IMAGE_EXTENSION_BY_MIME + FRAME_EXTENSION).
_IMAGE_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
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
    saved_image_path: Optional[str] = None,
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

    saved_image_path — имя файла в config.AI_CHAT_MEDIA_DIR (см.
    chat_attachments.py), которое уходит в db.add_ai_conversation_turn вместе
    с этим ходом, чтобы GET /ai/history мог отдать картинку. Не то же самое,
    что image_data_url: этот параметр только записывается в историю, самой
    модели ничего из него не уходит (для этого image_data_url).

    Проверка лимита — здесь, а не в вызывающих: это ЕДИНСТВЕННОЕ место, где
    HTTP-слой реально идёт к модели, и `ai_limits.check` обязан стоять перед
    каждым из них, включая ход, которым заканчивается опросник (в боте это
    тоже так — `_handle_question` проверяет лимит на каждый свой вызов, а не
    только на первый вопрос пользователя).
    """
    block = await ai_limits.check(user_id, ai_limits.KIND_QUESTION)
    if block is not None:
        raise ApiError(429, "question_limit_exceeded", "daily question limit reached", human=block.user_text)

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

    # Язык хода — из users.lang, а не из ContextVar по умолчанию: у HTTP-запроса
    # нет middleware бота, и без use_lang ai_trainer._with_language_tail дописал
    # бы в системный промпт «Отвечай ТОЛЬКО по-русски» атлету с английским
    # телефоном, а подписи кнопок отката (ai_undo, i18n.t внутри инструментов)
    # ушли бы по-русски. wait_for заводит задачу уже внутри with, а задача
    # копирует контекст в момент создания — язык доезжает до всех инструментов.
    user = await db.get_user(user_id)
    lang = user["lang"] if user is not None else i18n.DEFAULT_LANG
    try:
        with i18n.use_lang(lang):
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
        turn_id = await db.add_ai_conversation_turn(
            user_id, question, answer, wire_messages, image_path=saved_image_path
        )
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
        turn_id = await db.add_ai_conversation_turn(
            user_id, question, answer, fallback_wire, image_path=saved_image_path
        )

    return {
        "answer": answer,
        "draft": dict(draft_cell) if draft_cell else None,
        "questions": questions_cell,
        "actions": await _store_undo_actions(user_id, turn_id, actions),
    }


async def _store_undo_actions(
    user_id: int, turn_id: int, actions: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """Сохранить откаты этого хода и вернуть то, что уедет клиенту кнопками.

    Инструменты тренера, которые пишут в базу, отдают описание отката вместе с
    подписью кнопки (см. ai_undo.py), и модель в тексте ответа на эту кнопку
    прямо ссылается («Если мимо — отменишь кнопкой ниже»). До этой ручки
    `collect_action` их собирал и выбрасывал: в Telegram кнопка была, в
    приложении текст про неё был, а кнопки не было.

    Берём только откаты (`undo`). Остальные действия тренера — письмо
    разработчику (`feedback`) и предложение архивировать пачку упражнений
    (`archive_ids`) — это не откат уже сделанного, а предложение сделать
    что-то новое, и каждому нужна своя ручка со своим подтверждением; пока их
    нет, молча пропускаем, а не отдаём кнопку, которую нечем нажать.

    Несколько откатов за ход складываются в один, ровно как в боте
    (`_fold_undo_actions`): просьба «удали всё из дневника еды» — это два
    десятка вызовов с двумя десятками откатов, и двадцать кнопок под одним
    ответом никому не помогают. Порядок применения — обратный, им занимается
    `ai_undo.apply` для `kind="batch"`.
    """
    undos = [a for a in actions if a.get("undo") is not None]
    if not undos:
        return []
    if len(undos) > 1:
        user = await db.get_user(user_id)
        with i18n.use_lang(user["lang"] if user is not None else "ru"):
            label = i18n.t("ai.screen.actions_folded_label", n=len(undos))
        items = [(label, {"kind": "batch", "items": [a["undo"] for a in undos]})]
    else:
        items = [(undos[0]["label"], undos[0]["undo"])]
    return await db.add_ai_undo_actions(user_id, turn_id, items)


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
        # Кнопки отката того, что тренер сделал этим ходом (см.
        # `_store_undo_actions` и `POST /ai/undo`). Пустой список — обычное
        # дело: большинство ответов ничего не пишет и откатывать нечего.
        "actions": turn.get("actions") or [],
        "mentions": mentions,
        "limits": await _limits_json(user_id),
    }


def _validate_image_data_url(data_url: str, *, too_big_message: str) -> tuple[bytes, str]:
    """Проверить формат и настоящий размер фото-вложения (см.
    common.decode_data_url и докстринг api_v1_voice.py — тот же приём: JSON +
    data: URL, а не multipart) и вернуть (байты, расширение) — их сохраняет
    вызывающий в chat_attachments.save_photo, чтобы фото пережило уход с
    экрана чата (см. докстринг chat_attachments.py). Строка самой data: URL
    уходит ai_trainer.ask() отдельно, как и раньше — этот разбор только
    измеряет честные байты после base64, а не поверить длине JSON-поля."""
    raw, _mime, ext = common.decode_data_url(
        data_url,
        IMAGE_EXTENSION_BY_MIME,
        field="image_data_url",
        max_bytes=MAX_IMAGE_BYTES,
        too_big_error=(400, "photo_too_big", too_big_message),
    )
    return raw, ext


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
            raise ApiError(429, "busy", "another request is in flight", key="ai.screen.busy")


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
    await _require_ai_consent(request, user_id)
    if not ai_trainer.is_configured():
        raise ApiError(503, "not_configured", "ai trainer is not configured")

    body = await common.json_body(request)
    image_data_url = common.optional_str(body, "image_data_url")
    user = await db.get_user(user_id)
    lang = user["lang"] if user is not None else "ru"

    # Сохраняем ДО обращения к модели, не после: если ask() упадёт или
    # ответ не уложится в таймаут, до db.add_ai_conversation_turn дело не
    # дойдёт вовсе (см. _run_turn), а файл на диске — мелкая, отдельная от
    # хода операция, которой нет смысла зависеть от исхода вопроса тренеру.
    #
    # Сбой самого сохранения (диск, права, ФС только для чтения) — тоже не
    # повод не отвечать: картинка в истории чата — удобство сверху, а не
    # то, ради чего вопрос вообще задают. Молча остаёмся без неё, как и при
    # неудачной вытяжке кадра из видео (chat_attachments.save_video_frame).
    #
    # Но только после ВСЕХ проверок тела: раньше фото ложилось на диск до
    # проверки длины вопроса, и 400 «слишком длинно» оставлял файл, о котором
    # никто не знает по имени (сносит его только except ниже, до которого 400
    # не доходил).
    saved_image_path: Optional[str] = None
    with i18n.use_lang(lang):
        raw_image: Optional[tuple[bytes, str]] = None
        if image_data_url is not None:
            raw_image = _validate_image_data_url(
                image_data_url,
                too_big_message=i18n.t("ai.screen.photo_too_big", mb=MAX_IMAGE_BYTES // (1024 * 1024)),
            )
        question = common.optional_str(body, "question") or ""
        if not question:
            if image_data_url is None:
                raise ApiError(400, "bad_request", "question must not be empty", key="api.error.text_empty")
            # Фото без подписи — ровно как ai_photo_question в боте.
            question = i18n.t("ai.screen.default_photo_question")
    if len(question) > MAX_QUESTION_LENGTH:
        raise ApiError(
            400, "bad_request", f"question must be at most {MAX_QUESTION_LENGTH} characters",
            key="api.error.text_too_long", max=MAX_QUESTION_LENGTH,
        )

    # Бронь — тоже до сохранения: 429 busy из _claim_turn_or_429 стоит вне
    # try ниже и файл за собой не убрал бы.
    _claim_turn_or_429(user_id, lang)
    try:
        if raw_image is not None:
            try:
                saved_image_path = chat_attachments.save_photo(user_id, *raw_image)
            except Exception:
                logger.exception("chat photo save failed for user %s", user_id)
        # Последний сохранённый wire-снимок разговора этого пользователя — то
        # же самое, что бот держит в ai_history в FSM, только персистентно
        # (см. db.ai_conversation_turns и докстринг модуля). Пусто у нового
        # разговора или сразу после DELETE /ai/history.
        history = await db.get_ai_conversation_wire_history(user_id)
        turn = await _run_turn(
            user_id, question, history,
            image_data_url=image_data_url, saved_image_path=saved_image_path,
        )
        return JSONResponse(await _turn_response(user_id, turn, goal=question))
    except Exception:
        # Ход не состоялся (лимит, таймаут, сбой модели) — до
        # db.add_ai_conversation_turn дело не дошло, и файл на диске никто
        # не будет знать по имени: сносим сами, иначе это утечка на каждый
        # неудачный фото-вопрос.
        if saved_image_path is not None:
            chat_attachments.delete(saved_image_path)
        raise
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
    await _require_ai_consent(request, user_id)
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
    # Опросник снимается только после состоявшегося хода, а не до брони и
    # вызова модели: раньше clear стоял первым, и на 429 (busy/лимит), таймауте
    # или сбое модели ответы пропадали целиком — повтор того же ответа получал
    # 409 setup_stale, и человеку оставалось проходить опросник заново. Пока
    # хода нет, в базе лежит состояние ДО этого ответа (его мы не записывали),
    # так что повтор с тем же question_index честно проходит проверку выше.
    _claim_turn_or_429(user_id, lang)
    try:
        history = await db.get_ai_conversation_wire_history(user_id)
        turn = await _run_turn(user_id, text, history)
        # До _turn_response, а не после: он сам решает, заводить ли новый
        # опросник, и смотрит на «предыдущий» (_next_setup_step) — законченный
        # там лежать не должен, как и прежде.
        await db.clear_ai_setup_state(user_id)
        return JSONResponse(await _turn_response(user_id, turn, goal=state.get("goal") or text))
    finally:
        _busy.discard(user_id)


async def _resolve_conflict(user_id: int, draft: dict[str, Any], on_conflict: str) -> dict[str, Any]:
    """Тот же выбор, что делают кнопки `ai:prog:replace`/`ai:prog:copy` на
    экране конфликта имени (см. handlers/ai_trainer.ai_program_replace_conflict/
    ai_program_copy_conflict) — здесь, а не второй копией той логики, потому
    что снимать её в общий ai_program_actions незачем: обе кнопки и так зовут
    ai_program_actions.save_into_existing_program/save_as_new_program, разница
    только в том, что подставить им на входе (существующая программа под этим
    именем или новое свободное имя), и это решение — целиком HTTP-специфичный
    разбор одного поля `on_conflict`, а не общая с ботом часть.
    """
    if on_conflict == "replace":
        existing = await db.find_program_by_name(user_id, draft["name"])
        if existing is None:
            # Программу с этим именем успели удалить между конфликтом и этим
            # запросом — заменять уже нечего, добавляем как новую (см. тот же
            # случай в ai_program_replace_conflict).
            return await ai_program_actions.save_as_new_program(user_id, draft)
        return await ai_program_actions.save_into_existing_program(user_id, draft, existing)

    # on_conflict == "copy": сохраняем под ближайшим свободным именем
    # (db.unique_program_name), а не под занятым.
    alt_name = await db.unique_program_name(user_id, draft["name"], suffix="2")
    renamed = dict(draft)
    renamed["name"] = alt_name
    return await ai_program_actions.save_as_new_program(user_id, renamed)


async def save_program(request: Request) -> JSONResponse:
    """Забрать предложенный черновик программы — HTTP-аналог кнопки «Забрать»/
    «Добавить себе» в боте (см. handlers/ai_trainer.ai_program_save). Сама
    запись — ai_program_actions.finalize_program_save, тот же код, что и у
    кнопки: свой второй сохранятель здесь не заводится.

    `draft_id` обязателен и сверяется с тем, что реально лежит у ЭТОГО
    пользователя (db.get_ai_program_draft скопирован по telegram_id из
    токена) — черновик другого пользователя тем самым не виден и не
    сохраняем в принципе, а не только по сверке id.

    Необязательный `on_conflict` ("replace" или "copy") — HTTP-аналог тапа по
    кнопке экрана конфликта имени (см. `_resolve_conflict`): без него, как и
    раньше, совпадение имени просто отдаёт 409 `name_conflict`, а «отклонить»
    — это не звать ручку снова, отдельного маршрута под кнопку «Отмена» не
    заводим.
    """
    user_id = await common.authed_user_id(request)
    body = await common.json_body(request)
    draft_id = common.require(body, "draft_id", str)
    on_conflict = common.optional_str(body, "on_conflict")
    if on_conflict is not None and on_conflict not in ("replace", "copy"):
        raise ApiError(400, "bad_request", "on_conflict must be 'replace' or 'copy'")

    draft = await db.get_ai_program_draft(user_id)
    if draft is None or draft.get("id") != draft_id:
        raise ApiError(404, "draft_not_found", "program draft is gone or belongs to a stale answer")

    # Черновик убираем ДО записи (та же атомарность, что у ai_program_save в
    # боте — см. его докстринг): повторный запрос с тем же draft_id уже не
    # найдёт черновика и получит честный 404 вместо повторного сохранения.
    await db.clear_ai_program_draft(user_id)
    try:
        if on_conflict is not None:
            result = await _resolve_conflict(user_id, draft, on_conflict)
        else:
            result = await ai_program_actions.finalize_program_save(user_id, draft)
    except Exception as exc:
        logger.exception("AI program save failed for user %s", user_id)
        # Как и в боте: черновик возвращается, чтобы можно было попробовать
        # сохранить ещё раз, не спрашивая тренера заново.
        await db.set_ai_program_draft(user_id, draft_id, draft)
        raise ApiError(500, "save_failed", "failed to save the program") from exc

    if result.get("error") == "budget":
        await db.set_ai_program_draft(user_id, draft_id, draft)
        raise ApiError(409, "routine_budget_exceeded", "routine budget exceeded", human=result["message"])
    if result.get("error") == "name_conflict":
        await db.set_ai_program_draft(user_id, draft_id, draft)
        raise ApiError(409, "name_conflict", f"program name already exists: {result['name']}")

    return JSONResponse({
        "program_id": result["program_id"],
        "day_count": result["day_count"],
        "replacing": result["replacing"],
    })


async def train_from_draft(request: Request) -> JSONResponse:
    """«▶️ Начать тренировку» по несохранённому черновику — HTTP-аналог
    `ai:prog:train` в боте (см. handlers/ai_trainer.ai_program_train):
    тренировка стартует прямо по одному дню плана, без сохранения программы.

    Активную тренировку не трогаем, а до-планируем — ровно как бот: если она
    уже есть (например, была начата с нуля или по другой программе), в неё
    форкаются и добираются только те упражнения плана, которых там ещё нет
    (см. `done_ids`). Значит и поведение при уже открытой тренировке то же,
    что у обычного `POST /workouts/active`: она не мешает и не заменяется,
    используется как есть.

    Черновик должен быть из ровно одного дня (`can_train_now` в его JSON) —
    у многодневного плана «начать сейчас» неоднозначно (какой из дней?), и
    бот эту кнопку под таким черновиком не показывает вовсе.
    """
    user_id = await common.authed_user_id(request)
    body = await common.json_body(request)
    draft_id = common.require(body, "draft_id", str)

    draft = await db.get_ai_program_draft(user_id)
    if draft is None or draft.get("id") != draft_id:
        raise ApiError(404, "draft_not_found", "program draft is gone or belongs to a stale answer")
    if len(draft["days"]) != 1:
        raise ApiError(400, "bad_request", "draft must have exactly one day to train from it directly")

    day = draft["days"][0]
    workout_id, created = await db.get_or_create_active_workout(user_id)
    done_ids = set() if created else set(await db.list_exercise_ids_for_workout(workout_id))

    planned = []
    for item in day["items"]:
        ex_id = await db.get_or_create_user_exercise_by_name(user_id, item["name"])
        if ex_id is None or ex_id in done_ids:
            continue
        done_ids.add(ex_id)
        planned.append({"exercise_id": ex_id, "display_name": item["name"], "target": item.get("target")})

    if not planned:
        # Как и в боте: черновик остаётся — по нему ещё можно «Добавить
        # себе», ничего не израсходовано.
        user = await db.get_user(user_id)
        lang = user["lang"] if user is not None else "ru"
        with i18n.use_lang(lang):
            message = i18n.t("ai.screen.program_train.all_done")
        raise ApiError(409, "all_done", "every day of the draft is already trained", human=message)

    # Черновик израсходован: он больше не «предложение, которое ждёт
    # решения» — по нему уже начали заниматься (см. тот же комментарий в
    # ai_program_train).
    await db.clear_ai_program_draft(user_id)

    workout = await db.get_workout(workout_id)
    return JSONResponse(
        {
            "workout": {
                "id": workout["id"],
                "status": workout["status"],
                "started_at": workout["started_at"],
                "routine_id": workout["routine_id"],
            },
            "plan": {"name": day["name"], "items": planned},
        },
        status_code=201 if created else 200,
    )


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
    await _require_ai_consent(request, user_id)
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
                raise ApiError(422, "voice_empty", "empty transcript", key="ai.screen.voice_empty")
        return JSONResponse({"question": transcript})
    finally:
        _busy.discard(user_id)


def _decode_video_data_url(data_url: str, *, too_big_message: str) -> tuple[bytes, str]:
    """Формат и настоящий размер видео-вложения — тот же приём, что у фото
    выше и у голоса (api_v1_voice), см. common.decode_data_url. В отличие от
    фото, video_analysis.analyze() хочет сырые байты и mime отдельно (не
    целую data: URL), поэтому раскодированное и возвращаем."""
    raw, mime, _ext = common.decode_data_url(
        data_url,
        VIDEO_EXTENSION_BY_MIME,
        field="video_data_url",
        max_bytes=config.MAX_VIDEO_BYTES,
        too_big_error=(400, "video_too_heavy", too_big_message),
    )
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
    await _require_ai_consent(request, user_id)
    user = await db.get_user(user_id)
    lang = user["lang"] if user is not None else "ru"

    if not config.video_analysis_available():
        with i18n.use_lang(lang):
            raise ApiError(503, "not_configured", "video analysis is not configured", key="ai.screen.video_not_available_text")
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
                    400, "video_too_long", "video is too long",
                    key="ai.screen.video_too_long", seconds=config.MAX_VIDEO_SECONDS,
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
    saved_image_path: Optional[str] = None
    try:
        block = await ai_limits.check(user_id, ai_limits.KIND_VIDEO)
        if block is not None:
            raise ApiError(429, "video_limit_exceeded", "daily video analysis limit reached", human=block.user_text)
        block = await ai_limits.check(user_id, ai_limits.KIND_QUESTION)
        if block is not None:
            raise ApiError(429, "question_limit_exceeded", "daily question limit reached", human=block.user_text)

        exercise_hint = await _resolve_video_exercise_hint(user_id, body)
        caption = common.optional_str(body, "caption") or ""

        analysis = await video_analysis.analyze(raw, user_id, mime_type=mime, exercise_hint=exercise_hint)
        if analysis is None:
            with i18n.use_lang(lang):
                raise ApiError(502, "video_analysis_failed", "video analysis failed", key="ai.screen.video_analysis_failed")

        # Квота видео тратится за состоявшийся разбор — как и в боте
        # (db.increment_ai_video_count сразу после успешного analyze, до
        # вопроса тренеру, см. _analyze_video_and_answer). Сбой уже вернул бы
        # 502 выше.
        await db.increment_ai_video_count(user_id)

        # Кадр-превью для истории чата (см. chat_attachments.py) — само видео
        # не хранится. Через to_thread: ffmpeg-процесс синхронный и держал
        # бы event loop, пока разбирает файл. Неудача (битый кодек, ffmpeg
        # не смог) не должна ронять ответ тренера — разбор уже состоялся и
        # оплачен, история просто останется без картинки.
        saved_image_path = await asyncio.to_thread(chat_attachments.save_video_frame, user_id, raw)

        with i18n.use_lang(lang):
            asked = caption or (
                i18n.t("ai.screen.analyze_technique", hint=exercise_hint) if exercise_hint else ""
            )
            question = asked or i18n.t("ai.screen.default_video_question")
            # Под тем же use_lang: оценки уверенности/серьёзности в блоке
            # переводятся через i18n (video_analysis._localized_enum), и вне
            # with они уезжали бы модели по-русски даже англоязычному атлету.
            video_context = video_analysis.to_context_block(analysis)

        history = await db.get_ai_conversation_wire_history(user_id)
        turn = await _run_turn(
            user_id, question, history,
            video_context=video_context,
            saved_image_path=saved_image_path,
        )
        return JSONResponse(await _turn_response(user_id, turn, goal=question))
    except Exception:
        # Тот же случай, что и в ask_question: ход не состоялся уже ПОСЛЕ
        # того, как кадр лёг на диск (например, вопрос тренеру не уложился
        # в таймаут) — до db.add_ai_conversation_turn дело не дошло, и файл
        # без строки в БД просто утечка.
        if saved_image_path is not None:
            chat_attachments.delete(saved_image_path)
        raise
    finally:
        _busy.discard(user_id)


async def undo_action(request: Request) -> JSONResponse:
    """«↩️ Отменить» под ответом тренера — HTTP-аналог кнопки `ai:undo:` в боте.

    `key` — из `actions[].key` ответа `/ai/ask` (или `/ai/pending` после
    возвращения на экран). Само описание отката клиенту не отдаётся вовсе: в
    нём лежат id чужих строк и прежние значения полей, а клиенту для кнопки
    достаточно ключа и подписи.

    Описание забирается из базы НАВСЕГДА до применения
    (`db.take_ai_undo_action`), тем же приёмом и по той же причине, что в боте:
    двойной тап не должен откатить дважды и снести заодно запись, которую
    человек успел сделать после. Поэтому 409 `undo_failed` — это «ключ был,
    но вернуть как было уже не вышло» (по упражнению успели записать подход,
    строку удалили руками), и повторять запрос бессмысленно: кнопка честно
    гаснет и в этом случае тоже.
    """
    user_id = await common.authed_user_id(request)
    user = await db.get_user(user_id)
    lang = user["lang"] if user is not None else "ru"
    body = await common.json_body(request)
    key = str(body.get("key") or "").strip()
    if not key:
        raise ApiError(400, "bad_request", "key is required")

    payload = await db.take_ai_undo_action(user_id, key)
    with i18n.use_lang(lang):
        if payload is None:
            raise ApiError(404, "undo_gone", "undo is gone", key="ai.screen.undo.already_gone")
        message = await ai_undo.apply(user_id, payload)
        if message is None:
            raise ApiError(409, "undo_failed", "undo failed", key="ai.screen.undo.failed")
    return JSONResponse({"message": message})


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
    # Кнопки отката — те, что висят под ПОСЛЕДНИМ ходом: подпись у них уже
    # локализованная (её сделал сам инструмент тренера в момент записи), и
    # перевода на лету они не требуют, в отличие от черновика и опросника.
    # Откаты более старых ходов живы в базе (до вытеснения, см.
    # db.MAX_AI_UNDO_ACTIONS) и ими можно воспользоваться, но нарисовать их
    # клиенту не под чем: истории с id ходов у него нет.
    last_turn = await db.get_ai_conversation_history(user_id, limit=1)
    actions = await db.get_ai_undo_actions(user_id, last_turn[0]["id"]) if last_turn else []

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

    return JSONResponse({"program": program_json, "questions": questions_json, "actions": actions})


async def get_history(request: Request) -> JSONResponse:
    """История для отрисовки чата: только видимая часть (роль/текст/время),
    без wire-формата — клиенту нечего делать с tool-calls модели, и тащить их
    в JSON было бы лишним трафиком и утечкой внутренностей (см. докстринг
    модуля). `limit` считает ХОДЫ (вопрос+ответ), а не отдельные сообщения —
    так же, как хранит их db.ai_conversation_turns.

    `image_url` — у реплики пользователя, если к вопросу было приложено фото
    или видео (у видео — кадр-превью, см. chat_attachments.py). Путь вида
    `/ai/history/{id}/image` — БЕЗ префикса `/v1` (его перед каждым relative-
    URL сама подставляет APIClient.request на клиенте, см. её докстринг), а
    не ссылка на файл напрямую: у файла нет колонки владельца, и раздавать
    его можно только сверив id хода с автором токена (см. get_history_image
    ниже) — то же самое, чем /exercises/{id}/photo защищает своё фото
    упражнения.
    """
    user_id = await common.authed_user_id(request)
    limit = common.query_int(
        request, "limit", db.MAX_AI_CONVERSATION_TURNS,
        minimum=1, maximum=db.MAX_AI_CONVERSATION_TURNS,
    )
    turns = await db.get_ai_conversation_history(user_id, limit=limit)
    return JSONResponse({"messages": _history_messages(turns)})


# Сколько символов первого вопроса уходит в заголовок разговора (см.
# `_conversation_json`). Длиннее — это уже не заголовок списка, а сам вопрос.
CONVERSATION_TITLE_CHARS = 80


def _history_messages(turns: list[Any]) -> list[dict[str, Any]]:
    """Ходы (вопрос+ответ) — в плоскую ленту реплик. Общее для живого чата
    (`GET /ai/history`) и архивного разговора (`GET /ai/conversations/{id}`):
    рисует их клиент одним и тем же кодом, значит и форма обязана быть одна.

    `image_url` у архивной реплики тот же `/ai/history/{turn_id}/image`:
    владение проверяется по id хода (см. get_history_image), а не по тому, в
    каком разговоре он лежит, так что вложения архива отдаются как были.
    """
    messages: list[dict[str, Any]] = []
    for row in turns:
        user_message: dict[str, Any] = {
            "role": "user", "text": row["question"], "created_at": row["created_at"],
        }
        if row["image_path"]:
            user_message["image_url"] = f"/ai/history/{row['id']}/image"
        messages.append(user_message)
        messages.append({"role": "assistant", "text": row["answer"], "created_at": row["created_at"]})
    return messages


async def get_history_image(request: Request) -> Any:
    """Само вложение (фото к вопросу или кадр-превью видео) байтами — тот же
    приём, что и у своего фото упражнения (api_v1_media.get_exercise_photo):
    приватный файл, Bearer-токен обязателен, отдаём владельцу хода, а не по
    голому имени файла.

    `turn_id`, а не имя файла в URL: имя ничего не говорит о владельце, а
    db.get_ai_conversation_turn проверяет telegram_id хода против токена —
    чужой id (угаданный или подсмотренный) получает тот же 404, что и
    отсутствующий вовсе."""
    user_id = await common.authed_user_id(request)
    turn_id = int(request.path_params["turn_id"])
    turn = await db.get_ai_conversation_turn(user_id, turn_id)
    if turn is None or not turn["image_path"]:
        raise ApiError(404, "not_found", "no image for this turn")
    path = chat_attachments.path_for(turn["image_path"])
    if path is None:
        raise ApiError(404, "not_found", "no image for this turn")

    ext = os.path.splitext(path)[1].lower()
    content_type = _IMAGE_CONTENT_TYPES.get(ext, "application/octet-stream")
    # Та же логика кэша, что и у своего фото упражнения: приватное и может
    # смениться никогда (вложение хода не переписывается), но `immutable`
    # всё равно ни к чему — снесённый вместе с вытесненным ходом файл потом
    # должен запрашиваться заново, а не отдаваться из кэша браузера как 200.
    return FileResponse(path, media_type=content_type, headers={"Cache-Control": "private, max-age=0, must-revalidate"})


async def delete_history(request: Request) -> JSONResponse:
    """«Начать разговор заново» — без него испорченный контекст (модель
    зацепилась не за то в старом ходу) нечем починить. Заодно сбрасывает
    черновик программы и активный опросник — они разговору не переживают
    (тот же приём, что и в боте, где state.clear() после «в меню» убирает и
    ai_history, и ai_program_draft/ai_setup разом).

    DELETE, но ходы больше не стираются: разговор уезжает в архив на чтение
    (см. db.start_new_ai_conversation и GET /ai/conversations). Для клиента
    контракт прежний — `{"cleared": true}`, и следующий `GET /ai/history`
    пуст, — а глагол остался DELETE, чтобы не ломать уже выпущенные версии
    приложения ради переименования.
    """
    user_id = await common.authed_user_id(request)
    await db.start_new_ai_conversation(user_id)
    await db.clear_ai_program_draft(user_id)
    await db.clear_ai_setup_state(user_id)
    # И кнопки отката: ходов, под которыми они висели, на экране больше нет, а
    # тихо живущий ключ к «удалить вес» — это отмена, которую уже нечем
    # осознанно вызвать (в боте state.clear() уносит ai_undo ровно так же).
    await db.clear_ai_undo_actions(user_id)
    return JSONResponse({"cleared": True})


def _conversation_json(row: Any) -> dict[str, Any]:
    """Одна строка списка разговоров. `title` — первый вопрос человека,
    обрезанный: своего имени у разговора нет, а просить модель придумать его —
    платный вызов на каждое открытие списка."""
    title = (row["first_question"] or "").strip().replace("\n", " ")
    if len(title) > CONVERSATION_TITLE_CHARS:
        title = title[: CONVERSATION_TITLE_CHARS - 1].rstrip() + "…"
    return {
        "id": row["conversation_id"],
        "title": title,
        "turns": row["turns"],
        "started_at": row["started_at"],
        "last_at": row["last_at"],
    }


async def list_conversations(request: Request) -> JSONResponse:
    """Прошлые разговоры с тренером — то, что раньше стиралось насовсем.

    `current` — номер разговора, который открыт в чате прямо сейчас: он тоже
    есть в списке (он же самый свежий), и клиенту нужно знать, какую строку не
    открывать архивом, а показывать как текущую.
    """
    user_id = await common.authed_user_id(request)
    limit = common.query_int(request, "limit", 30, minimum=1, maximum=100)
    before_id = common.query_int(request, "before_id", 0, minimum=0)
    rows = await db.list_ai_conversations(
        user_id, limit=limit, before_id=before_id or None
    )
    return JSONResponse({
        "conversations": [_conversation_json(row) for row in rows],
        "current": await db.current_ai_conversation_id(user_id),
    })


async def get_conversation(request: Request) -> JSONResponse:
    """Один разговор целиком — та же форма, что у `GET /ai/history`, чтобы
    клиент рисовал архив тем же кодом, что и живой чат.

    Только чтение: продолжить архивный разговор нельзя (`POST /ai/ask` всегда
    пишет в текущий), и wire-снимок у него уже обнулён — см.
    db.start_new_ai_conversation. Пустой ответ вместо 404 у несуществующего
    или чужого номера — тот же приём, что и везде в `/v1`: чужой номер ничем
    не отличается от номера, под которым ничего нет.
    """
    user_id = await common.authed_user_id(request)
    conversation_id = int(request.path_params["conversation_id"])
    turns = await db.get_ai_conversation(user_id, conversation_id)
    return JSONResponse({"messages": _history_messages(turns)})


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
    Route("/ai/program/train", train_from_draft, methods=["POST"]),
    Route("/ai/undo", undo_action, methods=["POST"]),
    Route("/ai/pending", get_pending_state, methods=["GET"]),
    Route("/ai/conversations", list_conversations, methods=["GET"]),
    Route("/ai/conversations/{conversation_id:int}", get_conversation, methods=["GET"]),
    Route("/ai/history", get_history, methods=["GET"]),
    Route("/ai/history", delete_history, methods=["DELETE"]),
    Route("/ai/history/{turn_id:int}/image", get_history_image, methods=["GET"]),
    Route("/ai/thinking", get_thinking, methods=["GET"]),
]
