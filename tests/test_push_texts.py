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
    pool = push_texts.TEXTS[push_texts.SKIP_3]
    first_cycle = [await push_texts.pick_text(user_id, push_texts.SKIP_3) for _ in range(len(pool))]
    second_cycle = [await push_texts.pick_text(user_id, push_texts.SKIP_3) for _ in range(len(pool))]
    assert sorted(first_cycle) == sorted(pool)
    assert sorted(second_cycle) == sorted(pool)


async def test_skip_category_by_day_covers_every_milestone():
    assert set(push_texts.SKIP_CATEGORY_BY_DAY) == set(push_texts.SKIP_MILESTONE_DAYS)
    for day, category in push_texts.SKIP_CATEGORY_BY_DAY.items():
        assert push_texts.TEXTS[category], f"no copy for skip day {day}"


async def test_skip_pools_never_reference_a_different_days_wording():
    # a day-3 skip must never draw a "две недели"/"неделя" line, and vice versa
    day_words = {
        push_texts.SKIP_3: ["трет", "три дня"],
        push_texts.SKIP_5: ["пят"],
        push_texts.SKIP_7: ["недел"],
        push_texts.SKIP_10: ["десят"],
        push_texts.SKIP_14: ["четырнадцат", "две недели"],
    }
    for category, allowed_fragments in day_words.items():
        for text in push_texts.TEXTS[category]:
            assert any(f in text.lower() for f in allowed_fragments), text


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
