"""Действия из iOS-приложения — в ту же ленту, что и действия из бота.

Зачем. Лента `/activity` (см. activity_log.py и db.user_events) отвечает на
вопрос «как пользуются», и до этого модуля она отвечала на него ровно наполовину:
в ней были только события из Telegram. Человек, записавший всю тренировку в
приложении, выглядел в ней как человек, который сутки ничего не делал. Второй
ленты специально не заводим: вопрос задаётся про атлета, а не про клиент, и один
и тот же атлет за день успевает и записать подход в приложении, и нажать кнопку
в боте — разъехавшись по двум экранам, эти половины перестают складываться.

Как. Одна middleware Starlette поверх всего `/v1` (подключается в
`api_v1.build_app`), а не запись на каждом обработчике: обработчиков под сотню,
и запись, которую надо не забыть дописать в каждый новый, забывается на первом же.

Три решения, без которых лента становится мусором:

- Пишем ПОСЛЕ обработчика и только для аутентифицированных запросов. `telegram_id`
  берём из `request.state.user_id`, куда его кладёт `api_v1_common.authed_user_id`
  — то есть у уже разрезолвленного токена, без второго похода в базу за тем же
  ответом. Нет пометки — значит обработчик не спрашивал, кто это (`/health`,
  `/auth/*`), или спросил и получил 401: события без пользователя в ленту не
  положить.
- Пишем фразой, а не путём. `POST /workouts/12/sets` рядом с «нажал кнопку
  "🏁 Завершить"» читается как мусор, и лента, которую неприятно читать, не
  читается вовсе.
- Только POST/PATCH/DELETE. Лента про то, что человек ДЕЛАЕТ, а не про то, что
  подгрузил экран: одно открытие приложения — это десяток GET'ов, и они утопили
  бы в себе настоящие действия.
"""

from __future__ import annotations

import json
import logging
import re

from starlette.middleware.base import BaseHTTPMiddleware

import activity_log
import db

logger = logging.getLogger(__name__)

# Методы, которые что-то меняют. GET (и HEAD/OPTIONS заодно) не пишем — см.
# докстроку модуля.
WRITING_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})

# Фразы для самых частых действий: «метод + шаблон маршрута» → что человек
# сделал. Ключ — именно ШАБЛОН (`/workouts/{workout_id}/sets`), а не конкретный
# путь: по конкретному в ленте оказалась бы тысяча разных строк на одно и то же
# действие, и повторяющегося поведения в ней было бы не разглядеть.
ACTION_PHRASES: dict[tuple[str, str], str] = {
    ("POST", "/workouts/active"): "начал тренировку",
    ("POST", "/workouts/{workout_id}/sets"): "записал подход",
    ("POST", "/workouts/{workout_id}/sets/parse"): "записал подход строкой",
    ("DELETE", "/workouts/{workout_id}/exercises/{exercise_id}/last-set"): "удалил подход",
    ("DELETE", "/workouts/{workout_id}/sets/{set_id}"): "удалил подход",
    ("POST", "/workouts/{workout_id}/finish"): "закончил тренировку",
    ("POST", "/food"): "добавил еду",
    ("POST", "/food/parse"): "распознал еду",
    ("POST", "/ai/ask"): "спросил тренера",
    ("POST", "/workouts/backfill"): "начал занесение задним числом",
    ("PATCH", "/workouts/{workout_id}/date"): "перенёс дату тренировки",
    ("POST", "/import/csv"): "импортировал CSV",
    ("POST", "/ai/program/save"): "сохранил программу от тренера",
    ("POST", "/ai/program/train"): "начал тренировку по плану от тренера",
    ("POST", "/workouts/{workout_id}/sets/voice"): "записал подход голосом",
    ("PATCH", "/workouts/{workout_id}/sets/{set_id}"): "исправил подход",
    ("DELETE", "/workouts/{workout_id}/exercises/{exercise_id}"): "убрал упражнение из тренировки",
    ("DELETE", "/workouts/active"): "удалил идущую тренировку",
    ("DELETE", "/workouts/{workout_id}"): "удалил тренировку",
    ("PATCH", "/workouts/{workout_id}/note"): "написал заметку к тренировке",
    ("POST", "/workouts/{workout_id}/ai-comment"): "попросил разбор тренировки",
    ("POST", "/workouts/{workout_id}/routines"): "сохранил тренировку днём программы",
    ("POST", "/workouts/{workout_id}/repeat"): "повторил тренировку",
    ("POST", "/exercises"): "создал упражнение",
    ("PATCH", "/exercises/{exercise_id}"): "изменил упражнение",
    ("POST", "/exercises/{exercise_id}/archive"): "убрал упражнение в архив",
    ("POST", "/exercises/{exercise_id}/unarchive"): "вернул упражнение из архива",
    ("POST", "/bodyweight"): "записал вес тела",
    ("POST", "/ai/video"): "прислал видео тренеру",
    ("POST", "/ai/questions/answer"): "ответил на вопрос тренера",
    ("POST", "/support/messages"): "написал в поддержку",
    ("POST", "/feedback"): "оставил отзыв",
    ("PATCH", "/settings"): "поменял настройки",
    ("POST", "/programs/catalog/{key}"): "добавил готовую программу",
    ("POST", "/routines/{routine_id}/exercises"): "добавил упражнение в день программы",
}

