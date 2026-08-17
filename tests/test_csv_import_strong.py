"""Импорт нативного экспорта Strong — второго по частоте источника миграции.

У Strong дата и вес названы как у нас ("Date", "Weight"), а упражнение и номер
подхода — нет ("Exercise Name", "Set Order"): файл автоопределялся наполовину, и
человек всё равно шёл в ручной маппинг ради двух полей из четырёх. Ещё две вещи
из живого файла: кардио лежит в той же таблице с нулями в весе и повторах (это
роняло импорт целиком), а колонка веса подписана единицей аккаунта — «Weight
(lbs)» без пересчёта легло бы в историю как килограммы.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import handlers.csv_import as csv_import
from fsm import ImportFlow
from handlers.csv_import import (
    _auto_detect,
    _build_workout_groups,
    _parse_row_date,
    _read_table,
    _weight_factor,
)

STRONG_HEADER = (
    "Date,Workout Name,Duration,Exercise Name,Set Order,Weight,Reps,Distance,Seconds,RPE"
)
STRONG_SAMPLE = (
    STRONG_HEADER + "\n"
    '2026-08-15 14:23:54,"Afternoon Workout",38s,"Bench Press (Barbell)",1,50.0,10.0,0,0.0,\n'
    '2026-08-15 14:23:54,"Afternoon Workout",38s,"Bench Press (Barbell)",2,60.0,8.0,0,0.0,8\n'
    '2026-08-15 14:26:06,"Afternoon Workout",31s,"Back Extension",1,0,12.0,0,0.0,\n'
)


def test_strong_datetime_is_understood():
    """Strong пишет дату и время через пробел, без «T» посередине."""
    assert _parse_row_date("2026-08-15 14:23:54").isoformat() == "2026-08-15"


def test_strong_columns_auto_detect_without_manual_mapping():
    headers, _rows, _has_header = _read_table(STRONG_SAMPLE)

    mapping = _auto_detect(headers)

    assert {"date", "exercise", "weight", "reps"} <= mapping.keys()
    assert mapping["exercise"] == headers.index("Exercise Name")
    assert mapping["round"] == headers.index("Set Order")
    assert mapping["rpe"] == headers.index("RPE")


def test_workout_name_column_is_not_taken_for_the_exercise():
    """«Workout Name» стоит в файле раньше «Exercise Name» — если бы синонимом
    было просто «name», в историю поехало бы название тренировки вместо
    упражнения, и весь импорт слился бы в одно «Afternoon Workout»."""
    headers, _rows, _has_header = _read_table(STRONG_SAMPLE)

    mapping = _auto_detect(headers)

    assert headers[mapping["exercise"]] == "Exercise Name"


def test_strong_sets_import_with_rpe_and_blank_weight():
    headers, rows, _ = _read_table(STRONG_SAMPLE)
    mapping = _auto_detect(headers)

    workouts = _build_workout_groups(rows, mapping)

    (workout,) = workouts
    bench = next(e for e in workout["entries"] if e["name"] == "Bench Press (Barbell)")
    assert bench["sets"] == [[50.0, 10, None], [60.0, 8, 8.0]]
    back = next(e for e in workout["entries"] if e["name"] == "Back Extension")
    assert back["sets"] == [[0.0, 12, None]]


def test_cardio_rows_are_skipped_instead_of_failing_the_file():
    """Пробежка в Strong лежит в той же таблице: вес и повторы нули, работа — в
    Distance/Seconds, которых у нас нет. Раньше на этой строке падал весь файл."""
    text = (
        STRONG_HEADER + "\n"
        '2026-08-15 14:23:54,"Afternoon Workout",38s,"Bench Press (Barbell)",1,50.0,10.0,0,0.0,\n'
        '2026-08-15 15:00:00,"Run",30m,"Running",1,0,0,5.0,1800.0,\n'
    )
    headers, rows, _ = _read_table(text)
    mapping = _auto_detect(headers)

    workouts = _build_workout_groups(rows, mapping)

    names = [e["name"] for w in workouts for e in w["entries"]]
    assert names == ["Bench Press (Barbell)"]


def test_pounds_column_converts_to_kilograms():
    """«Weight (lbs)» — вес аккаунта в фунтах. Без пересчёта 225 lbs легли бы в
    историю как 225 кг: вечный рекорд упражнения и весовые клубы задаром."""
    text = (
        STRONG_HEADER.replace("Weight", "Weight (lbs)") + "\n"
        '2026-08-15 14:23:54,"Afternoon Workout",38s,"Bench Press (Barbell)",1,225,5,0,0.0,\n'
    )
    headers, rows, _ = _read_table(text)
    mapping = _auto_detect(headers)

    workouts = _build_workout_groups(
        rows, mapping, weight_factor=_weight_factor(headers, mapping)
    )

    assert workouts[0]["entries"][0]["sets"] == [[102.1, 5, None]]


def test_kilogram_column_is_left_alone():
    text = (
        STRONG_HEADER.replace("Weight", "Weight (kg)") + "\n"
        '2026-08-15 14:23:54,"Afternoon Workout",38s,"Bench Press (Barbell)",1,100,5,0,0.0,\n'
    )
    headers, rows, _ = _read_table(text)
    mapping = _auto_detect(headers)

    assert _weight_factor(headers, mapping) == 1.0
    workouts = _build_workout_groups(rows, mapping)
    assert workouts[0]["entries"][0]["sets"] == [[100.0, 5, None]]


async def test_strong_file_goes_from_upload_straight_to_confirmation(fresh_db, user_id):
    db = fresh_db
    gid = await db.create_muscle_group(user_id, "Грудь")
    await db.create_exercise(user_id, "Bench Press (Barbell)", gid)
    await db.create_exercise(user_id, "Back Extension", gid)

    message = MagicMock()
    message.document = SimpleNamespace(file_name="strong.csv")
    message.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    message.reply = AsyncMock()
    message.answer = AsyncMock()
    message.bot = MagicMock()
    message.bot.download = AsyncMock(
        return_value=SimpleNamespace(read=lambda: STRONG_SAMPLE.encode())
    )
    state = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )

    await csv_import.import_file_received(message, state)

    assert message.reply.await_count == 0, "никаких вопросов маппинга на родном экспорте"
    assert await state.get_state() == ImportFlow.confirming
    text = message.answer.await_args.args[0]
    assert "1 тренировка" in text and "Bench Press (Barbell)" in text
