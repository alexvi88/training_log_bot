"""The sticky reference photo pinned above the live tracker: it appears when an
exercise becomes active, is replaced on switch, and is cleaned up when there's no
active exercise left."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import exercise_media
from fsm import WorkoutFlow
from handlers import workout

# A seeded template that really does have photos on disk, so get_images() is exercised
# for real rather than mocked.
EX_WITH_PHOTO = "Тяга верхнего блока"


def _make_bot(user_id: int):
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    next_msg_id = iter(range(800, 900))

    async def _send_message(*args, **kwargs):
        return SimpleNamespace(message_id=next(next_msg_id), chat=SimpleNamespace(id=user_id))

    async def _send_photo(*args, **kwargs):
        return SimpleNamespace(message_id=next(next_msg_id), chat=SimpleNamespace(id=user_id))

    async def _send_media_group(*args, media, **kwargs):
        return [
            SimpleNamespace(
                message_id=next(next_msg_id),
                photo=[SimpleNamespace(file_id=f"file_id_{i}")],
            )
            for i, _ in enumerate(media)
        ]

    bot.send_message = AsyncMock(side_effect=_send_message)
    bot.send_photo = AsyncMock(side_effect=_send_photo)
    bot.send_media_group = AsyncMock(side_effect=_send_media_group)
    return bot


async def _make_state(user_id: int, chat_id: int = 42) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    state = FSMContext(storage=storage, key=key)
    await state.set_state(WorkoutFlow.logging_set)
    await state.update_data(live_chat_id=chat_id, live_message_id=1)
    return state


@pytest.fixture(autouse=True)
def _clear_file_id_cache():
    exercise_media._FILE_IDS.clear()
    yield
    exercise_media._FILE_IDS.clear()


@pytest.mark.asyncio
async def test_sticky_photo_sent_for_active_exercise(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    ex_id = await db.create_exercise(user_id, EX_WITH_PHOTO, group_id)
    state = await _make_state(user_id)
    bot = _make_bot(user_id)

    await workout._sync_sticky_photo(bot, state, ex_id)

    bot.send_media_group.assert_awaited_once()
    media = bot.send_media_group.await_args.kwargs["media"]
    assert len(media) == 2
    assert EX_WITH_PHOTO in media[0].caption
    assert media[1].caption is None  # only the first item of an album carries a caption

    data = await state.get_data()
    assert data["sticky_photo_ex_id"] == ex_id
    assert len(data["sticky_photo_msg_ids"]) == 2


@pytest.mark.asyncio
async def test_sticky_photo_not_resent_for_same_exercise(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    ex_id = await db.create_exercise(user_id, EX_WITH_PHOTO, group_id)
    state = await _make_state(user_id)
    bot = _make_bot(user_id)

    await workout._sync_sticky_photo(bot, state, ex_id)
    await workout._sync_sticky_photo(bot, state, ex_id)

    bot.send_media_group.assert_awaited_once()
    bot.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_sticky_photo_replaced_on_switch(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    first = await db.create_exercise(user_id, EX_WITH_PHOTO, group_id)
    second = await db.create_exercise(user_id, "Подтягивания", group_id)
    state = await _make_state(user_id)
    bot = _make_bot(user_id)

    await workout._sync_sticky_photo(bot, state, first)
    old_ids = (await state.get_data())["sticky_photo_msg_ids"]

    await workout._sync_sticky_photo(bot, state, second)

    assert bot.send_media_group.await_count == 2
    deleted = {c.kwargs["message_id"] for c in bot.delete_message.await_args_list}
    assert deleted == set(old_ids)
    data = await state.get_data()
    assert data["sticky_photo_ex_id"] == second
    assert not set(data["sticky_photo_msg_ids"]) & set(old_ids)


@pytest.mark.asyncio
async def test_sticky_photo_cleared_when_no_active_exercise(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    ex_id = await db.create_exercise(user_id, EX_WITH_PHOTO, group_id)
    state = await _make_state(user_id)
    bot = _make_bot(user_id)

    await workout._sync_sticky_photo(bot, state, ex_id)
    sent_ids = (await state.get_data())["sticky_photo_msg_ids"]

    await workout._sync_sticky_photo(bot, state, None)

    deleted = {c.kwargs["message_id"] for c in bot.delete_message.await_args_list}
    assert deleted == set(sent_ids)
    data = await state.get_data()
    assert data["sticky_photo_msg_ids"] is None
    assert data["sticky_photo_ex_id"] is None


@pytest.mark.asyncio
async def test_sticky_photo_prefers_custom_photo(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    ex_id = await db.create_exercise(user_id, EX_WITH_PHOTO, group_id)
    await db.set_exercise_photo(ex_id, "custom_file_id_123")
    state = await _make_state(user_id)
    bot = _make_bot(user_id)

    await workout._sync_sticky_photo(bot, state, ex_id)

    bot.send_photo.assert_awaited_once()
    assert bot.send_photo.await_args.kwargs["photo"] == "custom_file_id_123"
    bot.send_media_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_exercise_without_photo_sends_nothing_and_is_not_retried(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    ex_id = await db.create_exercise(user_id, "Совсем новое упражнение XYZ", group_id)
    state = await _make_state(user_id)
    bot = _make_bot(user_id)

    await workout._sync_sticky_photo(bot, state, ex_id)
    await workout._sync_sticky_photo(bot, state, ex_id)

    bot.send_media_group.assert_not_awaited()
    bot.send_photo.assert_not_awaited()
    data = await state.get_data()
    assert data["sticky_photo_msg_ids"] == []
    assert data["sticky_photo_ex_id"] == ex_id


@pytest.mark.asyncio
async def test_second_send_reuses_cached_file_ids(fresh_db, user_id):
    """The first upload is a real file; every later send is just the file_id string."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    ex_id = await db.create_exercise(user_id, EX_WITH_PHOTO, group_id)
    other = await db.create_exercise(user_id, "Подтягивания", group_id)
    state = await _make_state(user_id)
    bot = _make_bot(user_id)

    await workout._sync_sticky_photo(bot, state, ex_id)
    first_media = bot.send_media_group.await_args.kwargs["media"]
    assert all(not isinstance(m.media, str) for m in first_media)

    await workout._sync_sticky_photo(bot, state, other)
    await workout._sync_sticky_photo(bot, state, ex_id)

    third_media = bot.send_media_group.await_args.kwargs["media"]
    assert [m.media for m in third_media] == ["file_id_0", "file_id_1"]


