"""Продуктовые события жизненного цикла атлета, которые пишет сама база.

Зачем в базе, а не в хендлерах. Подход записывается из бота (кнопки, строка,
голос, правка прошлой тренировки), из приложения (ряд повторов, строка,
очередь без связи, экран блокировки), тренером-моделью; тренировка
начинается из пяти мест, программа заводится из восьми. Событие у каждого
вызывающего забылось бы в девятом. Поэтому `db.append_set`, `db.finish_workout`,
`db.create_program` и соседи сами кладут строку в `analytics_events` — той же
транзакцией, что и само действие: нет действия — нет события, и наоборот.

Откуда пришло действие, база сама не знает — это знает вход:

- бот — `main.SetUserLanguageMiddleware` ставит `tg` на каждый апдейт;
- REST `/v1` — `api_v1_common.authed_user_id` ставит `ios` и версию
  приложения из `X-App-Version` один раз на весь обработчик.

Контекст — ContextVar, как язык (`i18n.current_lang`): задача, порождённая
хендлером, наследует его. Контекста нет (фоновая задача, скрипт, тест,
досчёт сводки) — событие не пишется вовсе: событие без клиента в сводке
считалось бы «активным» человеком (`db.active_users_by_platform`), а действие
без человека за ним активностью не является.

Имена событий постоянные, свойства — короткие токены из закрытых наборов
ниже. Ни текста, ни id записей.
"""

from __future__ import annotations

import contextlib
import re
from contextvars import ContextVar
from typing import Iterator, Optional

PLATFORM_TG = "tg"
PLATFORM_IOS = "ios"

# --- имена событий ---------------------------------------------------------------

SIGNUP = "signup"
PROGRAM_CREATED = "program_created"
WORKOUT_STARTED = "workout_started"
SET_LOGGED = "set_logged"
WORKOUT_FINISHED = "workout_finished"
FIRST_SET = "first_set"
FIRST_WORKOUT_FINISHED = "first_workout_finished"
SECOND_WORKOUT_FINISHED = "second_workout_finished"
COACH_LINKED = "coach_linked"
AI_QUESTION = "ai_question"
VIDEO_REVIEW = "video_review"
IMPORT_DONE = "import_done"

# Вехи — один раз на человека (см. db._emit_event, once=True).
MILESTONES = frozenset({FIRST_SET, FIRST_WORKOUT_FINISHED, SECOND_WORKOUT_FINISHED})

ALL_EVENTS = frozenset({
    SIGNUP, PROGRAM_CREATED, WORKOUT_STARTED, SET_LOGGED, WORKOUT_FINISHED,
    FIRST_SET, FIRST_WORKOUT_FINISHED, SECOND_WORKOUT_FINISHED,
    COACH_LINKED, AI_QUESTION, VIDEO_REVIEW, IMPORT_DONE,
})

# --- значения свойств ------------------------------------------------------------

# Откуда подход: бот, приложение, кнопка повторов на экране блокировки (Live
# Activity), часы. Приложение шлёт `source` в теле POST /v1/workouts/{id}/sets
# (старые сборки — не шлют, тогда `app`). Импорт подходов по одному не пишет:
# его итог — одно `import_done` (иначе год истории из Hevy — тысячи
# `set_logged` в одну минуту).
SET_SOURCE_BOT = "bot"
SET_SOURCE_APP = "app"
SET_SOURCES = frozenset({SET_SOURCE_BOT, SET_SOURCE_APP, "lock_screen", "watch"})
# Что приложение вправе назвать само.
CLIENT_SET_SOURCES = frozenset({SET_SOURCE_APP, "lock_screen", "watch"})

# Откуда программа (`programs.source` → origin).
PROGRAM_ORIGINS = {
    "ai": "ai",
    "catalog": "catalog",
    "manual": "manual",
    "coach": "coach",
    "import": "import",  # чужая программа по ссылке-визитке (handlers/sharing)
    "workout": "workout",  # «сохранить тренировку программой»
}
OTHER = "other"

IMPORT_SOURCES = frozenset({"hevy", "strong", "csv", "notes"})
AI_QUESTION_KINDS = frozenset({"text", "photo", "video", "voice"})

# --- контекст --------------------------------------------------------------------

_platform: ContextVar[Optional[str]] = ContextVar("lifecycle_platform", default=None)
_app_version: ContextVar[Optional[str]] = ContextVar("lifecycle_app_version", default=None)

_MAX_VERSION_LEN = 32


def enter(platform: str, app_version: Optional[str] = None) -> None:
    """Выставить клиента на остаток задачи — апдейт бота или запрос /v1."""
    _platform.set(platform)
    version = app_version.strip()[:_MAX_VERSION_LEN] if isinstance(app_version, str) else None
    _app_version.set(version or None)


@contextlib.contextmanager
def using(platform: Optional[str], app_version: Optional[str] = None) -> Iterator[None]:
    """То же на время блока — тестам и фоновым задачам с известным клиентом."""
    t1 = _platform.set(platform)
    t2 = _app_version.set(app_version)
    try:
        yield
    finally:
        _platform.reset(t1)
        _app_version.reset(t2)


def platform() -> Optional[str]:
    return _platform.get()


def app_version() -> Optional[str]:
    return _app_version.get()


def default_set_source() -> str:
    return SET_SOURCE_APP if _platform.get() == PLATFORM_IOS else SET_SOURCE_BOT


def client_set_source(value: object) -> Optional[str]:
    """`source` из тела запроса приложения: только известные токены."""
    if isinstance(value, str) and value.strip().lower() in CLIENT_SET_SOURCES:
        return value.strip().lower()
    return None


# --- корзины ---------------------------------------------------------------------
#
# Точное число подходов или минут — тоже не текст, но и не нужно: воронке
# хватает корзин, а у корзин имена не меняются, когда меняется распределение.


def sets_bucket(n: int) -> str:
    if n <= 0:
        return "0"
    if n <= 5:
        return "1_5"
    if n <= 10:
        return "6_10"
    if n <= 20:
        return "11_20"
    if n <= 30:
        return "21_30"
    return "31p"


def duration_bucket(seconds: Optional[float]) -> str:
    """`none` — длительности нет: занесение задним числом или закрытая задним
    числом зависшая тренировка (конец поставлен на начало)."""
    if seconds is None or seconds <= 0:
        return "none"
    minutes = seconds / 60
    if minutes < 15:
        return "lt15"
    if minutes < 30:
        return "15_30"
    if minutes < 60:
        return "30_60"
    if minutes < 90:
        return "60_90"
    if minutes < 120:
        return "90_120"
    return "120p"


def count_bucket(n: int) -> str:
    """Тренировок в импорте."""
    if n <= 0:
        return "0"
    if n <= 10:
        return "1_10"
    if n <= 50:
        return "11_50"
    if n <= 200:
        return "51_200"
    return "201p"


def program_origin(source: Optional[str]) -> str:
    return PROGRAM_ORIGINS.get(source or "", OTHER)


def import_source(source: Optional[str]) -> str:
    return source if source in IMPORT_SOURCES else OTHER


def ai_question_kind(kind: Optional[str]) -> str:
    return kind if kind in AI_QUESTION_KINDS else "text"


_SOURCE = re.compile(r"[a-z0-9_-]{1,40}")


def signup_source(source: Optional[str]) -> Optional[str]:
    """Метка источника (`acquisition`: organic, referral, src_<слаг>…) — она
    уже очищена разбором deep link'а, здесь только страховка формы."""
    if isinstance(source, str) and _SOURCE.fullmatch(source):
        return source
    return None
