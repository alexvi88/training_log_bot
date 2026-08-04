"""Недельная сводка таблицей (Bot API 10.1) и локальное время силами клиента
(date_time entity, Bot API 9.5).

Обе фичи — из свежего API, поэтому обе обязаны деградировать: сводка уходит
текстом, если rich-сообщение не прошло, а entity сопровождается фолбэк-текстом,
который старый клиент покажет как есть.
"""
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest

import formatting
from handlers import history

pytestmark = pytest.mark.asyncio


def _rows():
    return [
        formatting.WeeklyRow(name="жим лёжа", top_weight=105.0, tonnage=4210.0, sets_count=12),
        formatting.WeeklyRow(name="присед", top_weight=140.0, tonnage=6800.0, sets_count=15),
    ]


# ---------- содержимое сводки ----------


async def test_text_summary_carries_every_number_the_table_does():
    """Текст — не урезанная версия: это тот же набор чисел, просто без сетки."""
    text = formatting.build_weekly_summary(_rows(), 4, 21400.0, "28.07–03.08")

    assert "28.07–03.08" in text
    assert "4 тренировки" in text
    for row in _rows():
        assert row.name in text
        assert str(row.sets_count) in text


async def test_empty_week_says_so_instead_of_printing_an_empty_table():
    text = formatting.build_weekly_summary([], 0, 0.0, "28.07–03.08")
    assert "тренировок не было" in text
    assert formatting.build_weekly_table([]) is None


async def test_table_has_a_header_row_and_one_row_per_exercise():
    table = formatting.build_weekly_table(_rows())

    assert len(table.cells) == 3  # шапка + два упражнения
    assert [c.is_header for c in table.cells[0]] == [True] * 4
    assert table.cells[1][0].text == "жим лёжа"
    assert table.cells[1][2].text == "12"


async def test_long_week_is_cut_only_in_the_display():
    """Строк показываем ограниченное число — а тоннаж недели остаётся полным.

    Список резался до вызова сводки, и итог считался по остатку: у человека с 20
    упражнениями недельный тоннаж выходил заниженным и не сходился с плиткой
    «ТОННАЖ ЗА 7 ДНЕЙ», которая считает по всем подходам.
    """
    rows = [
        formatting.WeeklyRow(name=f"упражнение {i}", top_weight=100.0, tonnage=1000.0, sets_count=3)
        for i in range(20)
    ]
    total = sum(r.tonnage for r in rows)

    text = formatting.build_weekly_summary(rows, 4, total, "28.07–03.08")
    table = formatting.build_weekly_table(rows)

    assert text.count("упражнение ") == formatting.WEEKLY_ROWS_LIMIT
    assert len(table.cells) == formatting.WEEKLY_ROWS_LIMIT + 1  # шапка + строки
    assert formatting.format_tonnage(total) in text  # 20 тонн, а не 12


async def test_numeric_columns_are_right_aligned():
    """Числа в колонках сравнивают глазами — вразнобой они не сравниваются."""
    table = formatting.build_weekly_table(_rows())
    assert [c.align for c in table.cells[1]] == ["left", "right", "right", "right"]


# ---------- деградация ----------


def _callback(user_id: int):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.answer_rich = AsyncMock()
    message.delete = AsyncMock()
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="t")
    callback.message = message
    callback.answer = AsyncMock()
    return callback


async def test_rich_send_failure_falls_back_to_text(fresh_db, user_id, monkeypatch):
    """Сервер ниже 10.1 не знает sendRichMessage — сводка должна прийти
    текстом, а не исчезнуть."""
    callback = _callback(user_id)
    callback.message.answer_rich = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="method not found")
    )
    sent = {}

    async def fake_safe_edit(cb, text, **kwargs):
        sent["text"] = text

    monkeypatch.setattr(history.ui, "safe_edit", fake_safe_edit)

    await history.prog_week(callback, MagicMock())

    assert "НЕДЕЛЯ" in sent["text"]
    callback.message.delete.assert_not_awaited()  # нечего удалять — rich не ушёл
    callback.answer.assert_awaited()


