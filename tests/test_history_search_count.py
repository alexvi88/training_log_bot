"""Поиск по истории («жим» и т.п.) врал про число найденных тренировок,
когда их было больше 20.

`db.search_workouts_by_exercise` режет выдачу `LIMIT 20` по умолчанию, а
`handlers.history.hist_search` печатал в заголовке `len(entries)` — то есть
«сколько влезло под лимит», а не «сколько всего нашлось». Прямое нарушение
правила TONE_OF_VOICE «любое утверждение о данных пользователя отправляется
только когда данные его подтверждают».
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from handlers import history


def _make_message(user_id: int, text: str):
    msg = MagicMock()
    msg.from_user = SimpleNamespace(id=user_id, username="tester")
    msg.text = text
    msg.delete = AsyncMock()
    msg.answer = AsyncMock()
    return msg


async def _log_bench_workouts(db, user_id: int, count: int) -> int:
    gid = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим штанги лёжа", gid)
    for i in range(count):
        started = f"2026-01-{i + 1:02d}T12:00:00"
        workout_id = await db.create_finished_workout(user_id, started, started, source="live")
        block_id = await db.create_block(workout_id, "single")
        await db.add_block_exercise(block_id, ex_id, 0)
        await db.add_set(block_id, ex_id, 1, 0, 100.0, 5, None)
    return ex_id


async def test_header_shows_the_real_total_when_more_than_20_match(fresh_db, user_id):
    db = fresh_db
    await _log_bench_workouts(db, user_id, 25)
    message = _make_message(user_id, "жим")

    await history.hist_search(message, state=SimpleNamespace(get_state=None))

    text = message.answer.await_args.args[0]
    assert "25" in text
    assert "20 из 25" in text  # список честно помечен как урезанный


async def test_header_shows_plain_count_when_everything_fits(fresh_db, user_id):
    db = fresh_db
    await _log_bench_workouts(db, user_id, 3)
    message = _make_message(user_id, "жим")

    await history.hist_search(message, state=SimpleNamespace(get_state=None))

    text = message.answer.await_args.args[0]
    assert "«жим»: 3" in text
    assert "из" not in text
