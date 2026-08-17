"""Bodyweight log: db round-trip, ordering, undo, rescale, and the screen text."""

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import db as dbmod
import formatting
from parser import ParseError, bodyweight_warning, parse_bodyweight

# ---------- parser ----------


@pytest.mark.parametrize("text,expected", [("80", 80.0), ("80.5", 80.5), ("80,5", 80.5), (" 72 ", 72.0)])
def test_parse_bodyweight_ok(text, expected):
    assert parse_bodyweight(text) == expected


@pytest.mark.parametrize("bad", ["", "abc", "0", "-5", "80 kg"])
def test_parse_bodyweight_rejects(bad):
    with pytest.raises(ParseError):
        parse_bodyweight(bad)


def test_parse_bodyweight_no_longer_rejects_a_plausible_typo_outright():
    """Regression for находка 16: 300 used to slip through the old 0-1000
    bound silently, and the fix is a soft warning (see below), not a hard
    reject — parse_bodyweight itself now only rejects the truly impossible."""
    assert parse_bodyweight("300") == 300.0


def test_parse_bodyweight_hard_ceiling():
    """Well past anything human, in kg or lb — still a hard reject."""
    with pytest.raises(ParseError):
        parse_bodyweight("9999")


# ---------- soft "does this look like a typo?" warning ----------


def test_bodyweight_warning_flags_a_suspicious_kg_value():
    """300 кг — точно тот случай из находки 16: "Сейчас: 300кг" молча ломало
    тренд. Теперь это не жёсткий отказ, а мягкая метка "подозрительно"."""
    assert bodyweight_warning(300.0, "kg") is not None
    assert bodyweight_warning(80.0, "kg") is None


def test_bodyweight_warning_accounts_for_pounds():
    """A perfectly normal pound entry shouldn't get flagged by the kg range —
    660lb is roughly 300kg, the same plausible ceiling, just in the other unit."""
    assert bodyweight_warning(180.0, "lb") is None  # ~82kg, ordinary
    assert bodyweight_warning(300.0, "lb") is None  # ~136kg, still plausible in lb
    assert bodyweight_warning(700.0, "lb") is not None


# ---------- db ----------


@pytest.mark.asyncio
async def test_bodyweight_log_roundtrip_and_order(user_id):
    await dbmod.add_bodyweight_log(user_id, 80.0, logged_at="2026-01-01T10:00:00")
    await dbmod.add_bodyweight_log(user_id, 79.0, logged_at="2026-02-01T10:00:00")
    await dbmod.add_bodyweight_log(user_id, 78.0, logged_at="2026-03-01T10:00:00")

    logs = await dbmod.list_bodyweight_logs(user_id)
    assert [r["weight"] for r in logs] == [80.0, 79.0, 78.0]  # ascending by date

    latest = await dbmod.get_latest_bodyweight(user_id)
    assert latest["weight"] == 78.0


@pytest.mark.asyncio
async def test_bodyweight_limit_returns_recent_ascending(user_id):
    for i, w in enumerate([80, 79, 78, 77]):
        await dbmod.add_bodyweight_log(user_id, w, logged_at=f"2026-0{i + 1}-01T10:00:00")
    recent = await dbmod.list_bodyweight_logs(user_id, limit=2)
    assert [r["weight"] for r in recent] == [78.0, 77.0]


@pytest.mark.asyncio
async def test_delete_bodyweight_log_removes_the_one_asked_for(user_id):
    """Удалить можно любую запись, не только последнюю — ту, что была неделю
    назад, а не только последнюю в дневнике."""
    id1 = await dbmod.add_bodyweight_log(user_id, 80.0, logged_at="2026-01-01T10:00:00")
    await dbmod.add_bodyweight_log(user_id, 79.0, logged_at="2026-02-01T10:00:00")
    removed = await dbmod.delete_bodyweight_log(id1, user_id)
    assert removed is True
    assert [r["weight"] for r in await dbmod.list_bodyweight_logs(user_id)] == [79.0]


@pytest.mark.asyncio
async def test_delete_bodyweight_log_refuses_someone_elses_row(user_id):
    log_id = await dbmod.add_bodyweight_log(user_id, 80.0, logged_at="2026-01-01T10:00:00")
    removed = await dbmod.delete_bodyweight_log(log_id, telegram_id=user_id + 1)
    assert removed is False
    assert [r["weight"] for r in await dbmod.list_bodyweight_logs(user_id)] == [80.0]