async def test_successful_rich_send_replaces_the_old_screen(fresh_db, user_id, monkeypatch):
    db = fresh_db
    gid = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", gid)
    today = dt.date.today()
    monday = today - dt.timedelta(days=today.weekday())
    workout_id = await db.create_workout(user_id, started_at=f"{monday.isoformat()}T10:00:00")
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.append_set(block_id, ex_id, 0, 100.0, 5)
    await db.finish_workout(workout_id, finished_at=f"{monday.isoformat()}T11:00:00")

    callback = _callback(user_id)
    monkeypatch.setattr(history.ui, "safe_edit", AsyncMock())

    await history.prog_week(callback, MagicMock())

    callback.message.answer_rich.assert_awaited_once()
    rich = callback.message.answer_rich.await_args.kwargs["rich_message"]
    table = rich.blocks[-1]
    assert table.cells[1][0].text == "Жим лёжа"
    callback.message.delete.assert_awaited_once()


# ---------- итог недели ----------


async def test_week_total_counts_exercises_the_table_does_not_show(fresh_db, user_id, monkeypatch):
    """Тот же итог на живых данных: 13 упражнений за неделю, показано 12,
    в заголовке — тоннаж всех тринадцати."""
    db = fresh_db
    gid = await db.create_muscle_group(user_id, "Грудь")
    today = dt.date.today()
    monday = today - dt.timedelta(days=today.weekday())
    workout_id = await db.create_workout(user_id, started_at=f"{monday.isoformat()}T10:00:00")
    for i in range(formatting.WEEKLY_ROWS_LIMIT + 1):
        ex_id = await db.create_exercise(user_id, f"Упражнение {i}", gid)
        block_id = await db.create_block(workout_id, "single")
        await db.add_block_exercise(block_id, ex_id, 0)
        await db.append_set(block_id, ex_id, 0, 100.0, 10)
    await db.finish_workout(workout_id, finished_at=f"{monday.isoformat()}T11:00:00")

    callback = _callback(user_id)
    callback.message.answer_rich = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="method not found")
    )
    sent = {}

    async def fake_safe_edit(cb, text, **kwargs):
        sent["text"] = text

    monkeypatch.setattr(history.ui, "safe_edit", fake_safe_edit)

    await history.prog_week(callback, MagicMock())

    # 13 упражнений × 1000 кг — столько же, сколько насчитает плитка тоннажа.
    expected = formatting.format_tonnage(1000.0 * (formatting.WEEKLY_ROWS_LIMIT + 1))
    assert expected in sent["text"]


# ---------- локальное время ----------


async def test_entity_carries_the_moment_and_a_readable_fallback():
    moment = dt.datetime(2026, 8, 2, 15, 20)
    text, entity = formatting.local_time_entity(moment, "02.08.2026 (вс), 15:20")

    assert text == "02.08.2026 (вс), 15:20"  # это увидят старые клиенты
    assert entity.type == "date_time"
    assert entity.unix_time == int(moment.replace(tzinfo=dt.timezone.utc).timestamp())


async def test_entity_offset_is_counted_in_utf16_units():
    """Смещения Telegram считает в UTF-16: кириллица и эмодзи до маркера иначе
    сдвинут метку на чужие символы."""
    stamp = "02.08.2026, 15:20"
    full = f"⚠️ У тебя висит тренировка с {stamp} — забыл закрыть?"
    _text, entity = formatting.local_time_entity(dt.datetime(2026, 8, 2, 15, 20), stamp)

    entities = formatting.entities_at(full, stamp, entity)

    offset = entities[0].offset
    encoded = full.encode("utf-16-le")
    assert encoded[offset * 2 : (offset + entities[0].length) * 2].decode("utf-16-le") == stamp


async def test_entities_are_dropped_when_the_marker_is_missing():
    _text, entity = formatting.local_time_entity(dt.datetime(2026, 8, 2, 15, 20), "15:20")
    assert formatting.entities_at("текст без метки", "15:20", entity) is None
