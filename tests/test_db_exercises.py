import asyncio

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
        for ex_id in ex_ids:
            block_id = await db.create_block(w, "single")
            await db.add_block_exercise(block_id, ex_id, 0)

    for _ in range(3):
        await _sequence(pulldown, row)
    for _ in range(2):
        await _sequence(pulldown, curl)

    followups = await db.list_common_followups(user_id, pulldown, limit=2)
    assert [f["id"] for f in followups] == [row, curl]


async def test_list_common_followups_ignores_a_one_off_pairing(fresh_db, user_id):
    """One leg day that happened to end with abs shouldn't make abs a suggestion
    on every upper-body day — a followup needs a repeat to count."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    pulldown = await db.create_exercise(user_id, "Pull down", group_id)
    abs_ex = await db.create_exercise(user_id, "Abs", group_id)

    w = await db.create_workout(user_id)
    for ex_id in (pulldown, abs_ex):
        block_id = await db.create_block(w, "single")
        await db.add_block_exercise(block_id, ex_id, 0)

    assert await db.list_common_followups(user_id, pulldown, limit=2) == []


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
    """Once an exercise is finished, the shortcuts should reflect what
    usually follows *that* exercise, not just whatever was logged most
    recently anywhere."""
    from handlers import workout

    group_id = await fresh_db.create_muscle_group(user_id, "Спина")
    pulldown = await fresh_db.create_exercise(user_id, "Pull down", group_id)
    row = await fresh_db.create_exercise(user_id, "Seated row", group_id)
    unrelated = await fresh_db.create_exercise(user_id, "Overhead press", group_id)
    await fresh_db.touch_exercise_last_used(unrelated)  # most recent overall, but never followed pulldown

    for _ in range(2):  # twice — a single pairing doesn't count as a habit
        w = await fresh_db.create_workout(user_id)
        block_a = await fresh_db.create_block(w, "single")
        await fresh_db.add_block_exercise(block_a, pulldown, 0)
        block_b = await fresh_db.create_block(w, "single")
        await fresh_db.add_block_exercise(block_b, row, 0)

    hint, kb = await workout._idle_view({"last_finished_exercise_id": pulldown}, user_id, is_empty=False)
    texts = [b.text for r in kb.inline_keyboard for b in r]
    assert "Seated row" in texts
    assert "Overhead press" not in texts


async def test_idle_view_falls_back_to_recent_when_no_established_followup(fresh_db, user_id):
    """Nothing reliably follows this exercise yet — the shortcut row shows the
    most recent exercises instead of going empty."""
    from handlers import workout

    group_id = await fresh_db.create_muscle_group(user_id, "Спина")
    pulldown = await fresh_db.create_exercise(user_id, "Pull down", group_id)
    row = await fresh_db.create_exercise(user_id, "Seated row", group_id)
    await fresh_db.touch_exercise_last_used(row)

    hint, kb = await workout._idle_view(
        {"last_finished_exercise_id": pulldown}, user_id, is_empty=False
    )
    texts = [b.text for r in kb.inline_keyboard for b in r]
    assert "Seated row" in texts


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
    assert "Seated row" in texts
    assert "Pull down" in texts


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


async def test_same_name_in_different_case_reuses_the_cyrillic_exercise(fresh_db, user_id):
    """SQLite's LOWER() only folds ASCII, so the unique index behind
    create_exercise's dedup treats "Жим лёжа" and "жим лёжа" as two names —
    splitting one exercise into two, each with its own history and records."""
    db = fresh_db
    gid = await db.create_muscle_group(user_id, "Грудь")

    first = await db.create_exercise(user_id, "Жим лёжа", gid)
    second = await db.create_exercise(user_id, "жим лёжа", gid)

    assert second == first
    assert await db.count_user_exercises(user_id) == 1


async def test_case_insensitive_lookup_still_works_for_ascii_names(fresh_db, user_id):
    db = fresh_db
    gid = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Bench Press", gid)

    found = await db.find_exercise_by_display_name(user_id, "bench press")
    assert found is not None and found["id"] == ex_id


async def test_concurrent_create_block_gets_distinct_order_indexes(fresh_db, user_id):
    """aiogram processes updates concurrently, so two blocks can be opened at
    once (a double-tapped exercise, "➕ Суперсет" racing the picker). Reading
    MAX(order_index) before the INSERT let both land on the same position,
    leaving the workout's exercise order arbitrary."""
    db = fresh_db
    workout_id = await db.create_workout(user_id)

    blocks = await asyncio.gather(
        db.create_block(workout_id, "single"),
        db.create_block(workout_id, "single"),
        db.create_block(workout_id, "single"),
    )

    rows = await db.list_blocks_for_workout(workout_id)
    assert sorted(r["order_index"] for r in rows) == [0, 1, 2]
    assert len(set(blocks)) == 3


