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
