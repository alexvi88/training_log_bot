"""Sticker reactions: pack loading/caching, occasion matching, and the opt-outs."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramAPIError

import config
import stickers

pytestmark = pytest.mark.asyncio


def _pack(*emoji_by_id: tuple[str, str]):
    return SimpleNamespace(
        stickers=[SimpleNamespace(file_id=fid, emoji=emoji) for fid, emoji in emoji_by_id]
    )


def _bot(pack=None, error: Exception | None = None):
    bot = MagicMock()
    bot.get_sticker_set = AsyncMock(side_effect=error) if error else AsyncMock(return_value=pack)
    bot.send_sticker = AsyncMock()
    return bot


@pytest.fixture(autouse=True)
def configured_pack(monkeypatch):
    monkeypatch.setattr(config, "STICKERS_ENABLED", True)
    monkeypatch.setattr(config, "STICKER_PACK_NAMES", ["krepysh"])
    stickers.reset_cache()
    yield
    stickers.reset_cache()


async def test_picks_a_sticker_matching_the_occasions_emoji():
    bot = _bot(_pack(("sleepy", "😴"), ("flex", "💪"), ("chart", "📈")))

    assert await stickers.send(bot, 42, stickers.WORKOUT_DONE) is True
    assert bot.send_sticker.await_args.kwargs["sticker"] == "flex"


async def test_variation_selectors_and_modifiers_still_match():
    """Pack authors tag stickers with 🏋️‍♂️ / 👏🏽; the occasion table lists 🏋 / 👏."""
    bot = _bot(_pack(("lifter", "🏋️‍♂️"), ("clap", "👏🏽")))

    await stickers.send(bot, 42, stickers.WORKOUT_DONE)
    assert bot.send_sticker.await_args.kwargs["sticker"] == "clap"  # 👏 outranks 🏋


async def test_falls_back_to_any_sticker_when_no_emoji_matches():
    bot = _bot(_pack(("cat", "🐈")))

    assert await stickers.send(bot, 42, stickers.WORKOUT_DONE) is True
    assert bot.send_sticker.await_args.kwargs["sticker"] == "cat"


async def test_avoids_repeating_the_previous_sticker_in_the_same_chat():
    bot = _bot(_pack(("flex_a", "💪"), ("flex_b", "💪")))

    await stickers.send(bot, 42, stickers.WORKOUT_DONE)
    first = bot.send_sticker.await_args.kwargs["sticker"]
    await stickers.send(bot, 42, stickers.WORKOUT_DONE)
    assert bot.send_sticker.await_args.kwargs["sticker"] != first


async def test_pack_is_fetched_once_and_cached():
    bot = _bot(_pack(("flex", "💪")))

    await stickers.send(bot, 42, stickers.WORKOUT_DONE)
    await stickers.send(bot, 43, stickers.WORKOUT_DONE)
    assert bot.get_sticker_set.await_count == 1


async def test_missing_pack_is_survived_and_not_retried_immediately():
    bot = _bot(error=TelegramAPIError(method=MagicMock(), message="STICKERSET_INVALID"))

    assert await stickers.send(bot, 42, stickers.WORKOUT_DONE) is False
    assert await stickers.send(bot, 42, stickers.WORKOUT_DONE) is False
    assert bot.get_sticker_set.await_count == 1
    bot.send_sticker.assert_not_awaited()


async def test_send_failure_never_raises():
    bot = _bot(_pack(("flex", "💪")))
    bot.send_sticker = AsyncMock(side_effect=RuntimeError("boom"))

    assert await stickers.send(bot, 42, stickers.WORKOUT_DONE) is False


async def test_nothing_is_sent_without_a_configured_pack(monkeypatch):
    monkeypatch.setattr(config, "STICKER_PACK_NAMES", [])
    bot = _bot(_pack(("flex", "💪")))

    assert stickers.is_configured() is False
    assert await stickers.send(bot, 42, stickers.WORKOUT_DONE) is False
    bot.get_sticker_set.assert_not_awaited()


async def test_global_switch_off_silences_stickers(monkeypatch):
    monkeypatch.setattr(config, "STICKERS_ENABLED", False)
    bot = _bot(_pack(("flex", "💪")))

    assert await stickers.send(bot, 42, stickers.WORKOUT_DONE) is False


async def test_send_to_user_respects_the_per_user_setting(fresh_db, user_id):
    bot = _bot(_pack(("flex", "💪")))

    assert await stickers.send_to_user(bot, user_id, stickers.WORKOUT_DONE) is True

    await fresh_db.update_user(user_id, stickers_enabled=0)
    bot.send_sticker.reset_mock()
    assert await stickers.send_to_user(bot, user_id, stickers.WORKOUT_DONE) is False
    bot.send_sticker.assert_not_awaited()


async def test_send_to_user_skips_unknown_users(fresh_db):
    bot = _bot(_pack(("flex", "💪")))

    assert await stickers.send_to_user(bot, 999999, stickers.WORKOUT_DONE) is False
    bot.send_sticker.assert_not_awaited()


async def test_stickers_are_on_by_default_for_new_users(fresh_db):
    user = await fresh_db.get_or_create_user(telegram_id=777, username="newbie")
    assert user["stickers_enabled"] == 1


# ---------- integration: where stickers actually appear ----------


async def test_push_sends_the_sticker_before_the_text(fresh_db, user_id):
    """The CTA button has to stay the bottom-most thing the user sees."""
    import engagement
    import push_texts

    order = []
    bot = _bot(_pack(("jab", "🤨")))
    bot.send_sticker = AsyncMock(side_effect=lambda **kw: order.append("sticker"))
    bot.send_message = AsyncMock(side_effect=lambda **kw: order.append("text"))

    decision = engagement.PushDecision(push_texts.SKIP_3, "ПРИВЕТ АТЛЕТ, третий день без зала.")
    await engagement._deliver(bot, user_id, decision)

    assert order == ["sticker", "text"]
    assert bot.send_sticker.await_args.kwargs["sticker"] == "jab"


async def test_push_still_goes_out_when_the_pack_is_broken(fresh_db, user_id):
    import engagement
    import push_texts

    bot = _bot(error=TelegramAPIError(method=MagicMock(), message="STICKERSET_INVALID"))
    bot.send_message = AsyncMock()

    decision = engagement.PushDecision(push_texts.SKIP_3, "ПРИВЕТ АТЛЕТ, третий день без зала.")
    await engagement._deliver(bot, user_id, decision)

    bot.send_message.assert_awaited_once()


async def test_settings_toggle_hidden_until_a_pack_is_configured(monkeypatch, fresh_db, user_id):
    import keyboards

    def labels():
        kb = keyboards.settings_keyboard(
            "kg", "epley", True, True, True,
            stickers_enabled=True, show_stickers_toggle=stickers.is_configured(),
        )
        return [b.text for row in kb.inline_keyboard for b in row]

    assert any("Стикеры" in label for label in labels())
    monkeypatch.setattr(config, "STICKER_PACK_NAMES", [])
    assert not any("Стикеры" in label for label in labels())


async def _finish_a_workout(db, bot, user_id: int, weight: float) -> None:
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.types import CallbackQuery

    from fsm import WorkoutFlow
    from handlers import workout

    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, f"Bench {weight}", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, bench, 0)
    await db.add_set(block_id, bench, 1, 0, weight, 5)

    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await state.set_state(WorkoutFlow.idle)
    await state.update_data(live_chat_id=user_id, live_message_id=1, workout_id=workout_id)
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.bot = bot
    callback.answer = AsyncMock()
    await workout._finalize_workout(callback, state, note=None)


async def test_finished_workout_gets_a_sticker_only_when_it_earned_a_badge(fresh_db, user_id):
    bot = _bot(_pack(("cup", "🏆")))
    bot.edit_message_text = AsyncMock()
    bot.send_message = AsyncMock()
    bot.delete_message = AsyncMock()

    # First ever workout unlocks 🌱 "Первый шаг".
    await _finish_a_workout(fresh_db, bot, user_id, weight=60)
    bot.send_sticker.assert_awaited_once()

    # The second one is just a workout — no badge, no milestone, no sticker.
    bot.send_sticker.reset_mock()
    await _finish_a_workout(fresh_db, bot, user_id, weight=62.5)
    bot.send_sticker.assert_not_awaited()


async def test_finished_workout_sticker_respects_the_user_setting(fresh_db, user_id):
    bot = _bot(_pack(("cup", "🏆")))
    bot.edit_message_text = AsyncMock()
    bot.send_message = AsyncMock()
    bot.delete_message = AsyncMock()
    await fresh_db.update_user(user_id, stickers_enabled=0)

    await _finish_a_workout(fresh_db, bot, user_id, weight=60)
    bot.send_sticker.assert_not_awaited()
