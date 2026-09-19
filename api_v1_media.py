"""REST `/v1` для демонстраций упражнений: фото старт/конец, клип, описание.

В боте это раздаёт handlers/exercises.py, читая файлы с диска и отправляя их
в Telegram (exercise_media.py) плюс текст техники (exercise_descriptions.py).
Здесь то же самое, только по HTTP: файл со старта/конца, зацикленный клип
(если уже снят) и текстовая инструкция для карточки упражнения в приложении.

Ключ поиска — не id упражнения, а имя шаблона (exercise_media.catalog_key,
т.е. `original_name`): один и тот же каталог фото и текста общий для всех,
кто форкнул один и тот же шаблон, и не зависит от того, как пользователь
переименовал свою копию. Резолв путей и текста целиком переиспользует
exercise_media.get_images_for/get_animation_for и
exercise_descriptions.effective_description — здесь нет второй реализации
этой логики, только транспорт.
"""

from __future__ import annotations

import mimetypes
import os
from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

import api_v1_common as common
import db
import exercise_descriptions
import exercise_media

ApiError = common.ApiError

# Content-Type по расширению: свой, а не общесистемный mimetypes.guess_type,
# чтобы не зависеть от того, что знает /etc/mime.types в контейнере — набор
# расширений тут и так фиксирован (exercise_media хранит только .jpg и .mp4).
_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".mp4": "video/mp4",
}

# Файлы каталога — иммутабельные ассеты free-exercise-db под фиксированными
# именами (scripts/gen_exercise_demos.py их не переписывает на месте, а
# кладёт заново под тем же слагом): раз имя не меняется, кэшировать можно
# навсегда.
_CACHE_CONTROL = "public, max-age=31536000, immutable"


async def _owned_exercise(exercise_id: int, user_id: int):
    """Та же проверка владения, что и в api_v1.py: id из URL мог быть угадан
    и принадлежать чужому упражнению."""
    exercise = await db.get_exercise(exercise_id)
    if exercise is None or exercise["user_id"] != user_id:
        raise ApiError(404, "not_found", "exercise not found")
    return exercise


def media_file_url(path: str) -> str:
    """Абсолютный путь диска -> относительный URL раздачи /media/exercises/<name>.

    Публичная (без подчёркивания): тот же формат URL нужен и превью каталожного
    шаблона в api_v1_templates.py — маршрут `/media/exercises/{name}` ниже общий
    для своих упражнений и ещё не форкнутых шаблонов, второй раздачи для
    шаблонов заводить незачем."""
    return f"/media/exercises/{os.path.basename(path)}"


# Старое приватное имя — как было до того, как понадобилось использовать его
# из другого модуля.
_url_for = media_file_url


async def get_exercise_media(request: Request) -> JSONResponse:
    user_id = await common.authed_user_id(request)
    exercise_id = int(request.path_params["exercise_id"])
    exercise = await _owned_exercise(exercise_id, user_id)

    images = exercise_media.get_images_for(exercise)
    animation = exercise_media.get_animation_for(exercise)

    image_urls = [_url_for(p) for p in images]
    animation_url = _url_for(animation) if animation else None

    body: dict[str, Any] = {
        "images": image_urls,
        "animation": animation_url,
        "has_media": bool(image_urls or animation_url),
    }
    return JSONResponse(body)


async def get_exercise_description(request: Request) -> JSONResponse:
    user_id = await common.authed_user_id(request)
    exercise_id = int(request.path_params["exercise_id"])
    exercise = await _owned_exercise(exercise_id, user_id)

    lang = request.query_params.get("lang")
    text = exercise_descriptions.effective_description(exercise, lang)
    if not text:
        raise ApiError(404, "not_found", "no description for this exercise")
    return JSONResponse({"description": text})


async def get_media_file(request: Request) -> Any:
    """Раздача самого файла (картинки/клипа) по имени.

    Авторизация: НЕ требуем Bearer-токен здесь, сознательно. Это не данные
    пользователя — один и тот же набор картинок и клипов из открытой базы
    free-exercise-db (MIT), общий для всех пользователей и не зависящий от
    того, чьё это упражнение (проверка владения уже прошла на шаге
    /exercises/{id}/media, который и выдаёт эти URL). Требование токена тут
    не добавило бы приватности, зато сломало бы обычный `AsyncImage(url:)` в
    SwiftUI — ему нужен URL, который можно просто загрузить, без ручной
    прокидки заголовка Authorization в каждый запрос за картинкой.

    Защита от выхода за каталог — обязательная часть: имя из URL не
    подклеивается к пути "на слово", а после os.path.join прогоняется через
    os.path.realpath и сверяется префиксом с realpath(MEDIA_DIR). Так
    "../../etc/passwd" (или любой другой способ вылезти через "..") не может
    отдать файл вне каталога с картинками — get_media_file просто вернёт 404,
    как на любое другое отсутствующее имя.
    """
    name = request.path_params["name"]

    media_root = os.path.realpath(exercise_media.MEDIA_DIR)
    candidate = os.path.realpath(os.path.join(exercise_media.MEDIA_DIR, name))

    # Сравнение с os.sep на конце — чтобы "/media/exercises_evil" не прошёл
    # проверку как "начинается с /media/exercises".
    if candidate != media_root and not candidate.startswith(media_root + os.sep):
        raise ApiError(404, "not_found", "media file not found")

    if not os.path.isfile(candidate):
        raise ApiError(404, "not_found", "media file not found")

    ext = os.path.splitext(candidate)[1].lower()
    content_type = _CONTENT_TYPES.get(ext) or mimetypes.guess_type(candidate)[0] or "application/octet-stream"

    return FileResponse(
        candidate,
        media_type=content_type,
        headers={"Cache-Control": _CACHE_CONTROL},
    )


routes = [
    Route("/exercises/{exercise_id:int}/media", get_exercise_media, methods=["GET"]),
    Route("/exercises/{exercise_id:int}/description", get_exercise_description, methods=["GET"]),
    Route("/media/exercises/{name:path}", get_media_file, methods=["GET"]),
]
