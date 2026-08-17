"""Импорт нативного экспорта Hevy — самого частого источника миграции.

Раньше файл из Hevy (start_time, exercise_title, weight_kg, set_index —
колонки, не совпадающие ни с одним из наших синонимов) не автоопределялся ни
по одному из четырёх обязательных полей: человек проходил маппинг с нуля.
Разминочные подходы (Hevy: set_type) импортируются как обычные — у нас нет
своего понятия «разминка», и фильтровать их не стали: пусть решает сам
пользователь, стоит ли их чистить руками.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import handlers.csv_import as csv_import
from fsm import ImportFlow
from handlers.csv_import import _auto_detect, _build_workout_groups, _parse_row_date, _read_table

HEVY_HEADER = (
    "title,start_time,end_time,description,exercise_title,superset_id,"
    "exercise_notes,set_index,set_type,weight_kg,reps,distance_km,duration_seconds,rpe"
)
HEVY_SAMPLE = (
    HEVY_HEADER + "\n"
    '"Push","7 Aug 2026, 00:26","7 Aug 2026, 00:50","","Bench Press (Barbell)",,"",0,"warmup",50,10,,,\n'
    '"Push","7 Aug 2026, 00:26","7 Aug 2026, 00:50","","Bench Press (Barbell)",,"",1,"normal",100,8,,,\n'
    '"Push","7 Aug 2026, 00:26","7 Aug 2026, 00:50","","Shoulder Press (Dumbbell)",,"",0,"normal",,12,,,\n'
)


def test_hevy_date_formats_are_understood():
    assert _parse_row_date("7 Aug 2026, 08:27").isoformat() == "2026-08-07"
    assert _parse_row_date("21 Dec 2025").isoformat() == "2025-12-21"


def test_hevy_russian_locale_dates_are_understood():
    """Живой файл: Hevy на телефоне с русским языком пишет "10 авг. 2026,
    19:21" — с точкой после сокращения месяца, которой нет в английском
    варианте. Раньше это падало на "не понял дату" уже на этапе подтверждения."""
    assert _parse_row_date("10 авг. 2026, 19:21").isoformat() == "2026-08-10"
    assert _parse_row_date("21 дек. 2025").isoformat() == "2025-12-21"


def test_hevy_columns_auto_detect_without_manual_mapping():
    headers, _rows, _has_header = _read_table(HEVY_SAMPLE)
    mapping = _auto_detect(headers)
    assert {"date", "exercise", "weight", "reps"} <= mapping.keys()
    assert mapping["round"] == headers.index("set_index")


def test_all_sets_import_including_warmups():
    headers, rows, _ = _read_table(HEVY_SAMPLE)
    mapping = _auto_detect(headers)

    workouts = _build_workout_groups(rows, mapping)

    (workout,) = workouts
    bench_sets = next(e for e in workout["entries"] if e["name"] == "Bench Press (Barbell)")["sets"]
    assert bench_sets == [[50.0, 10, None], [100.0, 8, None]]


def test_bodyweight_style_blank_weight_still_imports():
    headers, rows, _ = _read_table(HEVY_SAMPLE)
    mapping = _auto_detect(headers)

    workouts = _build_workout_groups(rows, mapping)

    shoulder = next(e for e in workouts[0]["entries"] if e["name"] == "Shoulder Press (Dumbbell)")
    assert shoulder["sets"] == [[0.0, 12, None]]


async def test_hevy_file_goes_from_upload_straight_to_confirmation(fresh_db, user_id):
    db = fresh_db
    gid = await db.create_muscle_group(user_id, "Грудь")
    await db.create_exercise(user_id, "Bench Press (Barbell)", gid)
    await db.create_exercise(user_id, "Shoulder Press (Dumbbell)", gid)

    message = MagicMock()
    message.document = SimpleNamespace(file_name="workout_data.csv")
    message.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    message.reply = AsyncMock()
    message.answer = AsyncMock()
    message.bot = MagicMock()
    message.bot.download = AsyncMock(return_value=SimpleNamespace(read=lambda: HEVY_SAMPLE.encode()))
    state = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )

    await csv_import.import_file_received(message, state)

    assert message.reply.await_count == 0, "никаких «не понял дату» и вопросов маппинга"
    assert await state.get_state() == ImportFlow.confirming
    text = message.answer.await_args.args[0]
    assert "1 тренировка" in text and "Bench Press (Barbell)" in text
