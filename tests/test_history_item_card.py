"""The history detail screen (hist:item) must show the same tonnage-equivalent
and PR-highlight content as the just-finished completion card — previously it
only rendered the bare sets, making a past workout look poorer than the one
you just logged."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery

from handlers import history

pytestmark = pytest.mark.asyncio


def _make_callback(user_id: int, data: str):
    message = MagicMock()
    message.text = "some previous screen"
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 1
    message.edit_text = AsyncMock(return_value=message)
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=2))
    message.delete = AsyncMock()
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def test_history_item_includes_tonnage_equivalent(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Ноги")
    squat = await db.create_exercise(user_id, "Присед", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, squat, 0)
    # 200kg x 10 = 2000kg tonnage, comfortably above the "это как N ..." threshold.
    await db.add_set(block_id, squat, 1, 0, 200.0, 10)
    await db.finish_workout(workout_id)

    callback = _make_callback(user_id, f"hist:item:{workout_id}")

    assert await history.show_history_item(callback, workout_id)

    text = callback.message.answer.await_args.args[0]
    assert "Суммарно за тренировку" in text
    assert "Это как" in text


async def test_history_item_marks_record_inside_the_exercise(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Жим лёжа", group_id)

    w1 = await db.create_workout(user_id, started_at="2026-01-01T12:00:00")
    b1 = await db.create_block(w1, "single")
    await db.add_block_exercise(b1, bench, 0)
    await db.add_set(b1, bench, 1, 0, 80.0, 5)
    await db.finish_workout(w1, finished_at="2026-01-01T12:00:00")

    w2 = await db.create_workout(user_id, started_at="2026-01-08T12:00:00")
    b2 = await db.create_block(w2, "single")
    await db.add_block_exercise(b2, bench, 0)
    await db.add_set(b2, bench, 1, 0, 90.0, 5)
    await db.finish_workout(w2, finished_at="2026-01-08T12:00:00")

    callback = _make_callback(user_id, f"hist:item:{w2}")

    assert await history.show_history_item(callback, w2)

    text = callback.message.answer.await_args.args[0]
    # Рекорд стоит внутри своего упражнения, отдельного списка под карточкой нет.
    lines = text.split("\n")
    ex_index = next(i for i, line in enumerate(lines) if "Жим лёжа" in line)
    record_index = next(i for i, line in enumerate(lines) if "к рекорду" in line)
    assert ex_index < record_index < ex_index + 5
    assert "Рекорды и сравнения" not in text


async def test_history_item_records_do_not_add_a_second_list(fresh_db, user_id):
    """Тренировка с рекордом в каждом упражнении: строк 🔥 столько же, сколько
    упражнений, и все они внутри блоков — карточка не отращивает второй список
    тех же рекордов под собой."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Разное")
    exercises = [
        await db.create_exercise(user_id, f"Упражнение {i}", group_id) for i in range(6)
    ]

    w1 = await db.create_workout(user_id, started_at="2026-01-01T12:00:00")
    for ex in exercises:
        block_id = await db.create_block(w1, "single")
        await db.add_block_exercise(block_id, ex, 0)
        await db.add_set(block_id, ex, 1, 0, 40.0, 8)
    await db.finish_workout(w1, finished_at="2026-01-01T12:00:00")

    w2 = await db.create_workout(user_id, started_at="2026-01-08T12:00:00")
    for ex in exercises:
        block_id = await db.create_block(w2, "single")
        await db.add_block_exercise(block_id, ex, 0)
        await db.add_set(block_id, ex, 1, 0, 50.0, 8)
    await db.finish_workout(w2, finished_at="2026-01-08T12:00:00")

    callback = _make_callback(user_id, f"hist:item:{w2}")

    assert await history.show_history_item(callback, w2)

    text = callback.message.answer.await_args.args[0]
    assert text.count("🔥 +") == len(exercises)
    assert "Рекорды и сравнения" not in text
