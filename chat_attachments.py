"""Вложения к истории чата с AI-тренером: фото к вопросу и кадр-превью видео.

До этого модуля и фото, и видео к вопросу тренеру уходили только в саму
модель — в ai_conversation_turns (db.py) от них оставался лишь текстовый
маркер («[фото] {подпись}», «[видео] {вопрос}», см. ai.screen.history_photo/
history_video в locales/*.json). В боте это незаметно, потому что хранилище
чата — сам Telegram: сообщение с фото или видео висит в переписке вечно,
даже если сервер о нём ничего не знает. У HTTP-варианта (`/v1`, iOS-приложение)
такого хранилища нет — экран чата показывает ровно то, что вернул
GET /ai/history, и без файла на диске уход с экрана стирал картинку насовсем.

Асимметрия хранения — сознательная, а не «пока не успели»:

- **Фото** сохраняется файлом целиком (`save_photo`). Оно весит сотни
  килобайт, и хранить их дёшево — тот же порядок величины, что и своё фото
  упражнения (exercise_photos.py).
- **Видео** НЕ хранится — из него достаётся один кадр-превью и сохраняется
  как обычное фото (`save_video_frame`). Двадцать роликов на одного
  человека — это уже под сотню мегабайт постоянного тома, а разбор всё
  равно делается один раз, в момент отправки: пересматривать исходник
  незачем, важно видеть в истории, о чём был разговор.

Кадр достаётся ffmpeg-бинарником из пакета `imageio-ffmpeg` (чистый pip,
без системных зависимостей — на Amvera нет ни apt, ни Dockerfile, только
`pip install -r requirements.txt`, см. amvera.yaml).

Каталог и порядок хранения — тот же приём, что и exercise_photos.py:
диск, а не БД (BLOB в SQLite на каждый ход раздул бы файл базы и её бэкап),
имя файла с uuid (а не производное от turn_id — ход своего id ещё не имеет
на момент сохранения, см. api_v1_ai._run_turn), путь проверяется на выход
за каталог перед каждой отдачей.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import uuid

import config

logger = logging.getLogger(__name__)

# Тот же набор, что фото к вопросу тренеру принимает в /ai/ask
# (api_v1_ai.IMAGE_EXTENSION_BY_MIME) — свой список здесь был бы вторым
# набором правил о том же самом.
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}

# Кадр всегда сохраняется JPEG — ffmpeg сам решает формат по расширению
# выходного файла, а JPEG и легче PNG, и не нужен ради одного статичного
# превью в истории чата.
FRAME_EXTENSION = "jpg"

# Секунда в ролик, а не самый первый кадр: на 0:00 часто ещё стоит атлет,
# ещё не начавший подход (наводит камеру, отходит к штанге) — секундой позже
# кадр уже про упражнение. Видео короче секунды — редкость (MAX_VIDEO_SECONDS
# далеко не одна секунда), и на этот случай есть запасной вариант ниже.
_FRAME_SEEK_SECONDS = "00:00:01.0"
_FRAME_SEEK_FALLBACK = "00:00:00.0"


def media_dir() -> str:
    """Каталог читается функцией, а не константой модуля: тесты подменяют
    config.AI_CHAT_MEDIA_DIR на временный каталог, и константа, снятая при
    импорте, осталась бы указывать на боевой /data."""
    return config.AI_CHAT_MEDIA_DIR


def path_for(name: str | None) -> str | None:
    """Имя файла из базы → полный путь, если файл на месте.

    Та же защита от выхода за каталог, что в exercise_photos.path_for: имя
    приходит из БД, но это последний рубеж перед открытием файла и не должен
    зависеть от того, как значение туда попало."""
    if not name:
        return None
    root = os.path.realpath(media_dir())
    candidate = os.path.realpath(os.path.join(media_dir(), name))
    if candidate != root and not candidate.startswith(root + os.sep):
        return None
    return candidate if os.path.isfile(candidate) else None


def _write(raw: bytes, ext: str, prefix: str) -> str:
    """Общая запись байт в каталог вложений — через временный файл с
    переименованием, чтобы обрыв на середине не оставил в каталоге
    полуфайл, который потом отдастся как «вложение» (тот же приём, что
    exercise_photos.save)."""
    os.makedirs(media_dir(), exist_ok=True)
    name = f"{prefix}_{uuid.uuid4().hex[:16]}.{ext}"
    final_path = os.path.join(media_dir(), name)
    tmp_path = final_path + ".part"
    with open(tmp_path, "wb") as fh:
        fh.write(raw)
    os.replace(tmp_path, final_path)
    return name


def save_photo(user_id: int, raw: bytes, ext: str) -> str:
    """Фото к вопросу тренеру → имя файла в каталоге вложений.

    Валидация формата — на совести вызывающего (api_v1_ai уже прогнала байты
    через common.decode_data_url с тем же списком расширений, что и здесь):
    дублировать проверку означало бы поддерживать два списка допустимых
    MIME одновременно."""
    ext = ext.lower().lstrip(".")
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"unsupported chat photo extension: {ext!r}")
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        raise ValueError("chat photo payload must be non-empty bytes")
    return _write(bytes(raw), ext, f"u{user_id}")


def _ffmpeg_path() -> str:
    """Путь к бинарнику ffmpeg из пакета imageio-ffmpeg.

    Импорт внутри функции, а не наверху модуля: пакет нужен только на пути
    сохранения видео-кадра, и модуль не должен падать при импорте там, где
    его почему-то не оказалось (тесты фото-пути его не касаются вовсе)."""
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def _extract_frame(video_path: str, out_path: str, seek: str) -> bool:
    result = subprocess.run(
        [
            _ffmpeg_path(), "-y",
            "-ss", seek,
            "-i", video_path,
            "-vframes", "1",
            "-q:v", "3",
            out_path,
        ],
        capture_output=True,
        timeout=30,
    )
    return result.returncode == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0


def save_video_frame(user_id: int, video_bytes: bytes) -> str | None:
    """Видео подхода → один кадр-превью, сохранённый как фото.

    Сам ролик НЕ сохраняется — см. докстринг модуля. None, если кадр
    достать не удалось (битый файл, неподдержанный кодек, ffmpeg упал):
    это не должно ронять весь ответ тренера, разбор видео к этому моменту
    уже состоялся и оплачен, история просто останется без картинки, как
    было бы вообще без этой фичи.
    """
    if not isinstance(video_bytes, (bytes, bytearray)) or not video_bytes:
        return None
    with tempfile.TemporaryDirectory() as tmp_dir:
        video_path = os.path.join(tmp_dir, "input")
        frame_path = os.path.join(tmp_dir, "frame.jpg")
        with open(video_path, "wb") as fh:
            fh.write(video_bytes)
        try:
            ok = _extract_frame(video_path, frame_path, _FRAME_SEEK_SECONDS)
            if not ok:
                # Ролик короче секунды или секундный кадр не вытащился по
                # другой причине — пробуем самый первый кадр как есть.
                ok = _extract_frame(video_path, frame_path, _FRAME_SEEK_FALLBACK)
            if not ok:
                logger.warning("chat_attachments: ffmpeg didn't produce a frame for user %s", user_id)
                return None
            with open(frame_path, "rb") as fh:
                frame_bytes = fh.read()
        except Exception:
            logger.exception("chat_attachments: video frame extraction failed for user %s", user_id)
            return None
    try:
        return save_photo(user_id, frame_bytes, FRAME_EXTENSION)
    except ValueError:
        logger.exception("chat_attachments: extracted frame rejected for user %s", user_id)
        return None


def delete(name: str | None) -> None:
    """Снести файл по имени. Отсутствие файла — не ошибка (тот же приём,
    что exercise_photos.delete): сюда приходят и имена из базы, файл под
    которыми уже мог пропасть."""
    path = path_for(name)
    if path is None:
        return
    try:
        os.remove(path)
    except OSError:
        logger.warning("chat_attachments: не удалось снести %s", path, exc_info=True)
