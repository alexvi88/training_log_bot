"""REST `/v1` для обратной связи и фактчека постов (в боте — handlers/feedback.py
и handlers/factcheck.py соответственно).

**Отзыв (`POST /feedback`).** У бота отзыв уходит админу прямым
`message.bot.send_message` — готовым инстансом `Bot`, который живёт в
`main.main()` на весь процесс поллинга. У HTTP-хендлера такого инстанса нет:
маршруты `/v1` собираются в mcp_server.build_app() до того, как `main()`
вообще создаёт бота (см. `api_v1.py`, докстринг модуля). Выход —
короткоживущий `Bot(token=...)` на одну отправку, сессия закрывается сразу
после: событие редкое (человек не шлёт отзывы каждую секунду), а держать процессу-исключение отдельный
долгоживущий Bot ради одного эндпоинта незачем.

Без `config.ADMIN_ID` отзыву в боте лететь некуда (см.
`handlers.feedback.feedback_message`, ветка `feedback.no_recipient`) — здесь
это 503: у API нет экрана, чтобы просто промолчать и сделать вид, что отзыв
принят.

Антиспам — простейший, в памяти процесса (`_daily_counts`): не переживает
рестарт и не общий между несколькими воркерами, если они когда-нибудь
появятся (сейчас процесс один, см. main.py — бот и `/v1` живут в одном
event loop). Постоянного per-user rate-limit в проекте нет (db.py трогать
нельзя), поэтому переиспечь готовое было не из чего.

**Фото к отзыву.** У бота отзыв идёт `message.copy_to` — летит вообще всё,
что человек прислал (фото, файлы, голос). HTTP не может скопировать чужое
сообщение, поэтому здесь только один тип вложения — фото, `image_data_url`,
тем же приёмом, что везде в `/v1` (см. `api_v1_ai.ask_question`,
`api_v1_media.upload_exercise_photo`): `data:<mime>;base64,...` в обычном
JSON-теле, а не multipart. Формат и потолок байт — те же самые
(`api_v1_ai.IMAGE_EXTENSION_BY_MIME`, `handlers.ai_trainer.MAX_IMAGE_BYTES`)
и тот же код ошибки на слишком большое фото (`photo_too_big`, 400) — это не
новый лимит, а старый, продублированный ради того самого инцидента с
пустым 413 от ingress Amvera: раздутое base64-тело клиент должен уметь
понять как «слишком большое», а не как оборванное соединение, а раз коды
ошибок для фото-вопроса тренеру клиент уже умеет показывать, заводить для
фото к отзыву второй набор текстов незачем. Текст отзыва при этом всегда
уходит отдельным `send_message` (см. `_send_feedback_to_admin`), а не
подписью к фото: подпись у Telegram обрезана 1024 символами
(`formatting.CAPTION_LIMIT`) против 4096 у отзыва (`MAX_FEEDBACK_LENGTH`) —
длинный отзыв с фото не должен молча резаться.

**Фактчек (`POST /factcheck`).** Разбирает та же `ai_trainer.fact_check_post`,
что дёргает `handlers/factcheck.py` — она и так принимает голые
`user_id`/`post_text`/`image_data_url`, без объекта телеграм-сообщения (тот
же приём, что уже применён для `analyze_food` в api_v1_food.py и для `ask`
в api_v1_ai.py, рефакторинг самого ai_trainer.py не понадобился).

Квота — `ai_limits.KIND_QUESTION`, та же, что тратит форвард в боте (см.
докстринг `handlers.factcheck.factcheck_forward`: «Та же квота, что у
обычных вопросов тренеру: это тоже вопрос, просто с чужим текстом вместо
своего»). Порядок ровно как в api_v1_ai.ask_question: проверка ДО вызова
модели, `db.try_increment_ai_question_count` ПОСЛЕ успешного ответа — сбой
провайдера не должен стоить человеку попытки.

Busy-замок — тот же приём, что у `/ai/ask` (`api_v1_ai._busy`) и
`/food/parse` (`api_v1_food._busy`), тот же общий примитив `busy_lock.py`, и
своя, отдельная от них копия набора `_busy`: у бота фактчек тоже держит свой
собственный набор, не общий с чатом тренера (см. `handlers.factcheck._busy`),
по той же причине — разбор форварда не должен блокировать человеку основной
чат и наоборот. Без замка два параллельных `POST /factcheck` одного
пользователя оба читают ещё не увеличенный счётчик, оба проходят
`ai_limits.check` и оба уходят в модель — `db.try_increment_ai_question_count`
атомарен, но режет только сам счётчик, а не платные вызовы, которые к этому
моменту уже сделаны. Бронь — ДО `ai_limits.check`, снимается в `finally` при
любом исходе (исключение, таймаут, обычный успех); занятому человеку отдаём
тот же 429/`busy`, что и у `/ai/ask`/`/food/parse` (`ai.screen.busy`), а не
изобретаем свой текст.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Optional

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import ai_limits
import ai_trainer
import api_v1_ai
import api_v1_common as common
import busy_lock
import config
import db
import formatting
import i18n
from handlers.ai_trainer import MAX_IMAGE_BYTES

logger = logging.getLogger(__name__)

ApiError = common.ApiError

# Тот же потолок, что у сообщения Telegram: отзыв в боте физически не может
# быть длиннее (aiogram сам режет ввод на этой границе), так что HTTP-клиенту
# оставляем ровно тот же лимит, а не выдумываем свой.
MAX_FEEDBACK_LENGTH = formatting.MESSAGE_LIMIT

# Пустой пост из фитнес-канала — не пост, а фото/эмодзи без текста; слишком
# длинный — предохранитель от присланной по ошибке статьи на разворот. Верхняя
# граница — тот же порядок, что MAX_QUESTION_LENGTH в api_v1_ai.py: разбор
# на модели стоит одинаково что вопрос, что пост, раздувать промпт незачем.
MAX_FACTCHECK_TEXT_LENGTH = 4000

# Отзывов в сутки на пользователя. В памяти процесса — см. докстринг модуля:
# честный компромисс без похода в db.py, которую трогать нельзя. Переживает
# любое число запросов за день, но не рестарт процесса и не второй воркер.
FEEDBACK_DAILY_LIMIT = 5

# user_id -> (date отправки последнего отзыва, счётчик за этот день).
_daily_counts: dict[int, tuple[dt.date, int]] = {}

# Свой, отдельный от api_v1_ai._busy/api_v1_food._busy набор — см. докстринг
# модуля про фактчек и почему он не делит замок ни с чатом тренера, ни с
# разбором еды.
_busy: set[int] = set()


def _feedback_quota_left(user_id: int) -> bool:
    """True, если у пользователя ещё остался отзыв на сегодня (UTC)."""
    today = dt.datetime.now(dt.timezone.utc).date()
    entry = _daily_counts.get(user_id)
    if entry is None or entry[0] != today:
        return True
    return entry[1] < FEEDBACK_DAILY_LIMIT


def _record_feedback_sent(user_id: int) -> None:
    today = dt.datetime.now(dt.timezone.utc).date()
    entry = _daily_counts.get(user_id)
    count = entry[1] + 1 if entry is not None and entry[0] == today else 1
    _daily_counts[user_id] = (today, count)


async def _send_feedback_to_admin(user_id: int, text: str, photo: Optional[bytes]) -> None:
    """Короткоживущий Bot на одну отправку — см. докстринг модуля.

    Текст и фото — двумя отдельными сообщениями, а не подписью к фото: см.
    докстринг модуля про CAPTION_LIMIT. Порядок — текст, потом фото: если
    отправка фото упадёт (сеть моргнула между двумя вызовами), у админа уже
    есть сам отзыв, и это не 503 — то, ради чего человек писал, долетело."""
    from aiogram import Bot
    from aiogram.types import BufferedInputFile

    bot = Bot(token=config.BOT_TOKEN)
    try:
        # "из приложения" обязательно в тексте: иначе на отзыв нельзя ответить
        # (в отличие от бота, у которого message.copy_to сохраняет отправителя
        # и на скопированное сообщение можно ответить прямо в Telegram) и
        # непонятно, что за user_id вообще такое — это единственная зацепка,
        # у кого спросить подробности.
        await bot.send_message(
            config.ADMIN_ID,
            f"📱 Фидбек из приложения от id {user_id}:\n\n{text}",
        )
        if photo is not None:
            await bot.send_photo(config.ADMIN_ID, BufferedInputFile(photo, filename="feedback.jpg"))
    finally:
        await bot.session.close()


async def submit_feedback(request: Request) -> JSONResponse:
    user_id = await common.authed_user_id(request)
    if config.ADMIN_ID is None:
        raise ApiError(503, "not_configured", "feedback has no recipient configured")

    body = await common.json_body(request)
    text = str(common.require(body, "text", str)).strip()
    if not text:
        raise ApiError(400, "bad_request", "text must not be empty", key="api.error.text_empty")
    if len(text) > MAX_FEEDBACK_LENGTH:
        raise ApiError(
            400, "bad_request", f"text must be at most {MAX_FEEDBACK_LENGTH} characters",
            key="api.error.text_too_long", max=MAX_FEEDBACK_LENGTH,
        )

    image_data_url = common.optional_str(body, "image_data_url")
    photo: Optional[bytes] = None
    if image_data_url is not None:
        user = await db.get_user(user_id)
        lang = user["lang"] if user is not None else "ru"
        with i18n.use_lang(lang):
            raw, _mime, _ext = common.decode_data_url(
                image_data_url,
                api_v1_ai.IMAGE_EXTENSION_BY_MIME,
                field="image_data_url",
                max_bytes=MAX_IMAGE_BYTES,
                too_big_error=(
                    400,
                    "photo_too_big",
                    i18n.t("ai.screen.photo_too_big", mb=MAX_IMAGE_BYTES // (1024 * 1024)),
                ),
            )
        photo = raw

    if not _feedback_quota_left(user_id):
        raise ApiError(429, "feedback_limit_exceeded", "daily feedback limit reached")

    try:
        await _send_feedback_to_admin(user_id, text, photo)
    except Exception as exc:
        logger.exception("feedback: delivery to admin failed for user %s", user_id)
        raise ApiError(503, "delivery_failed", "feedback could not be delivered") from exc

    # Считаем только реально доставленные — упавшая отправка не должна съедать
    # попытку человека, у которого и так что-то не работает.
    _record_feedback_sent(user_id)
    return JSONResponse({"delivered": True}, status_code=201)


async def submit_factcheck(request: Request) -> JSONResponse:
    user_id = await common.authed_user_id(request)
    if not ai_trainer.is_configured():
        raise ApiError(503, "not_configured", "fact-check is not configured")

    body = await common.json_body(request)
    text = common.optional_str(body, "text") or ""
    image_data_url = common.optional_str(body, "image_data_url")
    if not text and not image_data_url:
        raise ApiError(400, "bad_request", "text or image_data_url required")
    if len(text) > MAX_FACTCHECK_TEXT_LENGTH:
        raise ApiError(
            400, "bad_request", f"text must be at most {MAX_FACTCHECK_TEXT_LENGTH} characters",
            key="api.error.text_too_long", max=MAX_FACTCHECK_TEXT_LENGTH,
        )

    user = await db.get_user(user_id)
    lang = user["lang"] if user is not None else "ru"

    # Бронь — ДО проверки лимита: см. докстринг модуля и `_busy` выше про то,
    # почему без неё два параллельных запроса оба уходят в модель.
    if not busy_lock.try_claim(_busy, user_id):
        with i18n.use_lang(lang):
            raise ApiError(429, "busy", "another request is in flight", key="ai.screen.busy")
    try:
        block = await ai_limits.check(user_id, ai_limits.KIND_QUESTION)
        if block is not None:
            raise ApiError(429, "question_limit_exceeded", "daily question limit reached", human=block.user_text)

        try:
            # Под use_lang: языковой хвост системного промпта
            # (ai_trainer._with_language_tail) берётся из контекста, и без
            # него англоязычный атлет получал вердикт по-русски.
            with i18n.use_lang(lang):
                verdict = await asyncio.wait_for(
                    ai_trainer.fact_check_post(user_id, text, image_data_url),
                    timeout=config.AI_TOTAL_ANSWER_SECONDS,
                )
        except asyncio.TimeoutError as exc:
            raise ApiError(504, "timeout", "fact-check did not answer in time") from exc
        except Exception as exc:
            raise ApiError(502, "factcheck_failed", "fact-check failed") from exc

        # Квота — та же, что у /ai/ask (см. докстринг модуля): списывается только
        # за состоявшийся разбор, сбой выше уже вернул бы 502/504 и до сюда не дошёл.
        await db.try_increment_ai_question_count(user_id, config.AI_QUESTION_DAILY_LIMIT)
        return JSONResponse({"verdict": verdict})
    finally:
        _busy.discard(user_id)


routes = [
    Route("/feedback", submit_feedback, methods=["POST"]),
    Route("/factcheck", submit_factcheck, methods=["POST"]),
]