@pytest.mark.asyncio
async def test_logging_screen_sends_photo_above_tracker(fresh_db, user_id):
    """End-to-end through the real render path: the photo must be sent *before* the
    tracker, so the re-sent tracker always lands underneath it."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    ex_id = await db.create_exercise(user_id, EX_WITH_PHOTO, group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)

    state = await _make_state(user_id)
    await state.update_data(
        workout_id=workout_id, open_exercises=[ex_id], open_blocks={ex_id: block_id},
        active_exercise_id=ex_id,
    )
    bot = _make_bot(user_id)
    calls: list[str] = []
    bot.send_media_group.side_effect_order = None
    original_group, original_message = bot.send_media_group.side_effect, bot.send_message.side_effect

    async def _track_group(*args, **kwargs):
        calls.append("photo")
        return await original_group(*args, **kwargs)

    async def _track_message(*args, **kwargs):
        calls.append("tracker")
        return await original_message(*args, **kwargs)

    bot.send_media_group.side_effect = _track_group
    bot.send_message.side_effect = _track_message

    user = await db.get_user(user_id)
    await workout._render_logging_screen(bot, state, user)

    assert calls == ["photo", "tracker"]


@pytest.mark.asyncio
async def test_idle_screen_clears_sticky_photo(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    ex_id = await db.create_exercise(user_id, EX_WITH_PHOTO, group_id)
    workout_id = await db.create_workout(user_id)

    state = await _make_state(user_id)
    await state.update_data(workout_id=workout_id)
    bot = _make_bot(user_id)
    await workout._sync_sticky_photo(bot, state, ex_id)
    sent_ids = (await state.get_data())["sticky_photo_msg_ids"]

    user = await db.get_user(user_id)
    await workout._enter_idle_screen(bot, state, user, workout_id)

    deleted = {c.kwargs["message_id"] for c in bot.delete_message.await_args_list}
    assert set(sent_ids) <= deleted
    assert (await state.get_data())["sticky_photo_msg_ids"] is None
