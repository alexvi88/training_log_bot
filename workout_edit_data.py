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