@pytest.mark.asyncio
async def test_count_and_list_bodyweight_logs_page_newest_first(user_id):
    for i, w in enumerate([80, 79, 78, 77]):
        await dbmod.add_bodyweight_log(user_id, w, logged_at=f"2026-0{i + 1}-01T10:00:00")
    assert await dbmod.count_bodyweight_logs(user_id) == 4

    page0 = await dbmod.list_bodyweight_logs_page(user_id, limit=2, offset=0)
    assert [r["weight"] for r in page0] == [77.0, 78.0]

    page1 = await dbmod.list_bodyweight_logs_page(user_id, limit=2, offset=2)
    assert [r["weight"] for r in page1] == [79.0, 80.0]


@pytest.mark.asyncio
async def test_scale_bodyweight_logs(user_id):
    await dbmod.add_bodyweight_log(user_id, 100.0, logged_at="2026-01-01T10:00:00")
    await dbmod.scale_bodyweight_logs(user_id, 2.20462)
    latest = await dbmod.get_latest_bodyweight(user_id)
    assert latest["weight"] == pytest.approx(220.5)  # rounded to 1 decimal


# ---------- screen text ----------


def test_bodyweight_screen_empty():
    text = formatting.build_bodyweight_screen([], "kg")
    assert "Пока нет ни одной записи" in text


def test_bodyweight_screen_shows_latest_and_count():
    logs = [
        {"weight": 82.0, "logged_at": "2026-01-01T10:00:00"},
        {"weight": 80.0, "logged_at": "2026-02-01T10:00:00"},
    ]
    text = formatting.build_bodyweight_screen(logs, "kg")
    assert "Сейчас: <b>80кг</b>" in text
    assert "Всего 2 взвешивания." in text
    assert "С прошлой записи" not in text
    assert "За всё время" not in text


def test_bodyweight_screen_lists_entries_newest_first():
    logs = [
        {"weight": 82.0, "logged_at": "2026-01-01T10:00:00"},
        {"weight": 80.0, "logged_at": "2026-02-01T10:00:00"},
    ]
    text = formatting.build_bodyweight_screen(logs, "kg")
    assert "01.02.2026 — 80кг" in text
    assert "01.01.2026 — 82кг" in text
    assert text.index("01.02.2026") < text.index("01.01.2026")
    assert "Пиши" in text or "Напиши вес" in text


def test_bodyweight_screen_lists_only_period_logs_when_given():
    logs = [
        {"weight": 82.0, "logged_at": "2026-01-01T10:00:00"},
        {"weight": 80.0, "logged_at": "2026-02-01T10:00:00"},
    ]
    text = formatting.build_bodyweight_screen(logs, "kg", period_logs=logs[1:])
    assert "01.02.2026 — 80кг" in text
    assert "01.01.2026" not in text
    # latest/count still reflect the full history, not just the period
    assert "Всего 2 взвешивания." in text


# ---------- «✏️ Записи»: список с удалением любой записи ----------


def test_bodyweight_list_screen_empty():
    text = formatting.build_bodyweight_list_screen([], "kg", page=0, page_size=10, total=0)
    assert "Записей нет" in text


def test_bodyweight_list_screen_numbers_rows_and_shows_time():
    rows = [
        {"id": 2, "weight": 80.0, "logged_at": "2026-02-01T08:30:00"},
        {"id": 1, "weight": 82.0, "logged_at": "2026-01-01T07:00:00"},
    ]
    text = formatting.build_bodyweight_list_screen(rows, "kg", page=0, page_size=10, total=2)
    assert "1. 01.02.2026 08:30 — 80кг" in text
    assert "2. 01.01.2026 07:00 — 82кг" in text
    assert "Показано" not in text  # everything fits on one page


def test_bodyweight_list_screen_shows_page_range_when_paginated():
    rows = [{"id": 1, "weight": 80.0, "logged_at": "2026-02-01T08:00:00"}]
    text = formatting.build_bodyweight_list_screen(rows, "kg", page=1, page_size=1, total=3)
    assert "Показано 2–2 из 3" in text


# ---------- unit conversion: set weights ----------


