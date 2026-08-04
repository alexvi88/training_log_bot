"""Какие файлы импорт вообще способен прочитать.

Три вида «импорт не работает вовсе», все на реальных файлах:
  * без строки заголовков первая строка уходила в headers, и первая тренировка
    исчезала без следа (два подхода в файле → импортирован один);
  * файл из русского Excel (разделитель «;», запятая в дробях) проходил четыре
    шага маппинга и падал на «не понял дату «02.01.2025;Жим лёжа;100»» — про
    разделитель ни слова;
  * «повторы = 8.0» — обычное дело для таблиц — роняли импорт целиком.

Плюс тексты подтверждения, которые не согласовывались по числу
(«1 тренировки, 1 упражнения, 1 сетов») и предлагали загрузить ноль тренировок.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import handlers.csv_import as csv_import
from fsm import ImportFlow
from handlers.csv_import import _build_workout_groups, _read_table
from parser import ParseError

MAPPING = {"date": 0, "exercise": 1, "weight": 2, "reps": 3}


# ---------- разделители и заголовки ----------


DATA_LINES = "2025-01-02,Жим лёжа,100,8\n2025-01-02,Жим лёжа,100,7\n"


def test_a_header_row_is_told_apart_from_a_first_data_row():
    """Обе половины важны: свой экспорт с заголовками должен читаться как раньше,
    а файл без них — не терять первую строку."""
    headers, rows, has_header = _read_table("дата,упражнение,вес,повторы\n" + DATA_LINES)
    assert (has_header, headers[0], len(rows)) == (True, "дата", 2)

    headers, rows, has_header = _read_table(DATA_LINES)
    assert has_header is False
    assert len(rows) == 2, "первая строка данных больше не съедается заголовками"
    assert headers == ["Колонка 1", "Колонка 2", "Колонка 3", "Колонка 4"]
    # Позиции колонок спрашиваем у человека, но данные уже все на месте.
    assert len(_build_workout_groups(rows, MAPPING, first_line=1)[0]["entries"][0]["sets"]) == 2


@pytest.mark.parametrize("delimiter", [";", "\t", "|"])
def test_other_delimiters_are_sniffed_not_assumed(delimiter):
    text = delimiter.join(["дата", "упражнение", "вес", "повторы"]) + "\n"
    text += delimiter.join(["02.01.2025", "Жим лёжа", "100", "8"]) + "\n"
    text += delimiter.join(["02.01.2025", "Жим лёжа", "100", "7"]) + "\n"

    headers, rows, has_header = _read_table(text)

    assert has_header is True
    assert headers == ["дата", "упражнение", "вес", "повторы"]
    assert len(rows) == 2


def test_russian_excel_file_reads_semicolons_and_decimal_commas():
    """«;» как разделитель и «,» внутри дроби — то, что отдаёт русский Excel."""
    text = (
        "дата;упражнение;вес;повторы\n"
        "02.01.2025;Жим лёжа;100,5;8\n"
        "02.01.2025;Жим лёжа;97,5;8\n"
    )
    headers, rows, _ = _read_table(text)
    mapping = csv_import._auto_detect(headers)

    workouts = _build_workout_groups(rows, mapping)

    assert [s[0] for s in workouts[0]["entries"][0]["sets"]] == [100.5, 97.5]


async def test_russian_excel_file_goes_from_upload_straight_to_confirmation(fresh_db, user_id):
    """Весь путь целиком: раньше этот файл спрашивал четыре колонки (заголовки
    выглядели как одна) и в конце падал на «не понял дату»."""
    db = fresh_db
    gid = await db.create_muscle_group(user_id, "Грудь")
    await db.create_exercise(user_id, "Жим лёжа", gid)
    raw = "дата;упражнение;вес;повторы\n02.01.2025;Жим лёжа;100,5;8\n02.01.2025;Жим лёжа;97,5;8\n"

    message = MagicMock()
    message.document = SimpleNamespace(file_name="log.csv")
    message.from_user = SimpleNamespace(id=user_id, username="tester")
    message.reply = AsyncMock()
    message.answer = AsyncMock()
    message.bot = MagicMock()
    message.bot.download = AsyncMock(return_value=SimpleNamespace(read=lambda: raw.encode()))
    state = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )

    await csv_import.import_file_received(message, state)

    assert message.reply.await_count == 0, "никаких «не понял дату»"
    assert await state.get_state() == ImportFlow.confirming
    text = message.answer.await_args.args[0]
    assert "1 тренировка" in text and "2 подхода" in text


async def test_a_single_column_file_says_so_instead_of_dying_on_the_date():
    """Одна колонка после разбора — почти всегда неугаданный разделитель, и
    сказать об этом надо сразу, а не после четырёх шагов маппинга."""
    replies: list[str] = []
    message = MagicMock()
    message.document = SimpleNamespace(file_name="log.csv")
    message.reply = AsyncMock(side_effect=lambda text, **kw: replies.append(text))
    message.bot = MagicMock()
    message.bot.download = AsyncMock(
        return_value=SimpleNamespace(read=lambda: "дата\n02.01.2025\n".encode())
    )
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))

    await csv_import.import_file_received(message, state)

    assert "разделитель" in replies[0]
    assert await state.get_state() is None, "маппинг не начинался"


# ---------- числа ----------


@pytest.mark.parametrize("reps_text", ["8.0", "8,0", " 8.00 "])
def test_whole_reps_written_as_a_float_still_import(reps_text):
    workouts = _build_workout_groups([["2025-01-02", "Жим лёжа", "100", reps_text]], MAPPING)
    assert workouts[0]["entries"][0]["sets"][0][1] == 8


def test_fractional_reps_are_rejected_by_name():
    with pytest.raises(ParseError) as excinfo:
        _build_workout_groups([["2025-01-02", "Жим лёжа", "100", "8.5"]], MAPPING)
    assert "повторы: «8.5» — не целое число" in excinfo.value.message


def test_unparsable_weight_names_the_cell():
    with pytest.raises(ParseError) as excinfo:
        _build_workout_groups([["2025-01-02", "Жим лёжа", "сто", "8"]], MAPPING)
    assert "не понял вес «сто»" in excinfo.value.message


def test_short_row_complains_about_columns_not_about_numbers():
    with pytest.raises(ParseError) as excinfo:
        _build_workout_groups([["2025-01-02", "Жим лёжа", "100"]], MAPPING)
    assert "колонок меньше" in excinfo.value.message


# ---------- тексты ----------


def _message_event(user_id: int = 111):
    event = MagicMock()
    event.from_user = SimpleNamespace(id=user_id, username="tester")
    event.answer = AsyncMock()
    return event


async def _render(user_id, coro_factory, **data):
    state = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )
    await state.update_data(**data)
    event = _message_event(user_id)
    await coro_factory(event, state)
    call = event.answer.await_args
    return (call.args[0] if call.args else call.kwargs["text"]), state


async def test_confirmation_agrees_in_number_for_one_of_everything(fresh_db, user_id):
    text, _ = await _render(
        user_id,
        csv_import.show_confirmation,
        imp_workouts=[{"date": "2026-05-04", "entries": [{"name": "Присед", "sets": [(100.0, 5, None)]}]}],
        imp_resolved={},
    )
    assert "1 тренировка" in text and "1 упражнение" in text and "1 подход." in text


async def test_confirmation_agrees_in_number_for_five(fresh_db, user_id):
    entries = [{"name": f"Упр {i}", "sets": [(100.0, 5, None)] * 5} for i in range(5)]
    text, _ = await _render(
        user_id,
        csv_import.show_confirmation,
        imp_workouts=[{"date": "2026-05-04", "entries": entries}],
        imp_resolved={},
    )
    assert "1 тренировка" in text and "5 упражнений" in text and "25 подходов" in text


async def test_a_file_of_blank_rows_never_offers_to_load_zero_workouts(fresh_db, user_id):
    """Раньше это был экран «0 тренировки» с кнопкой «✅ Загрузить», после
    которой бот рапортовал «Импортировано 0 тренировок»."""
    text, state = await _render(
        user_id,
        csv_import._finish_mapping,
        imp_rows=[[], ["", "", "", ""]],
        imp_mapping=MAPPING,
        imp_has_header=True,
    )
    assert "0 тренировки" not in text
    assert "не нашёл ни одной строки" in text.lower()
    # И это не подтверждение: файл ждут заново.
    assert await state.get_state() == ImportFlow.awaiting_file
