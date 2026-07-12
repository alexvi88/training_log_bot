"""Text rotation: no repeat within a cycle, persisted per user+category."""

import pytest

import push_texts

pytestmark = pytest.mark.asyncio


async def test_every_variant_is_a_caps_atlet_address():
    for pool in push_texts.TEXTS.values():
        for text in pool:
            assert "АТЛЕТ" in text
            assert "боец" not in text.lower()


async def test_pick_text_cycles_through_pool_without_repeats(fresh_db, user_id):
    pool = push_texts.TEXTS[push_texts.WIN_BACK]
    seen = [await push_texts.pick_text(user_id, push_texts.WIN_BACK) for _ in range(len(pool))]
    assert sorted(seen) == sorted(pool)


async def test_pick_text_reshuffles_after_the_pool_is_exhausted(fresh_db, user_id):
    pool = push_texts.TEXTS[push_texts.SKIP]
    first_cycle = [await push_texts.pick_text(user_id, push_texts.SKIP) for _ in range(len(pool))]
    second_cycle = [await push_texts.pick_text(user_id, push_texts.SKIP) for _ in range(len(pool))]
    assert sorted(first_cycle) == sorted(pool)
    assert sorted(second_cycle) == sorted(pool)


async def test_rotation_is_isolated_per_user(fresh_db, user_id):
    other_id = 222
    await fresh_db.get_or_create_user(telegram_id=other_id, username="other")
    pool = push_texts.TEXTS[push_texts.WIN_BACK]

    await push_texts.pick_text(user_id, push_texts.WIN_BACK)
    remaining_for_other = await fresh_db.get_rotation_bag(other_id, push_texts.WIN_BACK)
    assert remaining_for_other == []

    seen_for_other = [await push_texts.pick_text(other_id, push_texts.WIN_BACK) for _ in range(len(pool))]
    assert sorted(seen_for_other) == sorted(pool)


async def test_pick_text_formats_placeholders():
    text = push_texts.TEXTS[push_texts.PLATEAU][0].format(exercise="Жим лёжа")
    assert "Жим лёжа" in text