@pytest.mark.asyncio
async def test_scale_user_set_weights_converts_nonzero_only(user_id):
    groups = await dbmod.list_muscle_groups(None, global_only=True)
    gid = groups[0]["id"]
    ex_id = await dbmod.create_exercise(user_id, "Жим", gid)
    wid = await dbmod.create_finished_workout(user_id, "2026-01-01T10:00:00", "2026-01-01T10:30:00")
    block_id = await dbmod.create_block(wid, "single")
    await dbmod.add_block_exercise(block_id, ex_id, 0)
    await dbmod.add_set(block_id, ex_id, 1, 0, 100.0, 8)
    await dbmod.add_set(block_id, ex_id, 2, 0, 0.0, 12)  # bodyweight set

    await dbmod.scale_user_set_weights(user_id, dbmod.config.LB_PER_KG)

    weights = sorted(s["weight"] for s in await dbmod.list_sets_for_block(block_id))
    assert weights == [0.0, pytest.approx(220.5)]  # zero untouched, 100 -> 220.5


# ---------- chart period window ----------


def test_window_all_returns_everything():
    from handlers.bodyweight import _window
    logs = [{"logged_at": "2026-01-01T10:00:00", "weight": 80.0}]
    assert _window(logs, 0, dt.date(2026, 1, 1)) == logs


def test_daily_average_points_collapses_same_day_entries():
    from handlers.bodyweight import _daily_average_points

    logs = [
        {"logged_at": "2026-01-01T08:00:00", "weight": 80.0},
        {"logged_at": "2026-01-01T20:00:00", "weight": 82.0},  # same day as above
        {"logged_at": "2026-01-02T08:00:00", "weight": 79.0},
    ]
    points = _daily_average_points(logs)
    assert len(points) == 2
    assert points[0][1] == pytest.approx(81.0)  # average of 80 and 82
    assert points[1][1] == pytest.approx(79.0)


def test_window_filters_by_weeks():
    import handlers.bodyweight as bw

    logs = [
        {"logged_at": "2026-01-01T10:00:00", "weight": 82.0},  # ~8.5 weeks ago
        {"logged_at": "2026-02-20T10:00:00", "weight": 80.0},  # within 8 weeks
    ]
    windowed = bw._window(logs, 8, dt.date(2026, 3, 1))
    assert [r["weight"] for r in windowed] == [80.0]


def test_window_uses_user_local_today_not_server_date(monkeypatch):
    """Находка 6: _window used to call dt.date.today() (server/UTC date)
    directly instead of the user's own "сегодня" — same class of bug as the
    backfill calendar, fixed the same way (timeutil.user_today)."""
    import handlers.bodyweight as bw

    class _ExplodingDate(dt.date):
        @classmethod
        def today(cls):
            raise AssertionError("_window must not call dt.date.today() itself")

    monkeypatch.setattr(bw.dt, "date", _ExplodingDate)
    logs = [{"logged_at": "2026-01-01T10:00:00", "weight": 80.0}]
    # Passing `today` explicitly must never touch dt.date.today() — the
    # ExplodingDate patch above would fail the test if it did.
    assert bw._window(logs, 8, dt.date(2026, 1, 1)) == logs


# ---------- typing a weight directly on the viewing screen ----------


async def _make_state(user_id: int) -> FSMContext:
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=MemoryStorage(), key=key)


def _make_message(user_id: int, text: str):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 42
    message.text = text
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=43))
    message.answer_photo = AsyncMock(return_value=SimpleNamespace(message_id=43))
    message.reply = AsyncMock()
    message.delete = AsyncMock()
    message.bot = MagicMock()
    message.bot.delete_message = AsyncMock()
    return message


@pytest.mark.asyncio
async def test_typing_weight_while_viewing_adds_log(fresh_db, user_id):
    import handlers.bodyweight as bw
    from fsm import BodyweightFlow

    state = await _make_state(user_id)
    await state.set_state(BodyweightFlow.viewing)

    message = _make_message(user_id, "83.7")
    await bw.bw_weight_entered(message, state)

    logs = await dbmod.list_bodyweight_logs(user_id)
    assert [r["weight"] for r in logs] == [83.7]
    message.answer.assert_awaited()


@pytest.mark.asyncio
async def test_typing_invalid_text_while_viewing_replies_with_error(fresh_db, user_id):
    import handlers.bodyweight as bw
    from fsm import BodyweightFlow

    state = await _make_state(user_id)
    await state.set_state(BodyweightFlow.viewing)

    message = _make_message(user_id, "not a number")
    await bw.bw_weight_entered(message, state)

    assert await dbmod.list_bodyweight_logs(user_id) == []
    message.reply.assert_awaited_once()


