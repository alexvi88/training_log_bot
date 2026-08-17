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
    message.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
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
    assert "1 тренировка" in text and "Жим лёжа" in text


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


def test_thousands_comma_is_not_read_as_a_decimal_point():
    """Английский/американский экспорт группирует тысячи запятой: «1,200»
    значит 1200, а не 1.2. Раньше запятая всегда читалась как десятичная
    точка — тихая порча веса без единой ошибки импорта."""
    from handlers.csv_import import _parse_number

    assert _parse_number("1,200") == 1200.0
    assert _parse_number("1,234,000.5") == 1234000.5
    # Десятичная запятая — обычный случай, не должен сломаться.
    assert _parse_number("100,5") == 100.5


def test_unparsable_weight_names_the_cell():
    with pytest.raises(ParseError) as excinfo:
        _build_workout_groups([["2025-01-02", "Жим лёжа", "сто", "8"]], MAPPING)
    assert "не понял вес «сто»" in excinfo.value.message


def test_short_row_complains_about_columns_not_about_numbers():
    with pytest.raises(ParseError) as excinfo:
        _build_workout_groups([["2025-01-02", "Жим лёжа", "100"]], MAPPING)
    assert "колонок меньше" in excinfo.value.message


def test_a_row_missing_the_optional_round_column_still_imports():
    """У rpe уже была та же терпимость к рваным строкам — у round её не было:
    ряд без опциональной хвостовой колонки (например, Hevy set_index) валил
    файл целиком на «колонок меньше», хотя round там просто не указан."""
    mapping = {**MAPPING, "round": 4}
    workouts = _build_workout_groups(
        [["2025-01-02", "Жим лёжа", "100", "8"]], mapping
    )
    assert workouts[0]["entries"][0]["sets"][0] == [100.0, 8, None]


def test_build_workout_groups_checks_the_future_against_the_passed_in_today():
    """dd.mm.yyyy-дата сверяется с «сегодня» по умолчанию через серверные часы
    (parser.parse_ru_date). Пользователь с положительным tz_offset может
    ре-импортировать свою же вчерашнюю (по UTC) тренировку и получить «дата в
    будущем» — _finish_mapping обязан прокидывать локальное «сегодня»
    пользователя, а не полагаться на дефолт."""
    import datetime as dt

    user_tomorrow = dt.date.today() + dt.timedelta(days=1)
    row = [[user_tomorrow.strftime("%d.%m.%Y"), "Жим лёжа", "100", "8"]]

    with pytest.raises(ParseError, match="будущем"):
        _build_workout_groups(row, MAPPING)

    workouts = _build_workout_groups(row, MAPPING, today=user_tomorrow)
    assert workouts[0]["date"] == user_tomorrow.isoformat()


# ---------- тексты ----------


def _message_event(user_id: int = 111):
    event = MagicMock()
    event.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
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
    assert "1 тренировка" in text and "Присед" in text


async def test_confirmation_lists_up_to_three_exercises_and_collapses_the_rest(fresh_db, user_id):
    """Пять упражнений в одной тренировке — экран подтверждения показывает их
    так же, как история: до трёх буллитами, остальные одной строкой "+N"."""
    entries = [{"name": f"Упр {i}", "sets": [(100.0, 5, None)] * 5} for i in range(5)]
    text, _ = await _render(
        user_id,
        csv_import.show_confirmation,
        imp_workouts=[{"date": "2026-05-04", "entries": entries}],
        imp_resolved={},
    )
    assert "1 тренировка" in text
    assert "Упр 0" in text and "Упр 1" in text and "Упр 2" in text
    assert "Упр 3" not in text and "Упр 4" not in text
    assert "+2 других" in text


async def test_confirmation_shows_several_workouts_per_page(fresh_db, user_id):
    """Раньше подтверждение листало по одной тренировке за раз — теперь, как в
    истории, несколько тренировок сразу на одной странице сообщения."""
    workouts = [
        {"date": f"2026-05-{day:02d}", "entries": [{"name": "Присед", "sets": [(100.0, 5, None)]}]}
        for day in range(1, 4)
    ]
    text, _ = await _render(
        user_id, csv_import.show_confirmation, imp_workouts=workouts, imp_resolved={}
    )
    assert "3 тренировки" in text
    assert "01.05.2026" in text and "02.05.2026" in text and "03.05.2026" in text
    assert "стр." not in text, "все три уместились на одной странице — номер страницы лишний"


async def test_confirmation_paginates_past_the_page_size(fresh_db, user_id):
    workouts = [
        {"date": f"2026-05-{day:02d}", "entries": [{"name": "Присед", "sets": [(100.0, 5, None)]}]}
        for day in range(1, 10)
    ]
    text, state = await _render(
        user_id, csv_import.show_confirmation, imp_workouts=workouts, imp_resolved={}
    )
    assert "9 тренировок" in text
    assert "стр. 1/2" in text
    assert "09.05.2026" not in text, "девятая тренировка должна быть на второй странице"

    event = _message_event(user_id)
    await csv_import._render_confirmation_page(event, state, 1)
    page2_text = event.answer.await_args.args[0]
    assert "09.05.2026" in page2_text
    assert "стр. 2/2" in page2_text


async def test_a_file_of_blank_rows_never_offers_to_load_zero_workouts(fresh_db, user_id):
    """Раньше это был экран «0 тренировки» с кнопкой «✅ Загрузить», после
    которой бот рапортовал «Загрузил 0 тренировок»."""
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