# Что из тела запроса показать в ленте рядом с фразой — то, что в боте видно
# само: набранный текст («100 8»), вопрос тренеру, надпись кнопки. Без этого
# лента приложения читалась «записал подход, записал подход, записал подход»,
# и что именно человек записал, было не узнать (владелец: «какие подходы чел
# записал, не проваливается в /activity»). Список ключей — белый: пароли,
# токены и base64 фото/видео в ленту не попадают ни при каком новом маршруте.
_TEXT_FIELDS = ("text", "question", "answer", "name", "note", "description")
# Короткие настройки (`PATCH /settings`) — «unit=lb», «lang=en».
_SCALAR_FIELDS = ("unit", "lang", "e1rm_formula", "timezone", "goal")
# Тело больше этого — почти наверняка фото или видео: не разбираем.
_MAX_DETAIL_BODY = 64 * 1024
_MAX_TEXT = 200

# Вид события в ленте. Отдельный от телеграмных KIND_*: «нажал кнопку» и
# «сделал запрос» — разные вещи, и смешивать их под одним видом значит потерять
# возможность спросить у базы «а что вообще делают из приложения».
KIND_API_ACTION = "api_action"

_CONVERTER = re.compile(r"{(\w+):[^}]+}")


def _template(path: str) -> str:
    """`/workouts/{workout_id:int}/sets` → `/workouts/{workout_id}/sets`.

    Конвертер Starlette — деталь маршрутизации, и в ленте от него один шум;
    словарь фраз ключуется по чистому шаблону, чтобы его можно было читать и
    дополнять, не помня, где `:int`, а где `:path`.
    """
    return _CONVERTER.sub(r"{\1}", path)


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 1] + "…"


