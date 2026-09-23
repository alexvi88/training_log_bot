"""Расшифровка голоса, общая для двух маршрутов `/v1`:

- `POST /ai/voice`               — голосовой вопрос тренеру (api_v1_ai.py)
- `POST /workouts/{id}/sets/voice` — подход голосом (api_v1.py)

Обе точки транскрибируют один и тот же файл одной и той же функцией —
`ai_trainer.transcribe_voice`, точно та же, что вызывает бот
(`handlers/ai_trainer.py::ai_voice_question` для вопроса,
`handlers/workout.py::log_set_voice` для подхода). Второй реализации
транскрипции здесь нет: этот модуль только декодирует присланный файл и
проверяет те же лимиты, что бот, — саму расшифровку по-прежнему делает
`ai_trainer.transcribe_voice`.

**Лимиты.** `MAX_VOICE_BYTES`/`MAX_VOICE_SECONDS` берутся из
`handlers.ai_trainer` (те же числа, что ограничивают голосовой вопрос в
боте), а не заводятся заново — иначе бот и API разошлись бы на лимитах при
первой же правке одного из них.

**Транспорт: JSON + data URL, не multipart.** У `/v1` уже есть прецедент для
бинарных вложений: `POST /food/parse` и бот-хендлер `/factcheck` кодируют
фото как `data:image/jpeg;base64,...` внутри обычного JSON-тела (см.
api_v1_food.py). Голос — тот же случай: одно вложение на запрос, без
множества файлов и без стриминга по частям. Заводить multipart ради
единственного типа полей означало бы второй способ разбора тела запроса в
API, которым больше ничего не пользуется, — а ~33% оверхеда base64 на голосе
до `MAX_VOICE_BYTES` не выглядит проблемой: это разговорная фраза, обычно на
порядок меньше потолка.

**Формат.** Телефон пишет голос не в OGG/Opus, как Telegram, а в M4A/AAC
(`AVAudioRecorder` на iOS по умолчанию пишет `.m4a`, контейнер MPEG-4).
`ai_trainer.transcribe_voice` сама ничего не декодирует и не конвертирует —
она отдаёт файлоподобный объект в OpenAI `audio.transcriptions.create`, а тот
определяет формат по расширению в `file.name` (поэтому боту достаточно
`buf.name = "voice.ogg"`, без единой строчки декодирования — см.
`handlers/ai_trainer.py::_download_voice_as_file`). Официально
поддерживаемый OpenAI список расширений — flac/m4a/mp3/mp4/mpeg/mpga/oga/
ogg/wav/webm, и m4a в нём есть напрямую: конвертировать нечем и незачем,
новая зависимость (ffmpeg-python/pydub — их нет ни в requirements.txt, ни
где-либо ещё в проекте) не нужна. `AUDIO_EXTENSION_BY_MIME` ниже — явный
список того, что мы вслед за OpenAI принимаем; расширение для имени файла
берётся из MIME в data URL, а не угадывается по байтам.

**Длительность.** У `types.Voice` из Telegram уже есть поле `duration` — у
HTTP-загрузки его взять неоткуда: в проекте нет ни одной библиотеки для
разбора медиаконтейнеров (проверить длительность MPEG-4/M4A без такой
библиотеки — значит писать её мини-версию самим, что не лучше настоящей
зависимости). Поэтому длительность — необязательное поле в теле запроса,
`duration_seconds`, которое присылает клиент (на телефоне это одно число из
рекордера). Соврать в нём можно, но ставка та же, что при вранье о размере
файла запросом напрямую в API в обход клиента: реальный размер всё равно
проверяется честно, по факту раскодированных байт, а `MAX_VOICE_SECONDS` —
это защита от типичного случая (длинная лекция вместо короткой фразы), а не
криптографическая гарантия.
"""

from __future__ import annotations

import io
from typing import Optional

import ai_trainer
import api_v1_common as common
from handlers.ai_trainer import MAX_VOICE_BYTES, MAX_VOICE_SECONDS

ApiError = common.ApiError

AUDIO_EXTENSION_BY_MIME = {
    "audio/mp4": "mp4",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/oga": "oga",
    "audio/flac": "flac",
}


def _decode_audio_data_url(data_url: str, *, too_big_message: str) -> tuple[bytes, str]:
    # Разбор data: URL общий для всех вложений `/v1` — см. common.decode_data_url
    # (голос был первым и единственным до фото/видео вопроса тренеру, api_v1_ai.py).
    raw, _mime, ext = common.decode_data_url(
        data_url,
        AUDIO_EXTENSION_BY_MIME,
        field="audio_data_url",
        max_bytes=MAX_VOICE_BYTES,
        too_big_error=(400, "voice_too_big", too_big_message),
    )
    return raw, ext


async def transcribe(
    body: dict,
    user_id: int,
    *,
    not_configured_message: str,
    too_long_message: str,
    too_big_message: str,
    transcribe_failed_message: str,
) -> str:
    """Тело запроса (`audio_data_url`, необязательный `duration_seconds`) →
    расшифрованный текст.

    Может вернуть пустую строку на невнятной/тихой записи (как и
    `ai_trainer.transcribe_voice` боту) — решение, что тогда показать,
    оставлено вызывающей стороне: у вопроса тренеру это отдельное сообщение
    (`ai.screen.voice_empty`), у подхода голосом — тот же путь, что и
    неразобранная строка текстом (`voice_parse` не увидит чисел в пустом
    тексте и вернёт None).
    """
    if not ai_trainer.is_voice_configured():
        raise ApiError(503, "not_configured", "voice is not configured", human=not_configured_message)

    duration = body.get("duration_seconds")
    if duration is not None:
        if not isinstance(duration, (int, float)) or isinstance(duration, bool):
            raise ApiError(400, "bad_request", "duration_seconds must be a number")
        if duration > MAX_VOICE_SECONDS:
            raise ApiError(400, "voice_too_long", "voice note is too long", human=too_long_message)

    data_url = common.require(body, "audio_data_url", str)
    raw, ext = _decode_audio_data_url(data_url, too_big_message=too_big_message)

    buf = io.BytesIO(raw)
    buf.name = f"voice.{ext}"
    try:
        transcript: Optional[str] = await ai_trainer.transcribe_voice(buf, user_id)
    except Exception as exc:
        raise ApiError(
            502, "voice_transcribe_failed", "transcription failed", human=transcribe_failed_message
        ) from exc
    return transcript or ""
