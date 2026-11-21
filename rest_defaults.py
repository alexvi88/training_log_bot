"""Отдых между подходами: цель по умолчанию для упражнения и чем её перебить.

Таймер отдыха в приложении раньше только считал вверх от последнего подхода —
без цели и без сигнала. Цель — число секунд у каждого упражнения в `/v1`
(`rest_seconds` в объекте упражнения, api_v1_common.exercise_json), а берётся
она так, по старшинству:

1. своя цель атлета у этого упражнения — `exercises.rest_seconds`
   (PATCH /v1/exercises/{id}, `null` — снять);
2. общая цель атлета — `users.rest_default_seconds` (PATCH /v1/settings): она
   заменяет авто-цель у всех упражнений без своей;
3. авто-цель по данным каталога (`auto_rest_seconds`), без всякой модели:

   - тяжёлое базовое со штангой (присед, тяги, жимы — есть паттерн движения
     `seed_data.movement_pattern`, снаряд — штанга) — 180 с;
   - прочее базовое — 120 с;
   - изоляция — 90 с;
   - пресс своим весом, удержание на время, движение без нагрузки — 60 с;
   - своё упражнение не из каталога — 120 с: что это за движение, мы не знаем,
     и середина вилки ошибается меньше всего.

Всё ключуется русской идентичностью шаблона (`original_name`, у копии —
`media_from`), а не показом: переименованное упражнение остаётся тем же
движением. Строк для показа модуль не держит — только числа.

Общая цель атлета едет до сериализатора через ContextVar, как язык запроса
(`i18n.current_lang`): её выставляет `api_v1_common.authed_user_id` из
`users` один раз на весь обработчик, и любой новый ответ с упражнением
получает её без своей проверки.
"""

from __future__ import annotations

from contextvars import ContextVar
from functools import lru_cache
from typing import Any, Optional

import seed_data

# Допустимая цель отдыха — и у упражнения, и общая: от 15 секунд до 15 минут.
MIN_REST_SECONDS = 15
MAX_REST_SECONDS = 900

HEAVY_BARBELL_COMPOUND = 180
COMPOUND = 120
ISOLATION = 90
LIGHT = 60
UNKNOWN = 120

# Снаряд упражнения в машинных токенах (`equipment` у распознавания по фото).
EQUIPMENT = ("barbell", "dumbbell", "machine", "cable", "bodyweight", "kettlebell", "other")

# Блок и кроссовер — тросы, а не рычажный тренажёр: у сайта это один «machine»
# (seo_catalog.equipment_of), тут различаем.
_CABLE_WORDS = ("блок", "кроссовер", "канат")

_user_default: ContextVar[Optional[int]] = ContextVar("rest_user_default", default=None)


def set_user_default(seconds: Any) -> None:
    """Общая цель атлета на остаток запроса (зовёт api_v1_common.authed_user_id)."""
    _user_default.set(seconds if valid_seconds(seconds) else None)


def user_default() -> Optional[int]:
    return _user_default.get()


def valid_seconds(value: Any) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool)
        and MIN_REST_SECONDS <= value <= MAX_REST_SECONDS
    )


@lru_cache(maxsize=None)
def _catalog_groups() -> dict[str, str]:
    """Идентичность шаблона → русское имя его группы. Без языка: застыть нечему."""
    return {name: group for group, name in seed_data.EXERCISE_TEMPLATES}


def is_catalog_identity(identity: Optional[str]) -> bool:
    return bool(identity) and identity in _catalog_groups()


def catalog_group(identity: str) -> Optional[str]:
    return _catalog_groups().get(identity)


@lru_cache(maxsize=None)
def equipment_of(identity: str) -> Optional[str]:
    """Снаряд шаблона каталога: один из EQUIPMENT или None — не из каталога.

    Основа — разбор сайта (`seo_catalog.equipment_of`, сверенный тестом на
    каждом показанном упражнении); тросы отделены от тренажёра по словам."""
    if not is_catalog_identity(identity):
        return None
    import seo_catalog  # тянет каталог сайта; нужен только здесь

    equipment = seo_catalog.equipment_of(identity)
    if equipment == "machine" and any(w in identity.lower() for w in _CABLE_WORDS):
        return "cable"
    return equipment or "other"


@lru_cache(maxsize=None)
def auto_rest_seconds(identity: Optional[str]) -> int:
    """Авто-цель отдыха по идентичности шаблона (см. докстринг модуля)."""
    if not is_catalog_identity(identity):
        return UNKNOWN
    group = catalog_group(identity)
    kind = seed_data.progression_kind(identity)
    equipment = equipment_of(identity)
    if kind in ("no_load", "timed"):
        return LIGHT
    if group == seed_data.ABS_GROUP_NAME and equipment == "bodyweight":
        return LIGHT
    if seed_data.movement_pattern(identity, group) is None:
        return ISOLATION
    if equipment == "barbell":
        return HEAVY_BARBELL_COMPOUND
    return COMPOUND


def _row_value(row: Any, key: str) -> Any:
    # `in` у sqlite3.Row ищет по значениям, поэтому имена — через keys().
    keys = row.keys() if hasattr(row, "keys") else ()
    return row[key] if key in keys else None


def exercise_identity(row: Any) -> Optional[str]:
    """Идентичность каталога, по которой считается авто-цель: своя или у копии —
    источника (`media_from`). Не из каталога — None."""
    for key in ("original_name", "media_from"):
        value = _row_value(row, key)
        if is_catalog_identity(value):
            return value
    return None


def override_of(row: Any) -> Optional[int]:
    value = _row_value(row, "rest_seconds")
    return value if valid_seconds(value) else None


def effective_rest_seconds(row: Any, user_default_seconds: Optional[int] = None) -> int:
    """Цель отдыха упражнения: своя → общая атлета → авто."""
    override = override_of(row)
    if override is not None:
        return override
    if valid_seconds(user_default_seconds):
        return user_default_seconds
    return auto_rest_seconds(exercise_identity(row))


def rest_json(row: Any) -> dict[str, Any]:
    """Поля `rest_seconds`/`rest_seconds_custom` объекта упражнения в `/v1`."""
    return {
        "rest_seconds": effective_rest_seconds(row, user_default()),
        "rest_seconds_custom": override_of(row) is not None,
    }
