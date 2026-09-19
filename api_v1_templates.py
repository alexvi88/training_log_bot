"""REST `/v1` для каталога упражнений (шаблоны из free-exercise-db).

В боте это handlers/exercises.py и handlers/routines.py: при подборе
упражнения (свой список пуст или не нашлось нужного) показываются шаблоны
каталога (`db.search_exercise_templates` / `db.list_templates_in_group`) —
превью с фото/техникой (`exercise_media`, `exercise_descriptions`), а
«➕ Добавить» форкает шаблон в свою копию (`db.fork_exercise_from_template`,
callback `tpladd`). GET /exercises отдаёт только уже свои упражнения — этот
модуль закрывает дыру, через которую в приложении вообще нечем было найти
шаблон, посмотреть его и завести себе, не изобретая новой семантики поверх
уже существующей в боте.

Шаблон (`is_template=1`, `user_id IS NULL`) — общий для всех read-only ряд,
владения у него нет; проверять есть что у самого форка (см. `_owned_template`,
которая лишь убеждается, что id вообще шаблон, а не чужое упражнение).
Имя шаблона в базе всегда каноническое (русское) — эндпоинты локализуют его на
рендере под `i18n.use_lang(user["lang"])`, тем же приёмом, что и
`/programs/catalog` в api_v1_programs.py.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import api_v1_common as common
import api_v1_media
import db
import exercise_descriptions
import exercise_media
import i18n
import seed_data

ApiError = common.ApiError
_authed_user_id = common.authed_user_id
_query_int = common.query_int


async def _owned_template(template_id: int):
    """"Владение" шаблоном — это просто "id существует и это шаблон, а не чужое
    личное упражнение": каталог общий, проверять здесь больше нечего (в
    отличие от _owned_exercise в других модулях, где важен user_id)."""
    template = await db.get_exercise(template_id)
    if template is None or not template["is_template"]:
        raise ApiError(404, "not_found", "template not found")
    return template


async def _user_lang(user_id: int) -> str:
    user = await db.get_user(user_id)
    return user["lang"] if user else i18n.DEFAULT_LANG


def _media_json(template) -> dict[str, Any]:
    """Тот же формат, что и GET /exercises/{id}/media (api_v1_media.py) —
    приложению незачем различать экран уже своего упражнения и превью ещё не
    форкнутого шаблона."""
    images = [api_v1_media.media_file_url(p) for p in exercise_media.get_images_for(template)]
    animation = exercise_media.get_animation_for(template)
    return {
        "images": images,
        "animation": api_v1_media.media_file_url(animation) if animation else None,
        "has_media": bool(images or animation),
    }


def _template_list_json(template, lang: str) -> dict[str, Any]:
    return {
        "id": template["id"],
        # `name`/`display_name` шаблона в базе всегда по-русски (см. докстринг
        # db.search_exercise_templates) — то, что видит атлет, локализуется тут.
        "name": seed_data.localized_exercise_name(template["name"], lang),
        "group_id": template["primary_group_id"],
    }


def _template_detail_json(template, lang: str) -> dict[str, Any]:
    data = _template_list_json(template, lang)
    data["description"] = exercise_descriptions.effective_description(template, lang)
    data["media"] = _media_json(template)
    return data


# ---------- поиск и просмотр каталога ----------

async def list_exercise_templates(request: Request) -> JSONResponse:
    """Ровно два режима поиска, как у бота: текстом (клавиатура «🔎 Поиск» в
    подборе упражнения) или по группе мышц (`db.list_templates_in_group`,
    просмотр каталога группы при создании нового упражнения). Один из двух
    параметров обязателен — без них отдавать «весь каталог» так же не
    задумано здесь, как и в db.search_exercise_templates при пустом query."""
    user_id = await _authed_user_id(request)
    lang = await _user_lang(user_id)
    query = request.query_params.get("query")
    group_id_param = request.query_params.get("group_id")
    with i18n.use_lang(lang):
        if query:
            limit = _query_int(request, "limit", 8, minimum=1, maximum=50)
            templates = await db.search_exercise_templates(user_id, query, limit=limit)
        elif group_id_param:
            try:
                group_id = int(group_id_param)
            except ValueError as exc:
                raise ApiError(400, "bad_request", "group_id must be int") from exc
            templates = await db.list_templates_in_group(group_id)
        else:
            raise ApiError(400, "bad_request", "query or group_id is required")
        payload = [_template_list_json(t, lang) for t in templates]
    return JSONResponse(payload)


async def get_exercise_template(request: Request) -> JSONResponse:
    """Превью шаблона (фото/клип/техника) до форка — «📋 <имя>» тап в боте,
    который открывает карточку с кнопкой «➕ Добавить», ничего ещё не заводя."""
    user_id = await _authed_user_id(request)
    template_id = int(request.path_params["template_id"])
    template = await _owned_template(template_id)
    lang = await _user_lang(user_id)
    with i18n.use_lang(lang):
        return JSONResponse(_template_detail_json(template, lang))


# ---------- форк в свою копию ----------

async def add_exercise_template(request: Request) -> JSONResponse:
    """«➕ Добавить» (tpladd) — db.fork_exercise_from_template одна на все
    входы (эта ручка, каталог группы, подбор в день программы, резолв при
    импорте CSV): своя копия под локализованным именем, но с идентичностью
    (`original_name`) и параметрами оригинала. Повторный вызов на уже
    форкнутый (и не заархивированный) шаблон возвращает ту же самую копию —
    как и в боте, это не ошибка."""
    user_id = await _authed_user_id(request)
    template_id = int(request.path_params["template_id"])
    await _owned_template(template_id)
    exercise_id = await db.fork_exercise_from_template(user_id, template_id)
    row = await db.get_exercise(exercise_id)
    return JSONResponse(common.exercise_json(row), status_code=201)


routes = [
    Route("/exercise-templates", list_exercise_templates, methods=["GET"]),
    Route("/exercise-templates/{template_id:int}", get_exercise_template, methods=["GET"]),
    Route("/exercise-templates/{template_id:int}/add", add_exercise_template, methods=["POST"]),
]