async def test_concurrent_append_routine_exercise_gets_distinct_order_indexes(fresh_db, user_id):
    db = fresh_db
    gid = await db.create_muscle_group(user_id, "Спина")
    routine_id = await db.create_routine(user_id, "Тяга")
    ex_ids = [await db.create_exercise(user_id, f"Упражнение {i}", gid) for i in range(3)]

    await asyncio.gather(*(db.append_routine_exercise(routine_id, ex) for ex in ex_ids))

    rows = await db.list_routine_exercises(routine_id)
    assert sorted(r["order_index"] for r in rows) == [0, 1, 2]


# ---------- English-locale search/fork/identity regressions ----------
#
# Шаблоны каталога в базе навсегда по-русски (`t.display_name` == `t.name`),
# а с приходом английской локализации то, что видит пользователь на другом
# языке, разошлось с этим — и сверка "по имени" там, где имелась в виду
# сверка "по идентичности" (`original_name`), стала ломаться сразу в
# нескольких местах. См. CLAUDE.md/PR-ревью: search_exercise_templates,
# fork_exercise_from_template, get_or_create_user_exercise_by_name,
# resolve_exercise_name.


async def test_search_exercise_templates_finds_an_english_query(fresh_db, user_id):
    """До фикса SQL-фильтр сравнивал английский запрос с t.display_name, а тот
    в базе всегда русский ("Жим штанги лёжа") — «bench», «squat», «curl»
    возвращали пустой список для любого пользователя, независимо от его
    языка интерфейса."""
    db = fresh_db
    for query in ("bench", "bench press", "squat", "curl", "deadlift"):
        matches = await db.search_exercise_templates(user_id, query)
        assert matches, f"{query!r} found nothing"


async def test_search_exercise_templates_english_match_excludes_owned_fork(fresh_db, user_id):
    """NOT EXISTS раньше сравнивал o.display_name с t.display_name — а форк на
    английском языке носит английское display_name, которое с русским
    t.display_name никогда не совпадёт, так что уже добавленный шаблон
    предлагался повторно."""
    db = fresh_db
    await db.set_user_lang(user_id, "en")
    template = next(
        t for t in await db.list_all_exercise_templates() if t["name"] == "Жим штанги лёжа"
    )
    await db.fork_exercise_from_template(user_id, template["id"])

    for query in ("bench press", "жим штанги лёжа"):
        matches = await db.search_exercise_templates(user_id, query)
        assert "Жим штанги лёжа" not in [m["name"] for m in matches], query


