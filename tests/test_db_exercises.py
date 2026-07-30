import pytest

pytestmark = pytest.mark.asyncio


async def _make_exercises(db, user_id, group_id, n, prefix="Exercise"):
    # create_exercise() dedupes by display_name per user (not per group), so
    # names must be unique across groups within a single test.
    ids = []
    for i in range(n):
        ex_id = await db.create_exercise(user_id, f"{prefix} {i:02d}", group_id)
        ids.append(ex_id)
    return ids


async def test_archive_then_unarchive_round_trips(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)

    await db.archive_exercise(ex_id)
    ex = await db.get_exercise(ex_id)
    assert ex["is_archived"] == 1
    archived = await db.list_archived_exercises(user_id)
    assert ex_id in {e["id"] for e in archived}
    assert ex_id not in {e["id"] for e in await db.list_user_exercises(user_id)}

    await db.unarchive_exercise(ex_id)
    ex = await db.get_exercise(ex_id)
    assert ex["is_archived"] == 0
    archived = await db.list_archived_exercises(user_id)
    assert ex_id not in {e["id"] for e in archived}
    assert ex_id in {e["id"] for e in await db.list_user_exercises(user_id)}


async def test_list_user_exercises_in_group_paginates(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    await _make_exercises(db, user_id, group_id, 10)

    total = await db.count_user_exercises_in_group(user_id, group_id)
    assert total == 10

    page0 = await db.list_user_exercises_in_group(user_id, group_id, limit=8, offset=0)
    page1 = await db.list_user_exercises_in_group(user_id, group_id, limit=8, offset=8)

    assert len(page0) == 8
    assert len(page1) == 2
    # no overlap between pages
    assert {r["id"] for r in page0}.isdisjoint({r["id"] for r in page1})


async def test_list_user_exercises_paginates_across_groups(fresh_db, user_id):
    db = fresh_db
    g1 = await db.create_muscle_group(user_id, "Грудь")
    g2 = await db.create_muscle_group(user_id, "Спина")
    await _make_exercises(db, user_id, g1, 5, prefix="Chest")
    await _make_exercises(db, user_id, g2, 5, prefix="Back")

    total = await db.count_user_exercises(user_id)
    assert total == 10

    page0 = await db.list_user_exercises(user_id, limit=8, offset=0)
    page1 = await db.list_user_exercises(user_id, limit=8, offset=8)
    assert len(page0) == 8
    assert len(page1) == 2


async def test_search_exercises_is_case_and_cyrillic_insensitive(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    await db.create_exercise(user_id, "Жим штанги лёжа", group_id)
    await db.create_exercise(user_id, "Присед", group_id)

    matches = await db.search_exercises(user_id, "ЖИМ")
    names = [r["display_name"] for r in matches]
    assert names == ["Жим штанги лёжа"]


async def test_search_exercises_escapes_like_wildcards(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    await db.create_exercise(user_id, "100% присед", group_id)
    await db.create_exercise(user_id, "Присед", group_id)

    # A literal "%" in the query should not act as a wildcard matching everything.
    matches = await db.search_exercises(user_id, "100%")
    names = [r["display_name"] for r in matches]
    assert names == ["100% присед"]


async def test_search_exercises_respects_limit(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    await _make_exercises(db, user_id, group_id, 5)

    matches = await db.search_exercises(user_id, "Exercise", limit=3)
    assert len(matches) == 3


async def test_get_next_exercise_in_workout_returns_following_block(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Bench press", group_id)
    triceps = await db.create_exercise(user_id, "Triceps pushdown", group_id)

    workout_id = await db.create_workout(user_id)
    b1 = await db.create_block(workout_id, "single")
    await db.add_block_exercise(b1, bench, 0)
    b2 = await db.create_block(workout_id, "single")
    await db.add_block_exercise(b2, triceps, 0)
    await db.finish_workout(workout_id)

    found_workout = await db.find_last_finished_workout_with_exercise(user_id, bench)
    assert found_workout == workout_id

    nxt = await db.get_next_exercise_in_workout(workout_id, bench)
    assert nxt["exercise_id"] == triceps


async def test_get_next_exercise_in_workout_none_when_last_exercise(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Bench press", group_id)

    workout_id = await db.create_workout(user_id)
    b1 = await db.create_block(workout_id, "single")
    await db.add_block_exercise(b1, bench, 0)
    await db.finish_workout(workout_id)

    assert await db.get_next_exercise_in_workout(workout_id, bench) is None


async def test_find_last_finished_workout_ignores_active_workout(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Bench press", group_id)

    active_id = await db.create_workout(user_id)
    b1 = await db.create_block(active_id, "single")
    await db.add_block_exercise(b1, bench, 0)

    assert await db.find_last_finished_workout_with_exercise(user_id, bench) is None


async def test_search_exercise_templates_finds_a_catalog_match(fresh_db, user_id):
    db = fresh_db
    # A brand-new user has nothing of their own yet — search_exercises alone
    # would say "ничего не нашлось" even though a matching template exists.
    matches = await db.search_exercise_templates(user_id, "жим штанги лёжа")
    names = [r["display_name"] for r in matches]
    assert "Жим штанги лёжа" in names
    assert all(r["is_template"] for r in matches)


async def test_search_exercise_templates_skips_ones_the_user_already_has(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    await db.create_exercise(user_id, "Жим штанги лёжа", group_id)

    # Already forked (or independently created) under the same name — showing
    # the template again would just be a confusing duplicate suggestion.
    matches = await db.search_exercise_templates(user_id, "жим штанги лёжа")
    assert "Жим штанги лёжа" not in [r["display_name"] for r in matches]


async def test_search_exercise_templates_respects_limit(fresh_db, user_id):
    db = fresh_db
    matches = await db.search_exercise_templates(user_id, "жим", limit=2)
    assert len(matches) <= 2


async def test_list_recent_exercises_orders_by_last_used(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    a = await db.create_exercise(user_id, "Pull down", group_id)
    b = await db.create_exercise(user_id, "Seated row", group_id)
    c = await db.create_exercise(user_id, "Lat pulldown", group_id)

    # touch_exercise_last_used stamps second-resolution "now", so setting the
    # timestamps directly (a full minute apart) is what actually exercises the
    # ordering — three real touches in a row could tie within the same second.
    await db.conn().execute("UPDATE exercises SET last_used_at = ? WHERE id = ?", ("2026-01-01T10:00:00", a))
    await db.conn().execute("UPDATE exercises SET last_used_at = ? WHERE id = ?", ("2026-01-01T10:01:00", b))
    await db.conn().execute("UPDATE exercises SET last_used_at = ? WHERE id = ?", ("2026-01-01T10:02:00", c))
    await db.conn().commit()

    recent = await db.list_recent_exercises(user_id, limit=2)
    assert [r["id"] for r in recent] == [c, b]


async def test_list_recent_exercises_excludes_given_ids(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    a = await db.create_exercise(user_id, "Pull down", group_id)
    b = await db.create_exercise(user_id, "Seated row", group_id)
    await db.touch_exercise_last_used(a)
    await db.touch_exercise_last_used(b)

    recent = await db.list_recent_exercises(user_id, limit=5, exclude_ids=(b,))
    assert [r["id"] for r in recent] == [a]


async def test_list_recent_exercises_skips_never_used(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    await db.create_exercise(user_id, "Never touched", group_id)
    assert await db.list_recent_exercises(user_id, limit=5) == []


async def _log_set(db, user_id, exercise_id, weight=100.0, reps=5):
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, exercise_id, 0)
    await db.add_set(block_id, exercise_id, 0, 0, weight, reps, None)


async def test_list_muscle_groups_order_by_usage_ranks_most_trained_first(fresh_db, user_id):
    db = fresh_db
    legs = await db.create_muscle_group(user_id, "Ноги")
    back = await db.create_muscle_group(user_id, "Спина")
    await db.create_muscle_group(user_id, "Грудь")  # never trained

    leg_ex = await db.create_exercise(user_id, "Squat", legs)
    back_ex = await db.create_exercise(user_id, "Row", back)
    await _log_set(db, user_id, back_ex)
    await _log_set(db, user_id, back_ex)
    await _log_set(db, user_id, back_ex)
    await _log_set(db, user_id, leg_ex)

    groups = await db.list_muscle_groups(user_id, order_by_usage=True)
    names = [g["name"] for g in groups]
    assert names.index("Спина") < names.index("Ноги") < names.index("Грудь")


async def test_list_muscle_groups_default_order_unaffected_by_usage(fresh_db, user_id):
    db = fresh_db
    await db.create_muscle_group(user_id, "Ноги нестандарт")
    back = await db.create_muscle_group(user_id, "Спина нестандарт")
    back_ex = await db.create_exercise(user_id, "Row", back)
    # Heavily used, but without order_by_usage this must not jump ahead of
    # "Ноги нестандарт" — both share the default sort_order, so plain name
    # order decides, same as before this exercise was ever logged.
    for _ in range(5):
        await _log_set(db, user_id, back_ex)

    groups = await db.list_muscle_groups(user_id)
    names = [g["name"] for g in groups]
    assert names.index("Ноги нестандарт") < names.index("Спина нестандарт")


async def test_list_common_followups_ranked_by_shared_workout_count(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    pulldown = await db.create_exercise(user_id, "Pull down", group_id)
    row = await db.create_exercise(user_id, "Seated row", group_id)
    curl = await db.create_exercise(user_id, "Curl", group_id)

    async def _sequence(*ex_ids):
        w = await db.create_workout(user_id)
        for i, ex_id in enumerate(ex_ids):
            block_id = await db.create_block(w, "single")
            await db.add_block_exercise(block_id, ex_id, 0)

    for _ in range(3):
        await _sequence(pulldown, row)
    await _sequence(pulldown, curl)

    followups = await db.list_common_followups(user_id, pulldown, limit=2)
    assert [f["id"] for f in followups] == [row, curl]


async def test_list_common_followups_ignores_exercises_that_came_before(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    pulldown = await db.create_exercise(user_id, "Pull down", group_id)
    row = await db.create_exercise(user_id, "Seated row", group_id)
    w = await db.create_workout(user_id)
    block_a = await db.create_block(w, "single")
    await db.add_block_exercise(block_a, row, 0)
    block_b = await db.create_block(w, "single")
    await db.add_block_exercise(block_b, pulldown, 0)

    followups = await db.list_common_followups(user_id, pulldown, limit=2)
    assert followups == []


async def test_idle_view_offers_common_followups_when_exercise_just_finished(fresh_db, user_id):
    """Once an exercise is finished, the "🕘" shortcuts should reflect what
    usually follows *that* exercise, not just whatever was logged most
    recently anywhere."""
    from handlers import workout

    group_id = await fresh_db.create_muscle_group(user_id, "Спина")
    pulldown = await fresh_db.create_exercise(user_id, "Pull down", group_id)
    row = await fresh_db.create_exercise(user_id, "Seated row", group_id)
    unrelated = await fresh_db.create_exercise(user_id, "Overhead press", group_id)
    await fresh_db.touch_exercise_last_used(unrelated)  # most recent overall, but never followed pulldown

    w = await fresh_db.create_workout(user_id)
    block_a = await fresh_db.create_block(w, "single")
    await fresh_db.add_block_exercise(block_a, pulldown, 0)
    block_b = await fresh_db.create_block(w, "single")
    await fresh_db.add_block_exercise(block_b, row, 0)

    hint, kb = await workout._idle_view({"last_finished_exercise_id": pulldown}, user_id, is_empty=False)
    texts = [b.text for r in kb.inline_keyboard for b in r]
    assert "🕘 Seated row" in texts
    assert "🕘 Overhead press" not in texts


async def test_idle_view_offers_recent_exercises_excluding_suggested(fresh_db, user_id):
    """The between-exercises pause screen should surface a couple of recently
    logged exercises as a one-tap shortcut, without repeating whatever is
    already offered as the "как в прошлый раз" suggestion."""
    from handlers import workout

    group_id = await fresh_db.create_muscle_group(user_id, "Спина")
    pull = await fresh_db.create_exercise(user_id, "Pull down", group_id)
    row = await fresh_db.create_exercise(user_id, "Seated row", group_id)
    await fresh_db.conn().execute(
        "UPDATE exercises SET last_used_at = ? WHERE id = ?", ("2026-01-01T10:00:00", pull)
    )
    await fresh_db.conn().execute(
        "UPDATE exercises SET last_used_at = ? WHERE id = ?", ("2026-01-01T10:01:00", row)
    )
    await fresh_db.conn().commit()

    hint, kb = await workout._idle_view({}, user_id, is_empty=False)
    texts = [b.text for r in kb.inline_keyboard for b in r]
    assert "🕘 Seated row" in texts
    assert "🕘 Pull down" in texts


# ---------- concurrent set logging ----------


async def test_append_set_numbers_rounds_sequentially(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    ex_id = await db.create_exercise(user_id, "Тяга", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)

    for _ in range(3):
        await db.append_set(block_id, ex_id, 0, 100.0, 8)

    assert [s["round_index"] for s in await db.list_sets_for_block(block_id)] == [1, 2, 3]


async def test_concurrent_appends_get_distinct_round_indexes(fresh_db, user_id):
    """aiogram handles updates concurrently, so two sets logged in quick
    succession used to read the same next_round_index before either inserted —
    20 racing writers all landed on round_index 1."""
    import asyncio

    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    ex_id = await db.create_exercise(user_id, "Тяга", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)

    await asyncio.gather(*[db.append_set(block_id, ex_id, 0, 100.0, 5) for _ in range(20)])

    indexes = [s["round_index"] for s in await db.list_sets_for_block(block_id)]
    assert len(indexes) == 20
    assert sorted(indexes) == list(range(1, 21))


async def test_append_set_counts_only_its_own_exercise(fresh_db, user_id):
    """A superset shares nothing: each exercise in the block numbers its own rounds."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    a = await db.create_exercise(user_id, "Тяга", group_id)
    b = await db.create_exercise(user_id, "Подтягивания", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, a, 0)
    await db.add_block_exercise(block_id, b, 1)

    await db.append_set(block_id, a, 0, 100.0, 8)
    await db.append_set(block_id, b, 1, 0.0, 10)
    await db.append_set(block_id, a, 0, 100.0, 8)

    sets = await db.list_sets_for_block(block_id)
    by_ex = {}
    for s in sets:
        by_ex.setdefault(s["exercise_id"], []).append(s["round_index"])
    assert by_ex[a] == [1, 2]
    assert by_ex[b] == [1]
