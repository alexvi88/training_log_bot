"""Картинка-визитка тренировки для шаринга — «🖼 Картинка» в истории бота, тот
же расчёт нужен и `/v1` (API-дыра #1: приложению нечем поделиться тренировкой).

Один расчёт на обоих потребителей, как и у `progression_data.py`: цифры и
подписи собирает `formatting.build_workout_card`, растр рисует
`charts.render_workout_card` — рендер синхронный (matplotlib) и уходит в
отдельный поток, чтобы не блокировать event loop ни бота, ни REST-запроса.

Владение тренировкой модуль не проверяет — это делает вызывающий (в боте это
`workout["user_id"] != callback.from_user.id`, в `/v1` — `_owned_workout`),
ровно как `progression_data.hint_for_workout` полагается на проверку снаружи.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
from typing import Optional

import charts
import db
import formatting
import i18n
import view_builder


@dataclasses.dataclass(frozen=True)
class WorkoutCardImage:
    png: bytes
    title: str
    footer: str


async def build(workout_id: int, user, theme: str = "bot") -> Optional[WorkoutCardImage]:
    """Готовая картинка тренировки, или `None`, если тренировка не найдена.

    `user` — строка `users` того, кто просит карточку (для языка, единиц и
    формулы e1RM); её достаёт вызывающий, а не этот модуль, — ровно как
    `hint_for_workout` в `progression_data.py`.

    `theme` — палитра растра (см. `charts.render_workout_card`): "bot"
    (по умолчанию, тёмная терминальная — как в Telegram) или "app" (светлая,
    в цветах iOS-приложения). Бот своё значение не передаёт вовсе — ему
    положен дефолт; `api_v1_history.py` просит "app" явно.
    """
    workout = await db.get_workout(workout_id)
    if workout is None:
        return None
    blocks = await view_builder.build_block_views(
        workout_id, user["e1rm_formula"], mark_records=True
    )
    started = dt.datetime.fromisoformat(workout["started_at"])
    with i18n.use_lang(user["lang"]):
        title, body, footer, note = formatting.build_workout_card(
            started, blocks, workout["note"], unit=user["unit"]
        )
    png = await asyncio.to_thread(charts.render_workout_card, title, body, footer, note, theme)
    return WorkoutCardImage(png=png, title=title, footer=footer)
