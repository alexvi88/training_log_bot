"""Общий хвост после правки уже записанных сетов финализированной тренировки —
одна функция и для бота (handlers/edit_workout.py), и для `/v1`
(api_v1_account.py).

Тот же приём, что у progression_data.py/dashboard_data.py: РЕШЕНИЕ, что делает
правка помимо самого UPDATE/DELETE, живёт в одном месте. Добавить подход,
удалить подход или упражнение целиком — три разных изменения данных, но
последствия одни и те же:

- пустые блоки (упражнение добавили, а ни одного подхода не занесли, или
  последний подход только что удалили) не должны застревать строкой
  «подходов нет» в истории навсегда — delete_empty_blocks обычно вызывается
  один раз, в момент завершения тренировки, а тут её надо звать снова;
- закешированный AI-комментарий описывает числа, которых уже нет —
  сбрасывается, чтобы не показывать разбор несуществующего подхода;
- значки (весовой клуб, тоннаж, стрики) — производные от сетов, и правка
  задним числом должна и снимать их, и начислять заново, а не только влиять
  на новую тренировку.

Второй код этой логики в REST-слое рано или поздно разошёлся бы с ботом —
поправил бы один, забыл про другой.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

import achievement_sync
import db


async def on_workout_edited(workout_id: int, keep_block_id: Optional[int] = None) -> None:
    """Общий хвост после add-set/add-exercise/remove-exercise/edit-set/
    delete-set на уже записанной тренировке. keep_block_id щадит блок, на
    котором сейчас стоит пользователь в боте (см. handlers/edit_workout.py) —
    удаление последнего подхода не должно вырывать экран из-под него; вызовы
    из `/v1` его не используют."""
    await db.delete_empty_blocks(workout_id, keep_block_id=keep_block_id)
    await db.set_workout_ai_comment(workout_id, None)
    workout = await db.get_workout(workout_id)
    if workout is not None:
        await achievement_sync.resync(workout["user_id"])


async def move_workout_to_date(workout_id: int, new_date: dt.date) -> None:
    """Перенести уже записанную тренировку на другой календарный день,
    сохранив время суток и длительность — «📅 Дата» на экране правки в боте
    (handlers.edit_workout) и PATCH /v1/workouts/{id}/date (api_v1_account).

    Один UPDATE тут не обходится, и это не украшательство:

    - метки подходов едут следом (shift_workout_set_timestamps). Длительность
      на карточке считается по разбегу sets.created_at внутри окна
      «не позже finished_at» (view_builder.workout_duration_seconds); оставшись
      на старом дне, подходы целиком выпадают за это окно, и тренировка молча
      теряет своё «· 55 мин» — вместе с hall-of-fame.longest_workout_seconds;
    - сдвиг даты меняет, какая прошлая сессия считается «предыдущей» для
      каждого упражнения, так что закешированный AI-комментарий описывал бы
      сравнение, которого больше нет;
    - стрики и «1 января» читаются по календарному дню, поэтому значки
      пересчитываются целиком (resync, а не evaluate_after_finish): перенос
      работает в обе стороны — может и достроить серию, и разорвать уже
      засчитанную, — а начисляющий путь умеет только добавлять.

    new_date — календарный день ПОЛЬЗОВАТЕЛЯ (его выбирают в календаре бота и
    в приложении по местным часам), а started_at хранится по часам сервера,
    то есть в UTC. Поэтому день подставляется к МЕСТНОМУ времени старта, и
    результат переводится обратно в UTC. Склейка даты с UTC-временем
    напрямую уводила тренировку на соседний день у всех, чья местная полночь
    не совпадает с UTC: утренняя тренировка в UTC+10 (вечер предыдущих суток
    по UTC) после переноса на 5-е оказывалась 6-го.
    """
    workout = await db.get_workout(workout_id)
    if workout is None:
        return
    offset = dt.timedelta(hours=await db.user_tz_offset(workout["user_id"]))
    started = dt.datetime.fromisoformat(workout["started_at"])
    finished = (
        dt.datetime.fromisoformat(workout["finished_at"]) if workout["finished_at"] else None
    )
    local_started = started + offset
    new_started = dt.datetime.combine(new_date, local_started.time()) - offset
    shift = new_started - started
    new_finished = (finished + shift).isoformat(timespec="seconds") if finished else None
    await db.update_workout_date(
        workout_id, new_started.isoformat(timespec="seconds"), new_finished
    )
    await db.shift_workout_set_timestamps(workout_id, shift.total_seconds())
    await db.set_workout_ai_comment(workout_id, None)
    await achievement_sync.resync(workout["user_id"])
