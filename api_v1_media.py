"""REST `/v1` для демонстраций упражнений: фото старт/конец, клип, описание.

В боте это раздаёт handlers/exercises.py, читая файлы с диска и отправляя их
в Telegram (exercise_media.py) плюс текст техники (exercise_descriptions.py).
Здесь то же самое, только по HTTP: файл со старта/конца, зацикленный клип
(если уже снят) и текстовая инструкция для карточки упражнения в приложении.

Здесь же — СВОЁ фото упражнения (GET/POST/DELETE `/exercises/{id}/photo`),
и вот оно как раз про конкретное упражнение конкретного человека: хранится
файлом на нашем диске (exercise_photos.py, колонка `exercises.custom_photo_path`),
отдаётся только владельцу и, в отличие от каталожных ассетов, может меняться —
поэтому у него своя проверка владения и свой заголовок кэша, см. ниже.

Ключ поиска для каталожной части — не id упражнения, а имя шаблона (exercise_media.catalog_key,
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

import api_v1_ai
import api_v1_common as common
import db
import exercise_descriptions
import exercise_media
import exercise_photos
import i18n
from handlers.ai_trainer import MAX_IMAGE_BYTES

ApiError = common.ApiError

# Content-Type по расширению: свой, а не общесистемный mimetypes.guess_type,
# чтобы не зависеть от того, что знает /etc/mime.types в контейнере — набор
# расширений тут и так фиксирован (exercise_media хранит только .jpg и .mp4).
_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".mp4": "video/mp4",
    # png/webp — только у своих фото: их присылает человек из приложения, и
    # набор форматов тут тот же, что у остальных фото в `/v1`
    # (api_v1_ai.IMAGE_EXTENSION_BY_MIME).
    ".png": "image/png",
    ".webp": "image/webp",
}

# Файлы каталога — иммутабельные ассеты free-exercise-db под фиксированными
# именами (scripts/gen_exercise_demos.py их не переписывает на месте, а
# кладёт заново под тем же слагом): раз имя не меняется, кэшировать можно
# навсегда.
_CACHE_CONTROL = "public, max-age=31536000, immutable"

# Своё фото — ровно наоборот. Оно приватное (это данные конкретного человека,
# а не открытая база) и может смениться в любой момент: тот же URL завтра
# отдаст другую картинку, так что `immutable` тут прямо противопоказан — после
# замены фото клиент показывал бы старое, пока не кончится год. `must-revalidate`
# при нулевом возрасте означает «спроси сервер», а сам ответ дешёвый: FileResponse
# отдаёт ETag/Last-Modified, и неизменившееся фото вернётся 304-м без тела.
_PHOTO_CACHE_CONTROL = "private, max-age=0, must-revalidate"


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



# ---------- своё фото упражнения ----------


async def get_exercise_photo(request: Request) -> Any:
    """Само фото байтами, а не ссылкой на него.

    Почему не отдельный публичный URL, как у каталожных картинок (см.
    get_media_file ниже): то — общая открытая база, одинаковая для всех, а это
    фотография конкретного человека, и открывать её любому, кто угадает имя
    файла, нельзя. Значит, нужен Bearer-токен, значит, `AsyncImage(url:)`
    отпадает и приложение всё равно грузит байты своим запросом — при таком
    раскладе промежуточный JSON со ссылкой не даёт ничего, кроме второго
    похода на сервер.

    Фото нет — 404, тем же кодом и текстом, каким `/v1` отвечает на «нет
    описания»: отсутствие фото это нормальный ответ, а не поломка.
    """
    user_id = await common.authed_user_id(request)
    exercise_id = int(request.path_params["exercise_id"])
    exercise = await _owned_exercise(exercise_id, user_id)

    path = exercise_photos.path_for_exercise(exercise)
    if path is None:
        raise ApiError(404, "not_found", "no photo for this exercise")

    ext = os.path.splitext(path)[1].lower()
    content_type = _CONTENT_TYPES.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"
    return FileResponse(path, media_type=content_type, headers={"Cache-Control": _PHOTO_CACHE_CONTROL})


async def upload_exercise_photo(request: Request) -> JSONResponse:
    """Загрузить своё фото упражнения — `{"image_data_url": "data:image/jpeg;base64,..."}`.

    Формат тела и лимиты — общие с остальными фото в `/v1` (api_v1_ai:
    IMAGE_EXTENSION_BY_MIME и MAX_IMAGE_BYTES, разбор — common.decode_data_url):
    своих чисел и своего набора форматов здесь нет намеренно, иначе одно и то
    же фото прошло бы в вопросе тренеру и не прошло бы тут.

    `custom_photo_file_id` при этом обнуляется (db.set_exercise_photo с одним
    только именем файла): старая ссылка ведёт на ПРЕЖНЮЮ картинку, и оставить
    её значило бы показывать в Telegram одно фото, а в приложении другое. Бот
    отправит новое файлом с диска и сам запомнит свежий file_id.
    """
    user_id = await common.authed_user_id(request)
    exercise_id = int(request.path_params["exercise_id"])
    await _owned_exercise(exercise_id, user_id)

    body = await common.json_body(request)
    data_url = common.require(body, "image_data_url", str)
    raw, _mime, ext = common.decode_data_url(
        data_url, api_v1_ai.IMAGE_EXTENSION_BY_MIME, field="image_data_url"
    )
    if len(raw) > MAX_IMAGE_BYTES:
        raise ApiError(
            400, "photo_too_big", i18n.t("ai.screen.photo_too_big", mb=MAX_IMAGE_BYTES // (1024 * 1024))
        )

    name = exercise_photos.save(exercise_id, raw, ext)
    await db.set_exercise_photo(exercise_id, None, name)
    return JSONResponse({"has_photo": True}, status_code=201)


async def delete_exercise_photo(request: Request) -> JSONResponse:
    """Убрать своё фото — карточка упражнения возвращается к каталожным
    картинкам, если они для него есть. Нет фото — 404, как и у GET: удалять
    нечего, и молчаливое «удалил» тут врало бы."""
    user_id = await common.authed_user_id(request)
    exercise_id = int(request.path_params["exercise_id"])
    exercise = await _owned_exercise(exercise_id, user_id)
    if not exercise_photos.has_photo(exercise):
        raise ApiError(404, "not_found", "no photo for this exercise")
    await db.delete_exercise_photo(exercise_id)
    return JSONResponse({"deleted": True})


routes = [
    Route("/exercises/{exercise_id:int}/media", get_exercise_media, methods=["GET"]),
    Route("/exercises/{exercise_id:int}/description", get_exercise_description, methods=["GET"]),
    Route("/exercises/{exercise_id:int}/photo", get_exercise_photo, methods=["GET"]),
    Route("/exercises/{exercise_id:int}/photo", upload_exercise_photo, methods=["POST"]),
    Route("/exercises/{exercise_id:int}/photo", delete_exercise_photo, methods=["DELETE"]),
    Route("/media/exercises/{name:path}", get_media_file, methods=["GET"]),
]