def _number(value) -> str:
    """80.0 → «80», 82.5 → «82.5» — как человек и набирал."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


async def describe_detail(body: dict, path_params: dict) -> str:
    """Подробности действия одной строкой: «Жим лёжа · 100×8», «Присед · «100 8»».

    Упражнение — по `exercise_id` из тела или пути, названием, как его видит
    человек; день программы — по `routine_id`. Незнакомое упражнение (чужое,
    удалённое) просто не называем: лента — не место для 404.
    """
    parts: list[str] = []
    exercise_id = body.get("exercise_id", path_params.get("exercise_id"))
    if isinstance(exercise_id, int) and not isinstance(exercise_id, bool):
        exercise = await db.get_exercise(exercise_id)
        if exercise is not None:
            parts.append(exercise["display_name"])
    routine_id = body.get("routine_id")
    if isinstance(routine_id, int) and not isinstance(routine_id, bool):
        routine = await db.get_routine(routine_id)
        if routine is not None:
            parts.append(routine["name"])
    weight, reps = body.get("weight"), body.get("reps")
    if reps is not None:
        line = f"{_number(weight)}×{_number(reps)}" if weight is not None else f"×{_number(reps)}"
        if body.get("rpe") is not None:
            line += f" @{_number(body['rpe'])}"
        parts.append(line)
    elif weight is not None:
        parts.append(_number(weight))
    for key in _TEXT_FIELDS:
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(f"«{_clip(value)}»")
    for key in _SCALAR_FIELDS:
        value = body.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            parts.append(f"{key}={_clip(str(value))}")
    return " · ".join(parts)


async def _request_json(request) -> dict:
    """Тело запроса словарём — или пустой словарь, если это не JSON-объект.

    Starlette кэширует прочитанное тело и отдаёт его обработчику заново, так
    что чтение здесь ничего у обработчика не отнимает."""
    if "json" not in request.headers.get("content-type", ""):
        return {}
    body = await request.body()
    if not body or len(body) > _MAX_DETAIL_BODY:
        return {}
    try:
        data = json.loads(body)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def describe(method: str, template: str) -> str:
    """Фраза для ленты. Незнакомому маршруту — честное «метод путь».

    Фолбэк нарочно не «прочерк» и не пропуск записи: новый эндпоинт должен быть
    в ленте видно сразу, пусть и машинной строкой, иначе он молча выпадет из
    картины до тех пор, пока кто-нибудь не вспомнит дописать сюда фразу.
    """
    phrase = ACTION_PHRASES.get((method, template))
    if phrase:
        return phrase
    return f"{method} {template}"


class LogApiActions(BaseHTTPMiddleware):
    """Каждый успешный меняющий запрос к `/v1` — событием в общую ленту."""

    def __init__(self, app, routes):
        super().__init__(app)
        # Шаблон маршрута по его обработчику. Собираем один раз на сборку
        # приложения, а не ищем маршрут заново на каждый запрос: роутинг уже
        # отработал к моменту, когда нас зовут, и повторять его — платить за
        # одно и то же дважды. Ключ — сама функция-обработчик: Starlette кладёт
        # её в scope["endpoint"], и в /v1 каждая занята ровно одним маршрутом.
        self._templates = {
            route.endpoint: _template(route.path)
            for route in routes
            if getattr(route, "endpoint", None) is not None
        }

    async def dispatch(self, request, call_next):
        # Тело читаем ДО обработчика: после него поток уже выбран. Только у
        # меняющих запросов — GET в ленту не пишутся вовсе.
        body: dict = {}
        if request.method.upper() in WRITING_METHODS:
            try:
                body = await _request_json(request)
            except Exception:
                logger.exception("Failed to read api action body")
        response = await call_next(request)
        self._log_failure(request, response)
        try:
            await self._record(request, response, body)
        except Exception:
            # Ровно как в activity_log.LogIncomingMessages: лог действий не тот
            # повод, чтобы человеку не засчиталась тренировка.
            logger.exception("Failed to log api action")
        return response

    def _log_failure(self, request, response) -> None:
        """Неуспешный ответ — строкой в журнал сервиса, каким бы ни был метод.

        Лента (`_record` ниже) намеренно знает только про POST/PATCH/DELETE:
        одно открытие приложения — это десяток GET'ов, и они утопили бы в себе
        настоящие действия. Но у этого правила оказалась цена: когда экран в
        приложении показывал пустоту вместо данных, в журнале не было НИ ОДНОЙ
        строки про тот запрос, и отличить «сервер ответил пусто» от «сервер
        ответил 404» было нечем — ни с телефона, ни из логов.

        Поэтому отказы пишутся всегда и всеми методами: их мало по определению,
        засорить журнал они не могут, зато сразу видно, какой маршрут и каким
        кодом ответил. Успешные GET'ы по-прежнему молчат.
        """
        if response.status_code < 400:
            return
        template = self._templates.get(request.scope.get("endpoint"), request.url.path)
        user_id = getattr(request.state, "user_id", None)
        who = f"user={user_id}" if user_id is not None else "anon"
        logger.warning(
            "[ios] %s | %s %s | %s | %s",
            who,
            request.method.upper(),
            request.url.path,
            template,
            response.status_code,
        )

    async def _record(self, request, response, body: dict | None = None) -> None:
        method = request.method.upper()
        if method not in WRITING_METHODS:
            return
        # 4xx/5xx — это не действие, а отказ: тренировка не началась, подход не
        # записался. В ленте «начал тренировку» напротив запроса, который вернул
        # 409, читалось бы прямой неправдой.
        if response.status_code >= 400:
            return
        user_id = getattr(request.state, "user_id", None)
        if user_id is None:
            return

        template = self._templates.get(request.scope.get("endpoint"), request.url.path)
        # Удаление аккаунта: запрос уже снёс всё, что о человеке было, и
        # событие «удалил аккаунт» под его id было бы первой строкой нового
        # следа — после «удалить всё» в базе оставалась user_events на него.
        if method == "DELETE" and template == "/account":
            return
        action = describe(method, template)
        detail = await describe_detail(body or {}, dict(request.path_params))
        if detail:
            action = f"{action}: {detail}"
        path = request.url.path
        await db.log_user_event(
            user_id,
            KIND_API_ACTION,
            action,
            # payload — метод и путь как есть: по фразе видно, ЧТО сделали, а
            # докопаться до конкретного запроса (какая тренировка, какой подход)
            # можно только по настоящему пути с id.
            f"{method} {path}",
            source=activity_log.SOURCE_IOS,
        )
        logger.info(
            "%s",
            activity_log.process_log_line(
                activity_log.SOURCE_IOS, user_id, action, f"{method} {path}", str(response.status_code)
            ),
        )
