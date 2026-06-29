from seed_data import EXERCISE_TEMPLATES, MUSCLE_GROUP_PRESETS

# asyncio_mode=auto (pytest.ini) runs the async tests below without an explicit
# marker, and leaves the pure-data sync tests alone — no module-level asyncio mark.


# ---------- pure data-integrity checks on the catalog itself ----------

def test_seed_template_groups_are_known():
    group_names = {name for name, _emoji, _order in MUSCLE_GROUP_PRESETS}
    for group_name, _ex_name in EXERCISE_TEMPLATES:
        assert group_name in group_names, f"unknown group {group_name!r}"


def test_seed_templates_have_no_duplicates():
    seen = set()
    for group_name, ex_name in EXERCISE_TEMPLATES:
        key = (group_name, ex_name.lower())
        assert key not in seen, f"duplicate template {ex_name!r} in {group_name!r}"
        seen.add(key)


def test_every_group_has_templates():
    by_group = {}
    for group_name, _ex_name in EXERCISE_TEMPLATES:
        by_group[group_name] = by_group.get(group_name, 0) + 1
    for name, _emoji, _order in MUSCLE_GROUP_PRESETS:
        assert by_group.get(name, 0) > 0, f"group {name!r} has no templates"


# ---------- DB-level reconciliation ----------

async def _global_template_pairs(db):
    """All seeded global templates as a set of (group_name, exercise_name)."""
    groups = await db.list_muscle_groups(user_id=None, global_only=True)
    name_by_id = {g["id"]: g["name"] for g in groups}
    cur = await db.conn().execute(
        "SELECT primary_group_id, name FROM exercises WHERE is_template = 1 AND user_id IS NULL"
    )
    rows = await cur.fetchall()
    return {(name_by_id.get(r["primary_group_id"]), r["name"]) for r in rows}


async def _insert_template(db, group_id, name):
    await db.conn().execute(
        "INSERT INTO exercises "
        "(user_id, name, primary_group_id, display_name, original_name, is_template, created_at) "
        "VALUES (NULL, ?, ?, ?, ?, 1, ?)",
        (name, group_id, name, name, db.now_iso()),
    )
    await db.conn().commit()


async def test_catalog_matches_seed_data_on_fresh_db(fresh_db):
    db = fresh_db
    assert await _global_template_pairs(db) == {
        (group_name, ex_name) for group_name, ex_name in EXERCISE_TEMPLATES
    }


async def test_sync_is_idempotent(fresh_db):
    db = fresh_db
    before = await _global_template_pairs(db)
    await db._sync_exercise_templates()
    await db._sync_exercise_templates()
    assert await _global_template_pairs(db) == before


async def test_sync_adds_missing_and_removes_obsolete(fresh_db):
    db = fresh_db
    groups = await db.list_muscle_groups(user_id=None, global_only=True)
    chest = next(g for g in groups if g["name"] == "Грудь")

    # Drop a catalog entry and inject a stale one that's no longer in EXERCISE_TEMPLATES.
    await db.conn().execute(
        "DELETE FROM exercises WHERE is_template = 1 AND name = ?", ("Жим штанги лёжа",)
    )
    await db.conn().commit()
    await _insert_template(db, chest["id"], "Упражнение из старого пресета")

    await db._sync_exercise_templates()

    pairs = await _global_template_pairs(db)
    assert ("Грудь", "Жим штанги лёжа") in pairs  # re-added
    assert ("Грудь", "Упражнение из старого пресета") not in pairs  # pruned


async def test_sync_prunes_duplicate_templates(fresh_db):
    db = fresh_db
    groups = await db.list_muscle_groups(user_id=None, global_only=True)
    chest = next(g for g in groups if g["name"] == "Грудь")

    # An older buggy seed could insert the same template twice.
    await _insert_template(db, chest["id"], "Жим штанги лёжа")
    cur = await db.conn().execute(
        "SELECT COUNT(*) FROM exercises WHERE is_template = 1 AND name = ?", ("Жим штанги лёжа",)
    )
    (dup_count,) = await cur.fetchone()
    assert dup_count == 2

    await db._sync_exercise_templates()

    cur = await db.conn().execute(
        "SELECT COUNT(*) FROM exercises WHERE is_template = 1 AND name = ?", ("Жим штанги лёжа",)
    )
    (count,) = await cur.fetchone()
    assert count == 1


async def test_sync_leaves_user_exercises_untouched(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    own_id = await db.create_exercise(user_id, "Мой жим", group_id)

    # A forked-from-template copy is a normal user exercise and must survive a resync.
    chest = next(
        g for g in await db.list_muscle_groups(user_id=None, global_only=True) if g["name"] == "Грудь"
    )
    template = (await db.list_templates_in_group(chest["id"]))[0]
    forked_id = await db.fork_exercise_from_template(user_id, template["id"])

    before = await db.count_user_exercises(user_id)
    await db._sync_exercise_templates()

    assert await db.count_user_exercises(user_id) == before
    assert await db.get_exercise(own_id) is not None
    assert await db.get_exercise(forked_id) is not None
