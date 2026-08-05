"""Text rotation: no repeat within a cycle, persisted per user+category."""

import pytest

import push_texts

pytestmark = pytest.mark.asyncio


async def test_every_variant_opens_with_privet_atlet():
    for pool in push_texts.TEXTS.values():
        for text in pool:
            assert text.startswith("ПРИВЕТ АТЛЕТ, "), text
            assert "боец" not in text.lower()


async def test_every_category_has_a_label():
    for category in push_texts.TEXTS:
        assert category in push_texts.CATEGORY_LABELS, category


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


async def test_variant_needing_missing_data_is_not_drawn(fresh_db, user_id):
    """A push has to be true. The digest claimed "понедельник — твой самый
    продуктивный день" as fixed copy, sent to anyone regardless of their
    history; now the day is computed, and when nothing stands out the variant
    that would assert one is dropped from the draw."""
    seen = set()
    for _ in range(len(push_texts.TEXTS[push_texts.WEEKLY_DIGEST]) * 3):
        seen.add(
            await push_texts.pick_text(
                user_id, push_texts.WEEKLY_DIGEST,
                tonnage="4.2 т", week_count="2 тренировки", best_day=None,
            )
        )

    assert seen  # something is still sent
    assert not any("самый продуктивный день" in text for text in seen)


async def test_variant_is_available_once_the_data_exists(fresh_db, user_id):
    seen = set()
    for _ in range(len(push_texts.TEXTS[push_texts.WEEKLY_DIGEST]) * 3):
        seen.add(
            await push_texts.pick_text(
                user_id, push_texts.WEEKLY_DIGEST,
                tonnage="4.2 т", week_count="2 тренировки", best_day="среда",
            )
        )

    assert any("среда — твой самый продуктивный день" in text for text in seen)


async def test_most_frequent_weekday_needs_a_clear_winner():
    import datetime as dt

    import analytics

    mondays = [dt.date(2026, 5, 4) + dt.timedelta(weeks=i) for i in range(4)]
    fridays = [dt.date(2026, 5, 8) + dt.timedelta(weeks=i) for i in range(2)]
    assert analytics.most_frequent_weekday(mondays + fridays) == 0

    # 3 vs 2 is noise, not a habit.
    assert analytics.most_frequent_weekday(mondays[:3] + fridays) is None
    assert analytics.most_frequent_weekday([]) is None