def _make_bw_confirm_callback(user_id: int, data: str):
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    callback.data = data
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.delete = AsyncMock()
    callback.message.chat = SimpleNamespace(id=user_id)
    callback.message.bot = MagicMock()
    callback.message.bot.delete_message = AsyncMock()
    callback.message.answer = AsyncMock(return_value=SimpleNamespace(message_id=44))
    callback.message.answer_photo = AsyncMock(return_value=SimpleNamespace(message_id=44))
    return callback


@pytest.mark.asyncio
async def test_a_suspicious_bodyweight_asks_before_logging(fresh_db, user_id):
    """Regression for находка 16: 300 used to be written straight to the log
    with no signal at all ("Сейчас: 300кг"). Now it's held back for a
    confirm, same as a suspicious set weight."""
    import handlers.bodyweight as bw
    from fsm import BodyweightFlow

    state = await _make_state(user_id)
    await state.set_state(BodyweightFlow.viewing)

    message = _make_message(user_id, "300")
    await bw.bw_weight_entered(message, state)

    assert await dbmod.list_bodyweight_logs(user_id) == []  # not written yet
    message.reply.assert_awaited_once()
    kwargs = message.reply.await_args.kwargs
    assert "Подозрительно" in message.reply.await_args.args[0]
    assert kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_confirming_a_suspicious_bodyweight_logs_it(fresh_db, user_id):
    import handlers.bodyweight as bw
    from fsm import BodyweightFlow

    state = await _make_state(user_id)
    await state.set_state(BodyweightFlow.viewing)
    await state.update_data(bw_pending_weight=300.0)

    callback = _make_bw_confirm_callback(user_id, "bw:wconf:yes")
    await bw.bw_weight_confirm_yes(callback, state)

    logs = await dbmod.list_bodyweight_logs(user_id)
    assert [r["weight"] for r in logs] == [300.0]


@pytest.mark.asyncio
async def test_bw_list_shows_entries_newest_first(fresh_db, user_id):
    import handlers.bodyweight as bw

    await dbmod.add_bodyweight_log(user_id, 80.0, logged_at="2026-01-01T10:00:00")
    await dbmod.add_bodyweight_log(user_id, 79.0, logged_at="2026-02-01T10:00:00")
    state = await _make_state(user_id)

    callback = _make_bw_confirm_callback(user_id, "bw:list:0")
    await bw.bw_list(callback, state)

    from fsm import BodyweightFlow
    assert await state.get_state() == BodyweightFlow.browsing.state
    text = callback.message.answer.await_args.args[0]
    assert "1. 01.02.2026" in text
    assert "2. 01.01.2026" in text


@pytest.mark.asyncio
async def test_bw_delete_record_removes_the_chosen_entry_and_reports_it(fresh_db, user_id):
    import handlers.bodyweight as bw

    id1 = await dbmod.add_bodyweight_log(user_id, 80.0, logged_at="2026-01-01T10:00:00")
    await dbmod.add_bodyweight_log(user_id, 79.0, logged_at="2026-02-01T10:00:00")
    state = await _make_state(user_id)

    callback = _make_bw_confirm_callback(user_id, f"bw:delrec:{id1}:0")
    await bw.bw_delete_record(callback, state)

    callback.answer.assert_awaited_once_with("Удалил запись")
    logs = await dbmod.list_bodyweight_logs(user_id)
    assert [r["weight"] for r in logs] == [79.0]


@pytest.mark.asyncio
async def test_bw_delete_record_someone_elses_entry_reports_not_found(fresh_db, user_id):
    import handlers.bodyweight as bw

    log_id = await dbmod.add_bodyweight_log(user_id, 80.0, logged_at="2026-01-01T10:00:00")
    await dbmod.get_or_create_user(user_id + 1, "other")
    state = await _make_state(user_id + 1)

    callback = _make_bw_confirm_callback(user_id + 1, f"bw:delrec:{log_id}:0")
    await bw.bw_delete_record(callback, state)

    callback.answer.assert_awaited_once_with("Запись не найдена")
    assert len(await dbmod.list_bodyweight_logs(user_id)) == 1