async def test_forking_the_same_template_in_two_languages_is_one_row(fresh_db, user_id):
    """fork_exercise_from_template пишет локализованное имя, а старый дедуп
    (create_exercise, по display_name) не видит, что «Жим штанги лёжа» и
    «Bench Press» — один и тот же шаблон: без identity-проверки форк на
    другом языке создавал ВТОРУЮ строку с тем же original_name, и история,
    рекорды и графики расходились по ним."""
    db = fresh_db
    template = next(
        t for t in await db.list_all_exercise_templates() if t["name"] == "Жим штанги лёжа"
    )

    await db.set_user_lang(user_id, "ru")
    first_id = await db.fork_exercise_from_template(user_id, template["id"])

    await db.set_user_lang(user_id, "en")
    second_id = await db.fork_exercise_from_template(user_id, template["id"])

    assert first_id == second_id
    rows = [
        e for e in await db.list_user_exercises(user_id)
        if e["original_name"] == "Жим штанги лёжа"
    ]
    assert len(rows) == 1


async def test_forking_onto_a_hand_typed_same_name_exercise_still_reuses_it(fresh_db, user_id):
    """Своё упражнение, названное пользователем руками так же, как локализация
    шаблона, должно по-прежнему работать как раньше — форк садится на ТУ же
    строку, а не плодит вторую."""
    db = fresh_db
    await db.set_user_lang(user_id, "en")
    group_id = await db.create_muscle_group(user_id, "Грудь")
    hand_id = await db.create_exercise(user_id, "Barbell Bench Press", group_id)

    template = next(
        t for t in await db.list_all_exercise_templates() if t["name"] == "Жим штанги лёжа"
    )
    forked_id = await db.fork_exercise_from_template(user_id, template["id"])

    assert forked_id == hand_id
    assert await db.count_user_exercises(user_id) == 1


async def test_program_install_does_not_mark_existing_fork_as_seeded(fresh_db, user_id):
    """Ошибка 3: поиск по русскому имени не находил форк англоязычного
    пользователя, поэтому установка программы на уже добавленное (форкнутое)
    упражнение помечала его seeded_from_program = 1 — и последующее удаление
    программы могло спрятать это упражнение, хотя оно реально используется."""
    db = fresh_db
    await db.set_user_lang(user_id, "en")
    template = next(
        t for t in await db.list_all_exercise_templates() if t["name"] == "Жим штанги лёжа"
    )
    existing_id = await db.fork_exercise_from_template(user_id, template["id"])
    # Пользователь реально потренировал упражнение — не «просиротевший» форк.
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, existing_id, 0)
    await db.finish_workout(workout_id)

    resolved_id = await db.get_or_create_user_exercise_by_name(user_id, "Жим штанги лёжа")

    assert resolved_id == existing_id
    ex = await db.get_exercise(existing_id)
    assert ex["seeded_from_program"] == 0


async def test_resolve_exercise_name_preview_matches_what_gets_saved(fresh_db, user_id):
    """Ошибка 4: resolve_exercise_name отдавал сырое русское имя шаблона в
    превью, а экран готовых программ локализует то же самое имя — то есть два
    экрана про одно упражнение говорили по-разному. Превью должно совпадать с
    тем именем, которое реально ляжет в БД при установке программы."""
    db = fresh_db
    await db.set_user_lang(user_id, "en")

    kind, preview_name = await db.resolve_exercise_name(user_id, "Жим штанги лёжа")
    assert kind == "template"

    saved_id = await db.get_or_create_user_exercise_by_name(user_id, "Жим штанги лёжа")
    saved = await db.get_exercise(saved_id)

    assert preview_name == saved["display_name"]


async def test_resolve_exercise_name_recognizes_an_existing_fork_as_own(fresh_db, user_id):
    """Тот же порядок identity-проверки, что и в get_or_create...: превью не
    должно говорить "template" (то есть "будет создано новое"), если шаблон
    пользователь уже когда-то форкнул — пусть даже на другом языке."""
    db = fresh_db
    await db.set_user_lang(user_id, "en")
    template = next(
        t for t in await db.list_all_exercise_templates() if t["name"] == "Жим штанги лёжа"
    )
    existing_id = await db.fork_exercise_from_template(user_id, template["id"])

    kind, name = await db.resolve_exercise_name(user_id, "Жим штанги лёжа")

    assert kind == "own"
    assert name == (await db.get_exercise(existing_id))["display_name"]
