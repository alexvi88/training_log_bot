"""Drives the progress-chart handlers (§7) end to end against a real DB.

Covers the photo-spam fix: viewing an exercise's chart must clear whatever
screen was on screen before it, and switching the period on an already-shown
chart must edit that same message instead of sending a new one.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import charts
import chat_bottom
from handlers import history

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def clean_chat_tracker():
    chat_bottom.reset()
    yield
    chat_bottom.reset()


def _make_callback(user_id: int, data: str, *, chart_on_screen: bool = False):
    """chart_on_screen: the current screen already is a chart photo sitting at the
    bottom of the chat — the case where ui.safe_edit_photo can swap media in place
    instead of deleting and resending."""
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 500
    message.text = None if chart_on_screen else "экран"
    message.photo = [SimpleNamespace(file_id="x")] if chart_on_screen else None
    message.delete = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=1))
    message.answer_photo = AsyncMock(return_value=SimpleNamespace(message_id=2))
    message.edit_media = AsyncMock(return_value=True)
    if chart_on_screen:
        chat_bottom.note_message(user_id, message.message_id)
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _make_state(user_id: int) -> FSMContext:
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=MemoryStorage(), key=key)


async def _seed_exercise_with_sessions(db, user_id: int, n_sessions: int) -> int:
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)
    for i in range(1, n_sessions + 1):
        workout_id = await db.create_finished_workout(
            user_id, started_at=f"2026-01-{i:02d}T10:00:00", finished_at=f"2026-01-{i:02d}T10:30:00"
        )
        block_id = await db.create_block(workout_id, "single")
        await db.add_block_exercise(block_id, ex_id, 0)
        await db.add_set(block_id, ex_id, round_index=1, order_in_round=0, weight=100.0, reps=8)
    return ex_id


async def test_prog_show_exercise_deletes_previous_screen_before_sending_chart(fresh_db, user_id):
    """The exercise-list message must be cleared, not left behind, when a chart is sent."""
    ex_id = await _seed_exercise_with_sessions(fresh_db, user_id, 3)
    state = await _make_state(user_id)

    callback = _make_callback(user_id, f"prog:ex:{ex_id}")
    await history.prog_show_exercise(callback, state)

    callback.message.delete.assert_awaited_once()
    callback.message.answer_photo.assert_awaited_once()
    callback.answer.assert_awaited_once()


async def test_prog_show_exercise_no_sessions_still_clears_previous_screen(fresh_db, user_id):
    group_id = await fresh_db.create_muscle_group(user_id, "Спина")
    ex_id = await fresh_db.create_exercise(user_id, "Тяга", group_id)
    state = await _make_state(user_id)

    callback = _make_callback(user_id, f"prog:ex:{ex_id}")
    await history.prog_show_exercise(callback, state)

    callback.message.delete.assert_awaited_once()
    callback.message.answer.assert_awaited_once()
    callback.message.answer_photo.assert_not_awaited()


async def test_prog_change_period_edits_chart_in_place(fresh_db, user_id):
    """Switching the shown period must reuse the existing chart message (no spam)."""
    ex_id = await _seed_exercise_with_sessions(fresh_db, user_id, 3)
    state = await _make_state(user_id)

    callback = _make_callback(user_id, f"prog:per:{ex_id}:20", chart_on_screen=True)
    await history.prog_change_period(callback, state)

    callback.message.edit_media.assert_awaited_once()
    callback.message.delete.assert_not_awaited()
    callback.message.answer_photo.assert_not_awaited()


async def test_prog_change_period_resends_when_something_landed_below(fresh_db, user_id):
    """Going through ui.safe_edit_photo means an editable-in-place chart is only
    edited while it's still the bottom message — otherwise the refreshed chart
    would be stranded above whatever arrived after it."""
    ex_id = await _seed_exercise_with_sessions(fresh_db, user_id, 3)
    state = await _make_state(user_id)

    callback = _make_callback(user_id, f"prog:per:{ex_id}:20", chart_on_screen=True)
    chat_bottom.note_message(user_id, callback.message.message_id + 1)  # a push, say

    await history.prog_change_period(callback, state)

    callback.message.edit_media.assert_not_awaited()
    callback.message.delete.assert_awaited_once()
    callback.message.answer_photo.assert_awaited_once()


async def test_prog_change_period_all_shows_every_session(fresh_db, user_id):
    ex_id = await _seed_exercise_with_sessions(fresh_db, user_id, 10)
    user = await fresh_db.get_user(user_id)

    text, png, kb = await history._render_progress_view(ex_id, user, 9999)

    assert "01.01.2026" in text
    assert "10.01.2026" in text
    assert png is not None


# ---------- _render_progress_view's per-user chart cache ----------


async def test_render_progress_view_reuses_cache_for_same_data_and_period(fresh_db, user_id, monkeypatch):
    ex_id = await _seed_exercise_with_sessions(fresh_db, user_id, 3)
    user = await fresh_db.get_user(user_id)
    calls = 0
    real_render = charts.render_metric_over_sessions

    def _counting_render(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_render(*args, **kwargs)

    monkeypatch.setattr(charts, "render_metric_over_sessions", _counting_render)

    text1, png1, _ = await history._render_progress_view(ex_id, user, 8)
    text2, png2, _ = await history._render_progress_view(ex_id, user, 8)

    assert calls == 1  # second call was a cache hit — no re-render
    assert png1 == png2
    assert text1 == text2


async def test_render_progress_view_invalidates_when_an_older_set_is_edited(fresh_db, user_id, monkeypatch):
    """Editing a set in a session that ISN'T the most recent one must still bust
    the cache — a fingerprint keyed only on the latest session would miss this."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)
    workout1 = await db.create_finished_workout(user_id, "2026-01-01T10:00:00", "2026-01-01T10:30:00")
    block1 = await db.create_block(workout1, "single")
    await db.add_block_exercise(block1, ex_id, 0)
    set_id = await db.add_set(block1, ex_id, round_index=1, order_in_round=0, weight=100.0, reps=8)
    workout2 = await db.create_finished_workout(user_id, "2026-01-08T10:00:00", "2026-01-08T10:30:00")
    block2 = await db.create_block(workout2, "single")
    await db.add_block_exercise(block2, ex_id, 0)
    await db.add_set(block2, ex_id, round_index=1, order_in_round=0, weight=105.0, reps=8)
    user = await db.get_user(user_id)

    calls = 0
    real_render = charts.render_metric_over_sessions

    def _counting_render(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_render(*args, **kwargs)

    monkeypatch.setattr(charts, "render_metric_over_sessions", _counting_render)

    text_before, _, _ = await history._render_progress_view(ex_id, user, 8)
    assert calls == 1

    # Edit the OLDER (first) workout's set, not the latest one.
    await db.update_set(set_id, weight=150.0, reps=8)

    text_after, _, _ = await history._render_progress_view(ex_id, user, 8)
    assert calls == 2  # re-rendered instead of serving the stale cached chart
    assert text_before != text_after


async def test_render_progress_view_does_not_leak_cache_across_exercises(fresh_db, user_id, monkeypatch):
    ex_a = await _seed_exercise_with_sessions(fresh_db, user_id, 3)
    group_id = await fresh_db.create_muscle_group(user_id, "Спина")
    ex_b = await fresh_db.create_exercise(user_id, "Тяга", group_id)
    workout = await fresh_db.create_finished_workout(user_id, "2026-02-01T10:00:00", "2026-02-01T10:30:00")
    block = await fresh_db.create_block(workout, "single")
    await fresh_db.add_block_exercise(block, ex_b, 0)
    await fresh_db.add_set(block, ex_b, round_index=1, order_in_round=0, weight=50.0, reps=10)
    user = await fresh_db.get_user(user_id)

    calls = 0
    real_render = charts.render_metric_over_sessions

    def _counting_render(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_render(*args, **kwargs)

    monkeypatch.setattr(charts, "render_metric_over_sessions", _counting_render)

    await history._render_progress_view(ex_a, user, 8)
    text_b, png_b, _ = await history._render_progress_view(ex_b, user, 8)
    assert calls == 2  # switching exercise is never a cache hit off the other one's render
    assert "Жим лёжа" not in text_b


def _back_button_cb(markup) -> str:
    for row in markup.inline_keyboard:
        for button in row:
            if button.text == "⬅️ Назад":
                return button.callback_data
    raise AssertionError("no back button found")


async def test_prog_show_exercise_back_returns_to_originating_group(fresh_db, user_id):
    """Opened via the group exercise list — back must return to that same group, not the top-level group picker."""
    group_id = await fresh_db.create_muscle_group(user_id, "Грудь")
    ex_id = await fresh_db.create_exercise(user_id, "Жим лёжа", group_id)
    state = await _make_state(user_id)

    callback = _make_callback(user_id, f"prog:ex:{ex_id}:{group_id}")
    await history.prog_show_exercise(callback, state)

    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    assert _back_button_cb(kb) == f"prog:grp:{group_id}"


async def test_prog_show_exercise_back_returns_to_exercise_detail_card(fresh_db, user_id):
    """Opened from the exercise-manage detail card ("⚙️ Упражнения") — back must return there, not to progress groups."""
    group_id = await fresh_db.create_muscle_group(user_id, "Грудь")
    ex_id = await fresh_db.create_exercise(user_id, "Жим лёжа", group_id)
    state = await _make_state(user_id)

    callback = _make_callback(user_id, f"prog:ex:{ex_id}:m")
    await history.prog_show_exercise(callback, state)

    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    assert _back_button_cb(kb) == f"exm:ex:{ex_id}"


async def test_prog_change_period_preserves_origin(fresh_db, user_id):
    """Switching the period must not lose track of where "⬅️ Назад" should return to."""
    ex_id = await _seed_exercise_with_sessions(fresh_db, user_id, 3)
    state = await _make_state(user_id)

    callback = _make_callback(user_id, f"prog:per:{ex_id}:20:m", chart_on_screen=True)
    await history.prog_change_period(callback, state)

    kb = callback.message.edit_media.await_args.kwargs["reply_markup"]
    assert _back_button_cb(kb) == f"exm:ex:{ex_id}"


def _has_button_cb(markup, cb: str) -> bool:
    return any(b.callback_data == cb for row in markup.inline_keyboard for b in row)


async def test_prog_group_list_paginates(fresh_db, user_id):
    """A group with more than one page of exercises shows a next-page arrow."""
    group_id = await fresh_db.create_muscle_group(user_id, "Грудь")
    for i in range(15):  # > RECENT_EXERCISES_LIMIT (8)
        await fresh_db.create_exercise(user_id, f"Упражнение {i}", group_id)
    state = await _make_state(user_id)

    callback = _make_callback(user_id, f"prog:grp:{group_id}")
    await history.prog_pick_group(callback, state)
    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    assert _has_button_cb(kb, f"prog:gpage:{group_id}:1")

    page1 = _make_callback(user_id, f"prog:gpage:{group_id}:1")
    await history.prog_group_page(page1, state)
    kb1 = page1.message.answer.await_args.kwargs["reply_markup"]
    assert _has_button_cb(kb1, f"prog:gpage:{group_id}:0")  # back to page 0


async def test_prog_show_exercise_has_card_button_regardless_of_origin(fresh_db, user_id):
    """The progress screen always offers a way to jump to the exercise's card,
    even when it wasn't opened from there (origin is a muscle group, not "m")."""
    group_id = await fresh_db.create_muscle_group(user_id, "Грудь")
    ex_id = await fresh_db.create_exercise(user_id, "Жим лёжа", group_id)
    state = await _make_state(user_id)

    callback = _make_callback(user_id, f"prog:ex:{ex_id}:{group_id}")
    await history.prog_show_exercise(callback, state)

    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    assert _has_button_cb(kb, f"prog:card:{ex_id}")


async def test_prog_card_button_shows_exercise_card_from_any_state(fresh_db, user_id):
    """Tapping the card button must render the exercise card even though the
    FSM state coming from the progress-menu flow isn't ExerciseManage.picking_exercise."""
    from handlers import exercises

    group_id = await fresh_db.create_muscle_group(user_id, "Грудь")
    ex_id = await fresh_db.create_exercise(user_id, "Жим лёжа", group_id)
    state = await _make_state(user_id)  # no FSM state set at all

    callback = _make_callback(user_id, f"prog:card:{ex_id}")
    await exercises.prog_show_exercise_card(callback, state)

    callback.message.delete.assert_awaited_once()
    text = callback.message.answer.await_args.args[0]
    assert "Жим лёжа" in text


# ---------- progress entry with no workout history yet ----------


async def test_progress_entry_offers_start_workout_when_no_history(fresh_db, user_id):
    """A brand-new user used to pick a group, see "пусто", and back out — the
    picker itself couldn't say there was nothing to show until already drilled in."""
    state = await _make_state(user_id)
    callback = _make_callback(user_id, "menu:progress")

    await history.show_progress_entry(callback, state)

    # No chart on screen and chat_bottom hasn't seen this message, so ui.safe_edit
    # takes the delete-and-resend path — the screen goes out via message.answer.
    sent = callback.message.answer.await_args
    assert "первой завершённой тренировки" in sent.args[0]
    kb = sent.kwargs["reply_markup"]
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "menu:start_workout" in cbs


async def test_progress_entry_shows_groups_once_a_workout_exists(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.add_set(block_id, ex_id, 1, 0, 100.0, 8)
    await db.finish_workout(workout_id)

    state = await _make_state(user_id)
    callback = _make_callback(user_id, "menu:progress")

    await history.show_progress_entry(callback, state)

    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert any(cb.startswith("prog:grp:") for cb in cbs)
    assert "menu:start_workout" not in cbs