@pytest.mark.asyncio
async def test_bw_delete_record_steps_back_a_page_when_it_empties_the_last_one(fresh_db, user_id, monkeypatch):
    """Удалить единственную запись на второй странице — и не остаться на
    пустом экране без единой кнопки назад."""
    import handlers.bodyweight as bw
    import keyboards

    ids = []
    for i, w in enumerate([80, 79]):
        ids.append(await dbmod.add_bodyweight_log(user_id, w, logged_at=f"2026-0{i + 1}-01T10:00:00"))
    state = await _make_state(user_id)

    monkeypatch.setattr(keyboards, "BODYWEIGHT_LIST_PAGE_SIZE", 1)
    # page 1 holds the oldest entry (newest-first order) — delete it.
    callback = _make_bw_confirm_callback(user_id, f"bw:delrec:{ids[0]}:1")
    await bw.bw_delete_record(callback, state)

    text = callback.message.answer.await_args.args[0]
    assert "1. 01.02.2026" in text  # back on page 0, the remaining entry


@pytest.mark.asyncio
async def test_declining_a_suspicious_bodyweight_leaves_the_log_empty(fresh_db, user_id):
    import handlers.bodyweight as bw
    from fsm import BodyweightFlow

    state = await _make_state(user_id)
    await state.set_state(BodyweightFlow.viewing)
    await state.update_data(bw_pending_weight=300.0)

    callback = _make_bw_confirm_callback(user_id, "bw:wconf:no")
    await bw.bw_weight_confirm_no(callback, state)

    assert await dbmod.list_bodyweight_logs(user_id) == []


def test_bodyweight_screen_fits_the_caption_cap_on_a_long_history():
    """The screen is sent as a photo caption. An over-long one doesn't truncate —
    safe_edit_photo has already deleted the old screen when the send fails, so
    the whole screen would disappear from the chat."""
    import datetime as dt

    import formatting

    start = dt.date(2026, 1, 1)
    logs = [
        {
            "weight": 82.5 + (i % 7) * 0.1,
            "logged_at": f"{start + dt.timedelta(days=i)}T08:00:00",
        }
        for i in range(120)
    ]
    text = formatting.build_bodyweight_screen(logs)

    assert formatting.telegram_length(text) <= formatting.CAPTION_LIMIT
    assert "Показано" in text  # and it says the list was cut
    assert "Напиши вес" in text  # the call to action survives the trim


def test_bodyweight_screen_keeps_the_most_recent_entries():
    import datetime as dt

    import formatting

    start = dt.date(2026, 1, 1)
    logs = [
        {"weight": 80.0 + i * 0.1, "logged_at": f"{start + dt.timedelta(days=i)}T08:00:00"}
        for i in range(60)
    ]
    text = formatting.build_bodyweight_screen(logs)

    assert "01.03.2026" in text  # newest entry (day 60) kept
    assert "01.01.2026" not in text  # oldest trimmed away


def test_bodyweight_screen_short_history_lists_everything():
    import formatting

    logs = [{"weight": 82.5, "logged_at": "2026-01-01T08:00:00"},
            {"weight": 82.1, "logged_at": "2026-01-02T08:00:00"}]
    text = formatting.build_bodyweight_screen(logs)

    assert "01.01.2026" in text and "02.01.2026" in text
    assert "Показано" not in text


# ---------- одно взвешивание в день, и задним числом ----------


def test_bodyweight_screen_collapses_several_entries_of_one_day():
    """Взвесился трижды подряд — в списке всё равно один день, последнее число.
    Раньше три попытки занимали три строки и выглядели тремя днями."""
    logs = [
        {"weight": 81.4, "logged_at": "2026-02-01T07:00:00"},
        {"weight": 81.0, "logged_at": "2026-02-01T07:05:00"},
        {"weight": 80.8, "logged_at": "2026-02-01T07:09:00"},
    ]
    text = formatting.build_bodyweight_screen(logs, "kg")
    assert text.count("01.02.2026 — ") == 1
    assert "01.02.2026 — 80.8кг" in text
    assert "Всего 1 взвешивание." in text


def test_parse_bodyweight_entry_reads_a_past_date():
    import datetime as dt

    from parser import parse_bodyweight_entry

    weight, date = parse_bodyweight_entry("82.5 01.08.2026", today=dt.date(2026, 8, 7))
    assert weight == 82.5
    assert date == dt.date(2026, 8, 1)


def test_parse_bodyweight_entry_without_a_date_stays_plain():
    from parser import parse_bodyweight_entry

    assert parse_bodyweight_entry("80,5") == (80.5, None)


def test_parse_bodyweight_entry_rejects_a_future_date():
    import datetime as dt

    import pytest

    from parser import ParseError, parse_bodyweight_entry

    with pytest.raises(ParseError):
        parse_bodyweight_entry("82.5 01.09.2026", today=dt.date(2026, 8, 7))
