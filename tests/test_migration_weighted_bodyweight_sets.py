"""Миграция v18 (db._move_weighted_sets_to_weighted_variant): подходы с весом
из упражнения «только повторы» («Подтягивания» с поясом) переезжают в копию
варианта «с весом» — найденную по идентичности или заведённую из каталога.

Каждый тест собирает историю на свежей базе прямо через db (как её писали до
запрета веса), гонит миграцию и смотрит на блоки, раунды, заметки и снимок
нагрузки, а в конце — на экран прогресса обоих упражнений через /v1."""

import httpx
import pytest

import api_v1
import db

pytestmark = pytest.mark.asyncio

PULL = "Подтягивания"
WEIGHTED = "Подтягивания с весом"


async def _template_id(name: str) -> int:
    cur = await db.conn().execute(
        "SELECT id FROM exercises WHERE is_template = 1 AND user_id IS NULL AND name = ?", (name,)
    )
    return (await cur.fetchone())["id"]


async def _user(telegram_id: int = 111, lang: str = "ru") -> int:
    await db.get_or_create_user(telegram_id=telegram_id, username=f"u{telegram_id}")
    await db.set_user_lang(telegram_id, lang)
    return telegram_id


async def _fork(user_id: int, name: str) -> int:
    return await db.fork_exercise_from_template(user_id, await _template_id(name))


async def _workout(user_id: int, day: int, *, import_batch_id: str | None = None) -> int:
    return await db.create_finished_workout(
        user_id,
        started_at=f"2026-03-{day:02d}T10:00:00",
        finished_at=f"2026-03-{day:02d}T11:00:00",
        import_batch_id=import_batch_id,
    )


async def _block(workout_id: int, entries: dict[int, list[tuple[float, int]]], kind: str = "single") -> int:
    """Блок с упражнениями `entries` (порядок — order_in_block) и их подходами
    раундами: раунд i — i-й подход каждого упражнения, как пишет суперсет."""
    block_id = await db.create_block(workout_id, kind)
    for order, ex_id in enumerate(entries):
        await db.add_block_exercise(block_id, ex_id, order)
    rounds = max(len(sets) for sets in entries.values())
    for i in range(rounds):
        for order, (ex_id, sets) in enumerate(entries.items()):
            if i < len(sets):
                weight, reps = sets[i]
                await db.add_set(block_id, ex_id, i + 1, order, weight, reps)
    return block_id


async def _sets(block_id: int, ex_id: int) -> list[tuple]:
    cur = await db.conn().execute(
        "SELECT round_index, order_in_round, weight, reps FROM sets "
        "WHERE block_id = ? AND exercise_id = ? ORDER BY round_index, id",
        (block_id, ex_id),
    )
    return [tuple(r) for r in await cur.fetchall()]


async def _layout(workout_id: int) -> list[tuple[str, list[int]]]:
    """Порядок блоков тренировки: (тип, [exercise_id по order_in_block])."""
    out = []
    for block in await db.list_blocks_for_workout(workout_id):
        exs = [be["exercise_id"] for be in await db.get_block_exercises(block["id"])]
        out.append((block["type"], exs))
    return out


async def _copies(user_id: int, original_name: str) -> list:
    cur = await db.conn().execute(
        "SELECT * FROM exercises WHERE user_id = ? AND is_template = 0 AND original_name = ?",
        (user_id, original_name),
    )
    return await cur.fetchall()


async def _weighted_left(ex_id: int) -> int:
    cur = await db.conn().execute(
        "SELECT COUNT(*) FROM sets WHERE exercise_id = ? AND weight > 0", (ex_id,)
    )
    return (await cur.fetchone())[0]


# ---------- целиком с весом ----------


@pytest.mark.parametrize("lang, shown", [("ru", WEIGHTED), ("en", "Weighted Pull-Ups")])
async def test_only_weighted_workout_moves_whole_block_and_forks_variant(fresh_db, lang, shown):
    user = await _user(lang=lang)
    pull = await _fork(user, PULL)
    bench = await _fork(user, "Жим штанги лёжа")
    wid = await _workout(user, 1)
    pull_block = await _block(wid, {pull: [(10, 6), (10, 5), (12.5, 4)]})
    bench_block = await _block(wid, {bench: [(80, 8)]})
    await db.set_workout_exercise_note(wid, pull, "пояс 10")
    assert await _copies(user, WEIGHTED) == []

    stats = await db._move_weighted_sets_to_weighted_variant()

    [variant] = await _copies(user, WEIGHTED)
    # Заведена из каталога, как это делает приложение: имя на языке атлета,
    # режим нагрузки каталога.
    assert variant["display_name"] == shown
    assert (variant["bodyweight_load"], variant["bodyweight_factor"]) == ("full", 1.0)
    assert variant["last_used_at"] == "2026-03-01T10:00:00"
    # Тот же блок, то же место, те же раунды — сменилось только упражнение.
    assert await _layout(wid) == [("single", [variant["id"]]), ("single", [bench])]
    assert await _sets(pull_block, variant["id"]) == [
        (1, 0, 10, 6), (2, 0, 10, 5), (3, 0, 12.5, 4)
    ]
    assert await _sets(pull_block, pull) == []
    assert await _sets(bench_block, bench) == [(1, 0, 80, 8)]
    # Заметка уехала за подходами: простого в тренировке не осталось.
    assert await db.get_workout_exercise_note(wid, variant["id"]) == "пояс 10"
    assert await db.get_workout_exercise_note(wid, pull) is None
    assert stats["sets"] == 3 and stats["forked"] == 1 and stats["workouts"] == 1


# ---------- смешанная тренировка ----------


async def test_mixed_workout_splits_weighted_sets_into_a_new_block_right_after(fresh_db):
    user = await _user()
    pull = await _fork(user, PULL)
    bench = await _fork(user, "Жим штанги лёжа")
    wid = await _workout(user, 2)
    await _block(wid, {bench: [(80, 8)]})
    pull_block = await _block(wid, {pull: [(0, 12), (10, 6), (0, 10), (15, 3)]})
    row = await _block(wid, {await _fork(user, "Тяга штанги в наклоне"): [(60, 10)]})
    await db.set_workout_exercise_note(wid, pull, "хват шире")

    await db._move_weighted_sets_to_weighted_variant()

    variant = (await _copies(user, WEIGHTED))[0]["id"]
    layout = await _layout(wid)
    assert [exs for _t, exs in layout] == [[bench], [pull], [variant], [layout[3][1][0]]]
    blocks = await db.list_blocks_for_workout(wid)
    assert [b["order_index"] for b in blocks] == [0, 1, 2, 3]
    assert blocks[3]["id"] == row
    # Без веса остались на месте (с пропусками в раундах — как после удаления
    # подхода), с весом — в новом блоке раундами 1..n.
    assert await _sets(pull_block, pull) == [(1, 0, 0, 12), (3, 0, 0, 10)]
    assert await _sets(blocks[2]["id"], variant) == [(1, 0, 10, 6), (2, 0, 15, 3)]
    # Простое в тренировке осталось — заметка про него и остаётся.
    assert await db.get_workout_exercise_note(wid, pull) == "хват шире"
    assert await db.get_workout_exercise_note(wid, variant) is None


# ---------- копия варианта уже есть ----------


async def test_existing_variant_copy_is_reused_revived_and_appended_to(fresh_db):
    user = await _user()
    pull = await _fork(user, PULL)
    variant = await _fork(user, WEIGHTED)
    await db.archive_exercise(variant)
    wid = await _workout(user, 3)
    # Вариант уже записан в этой тренировке, а простое с весом — отдельным блоком.
    variant_block = await _block(wid, {variant: [(20, 3)]})
    pull_block = await _block(wid, {pull: [(10, 5), (10, 4)]})
    await db.set_workout_exercise_note(wid, variant, "тяжело")
    await db.set_workout_exercise_note(wid, pull, "пояс")

    stats = await db._move_weighted_sets_to_weighted_variant()

    assert [r["id"] for r in await _copies(user, WEIGHTED)] == [variant]
    assert stats["forked"] == 0 and stats["revived"] == 1
    assert not (await db.get_exercise(variant))["is_archived"]
    assert await _sets(variant_block, variant) == [(1, 0, 20, 3), (2, 0, 10, 5), (3, 0, 10, 4)]
    # Блок простого опустел и ушёл; в тренировке один блок варианта.
    assert await db.get_block(pull_block) is None
    assert await _layout(wid) == [("single", [variant])]
    assert await db.get_workout_exercise_note(wid, variant) == "тяжело\nпояс"
    assert await db.get_workout_exercise_note(wid, pull) is None


# ---------- суперсет ----------


async def test_superset_all_weighted_keeps_the_superset_in_place(fresh_db):
    user = await _user()
    pull = await _fork(user, PULL)
    dips = await _fork(user, "Отжимания на брусьях")
    wid = await _workout(user, 4)
    block = await _block(wid, {dips: [(0, 12), (0, 10)], pull: [(10, 6), (10, 5)]}, kind="superset")

    await db._move_weighted_sets_to_weighted_variant()

    variant = (await _copies(user, WEIGHTED))[0]["id"]
    assert await _layout(wid) == [("superset", [dips, variant])]
    # order_in_round = место в блоке, раунды совпадают с партнёром.
    assert await _sets(block, variant) == [(1, 1, 10, 6), (2, 1, 10, 5)]
    assert await _sets(block, dips) == [(1, 0, 0, 12), (2, 0, 0, 10)]


async def test_superset_mixed_and_superset_with_the_variant_itself(fresh_db):
    user = await _user()
    pull = await _fork(user, PULL)
    variant = await _fork(user, WEIGHTED)
    dips = await _fork(user, "Отжимания на брусьях")
    # Простое и вариант в одном суперсете: подходы с весом дописываются к
    # варианту в том же блоке, простое уходит из блока.
    w1 = await _workout(user, 5)
    b1 = await _block(w1, {pull: [(5, 8)], variant: [(20, 3)]}, kind="superset")
    # Смешанный суперсет с брусьями: без веса остаются, с весом — новый блок следом.
    w2 = await _workout(user, 6)
    b2 = await _block(w2, {dips: [(0, 12), (0, 10)], pull: [(0, 8), (10, 5)]}, kind="superset")

    await db._move_weighted_sets_to_weighted_variant()

    assert await _layout(w1) == [("single", [variant])]
    assert await _sets(b1, variant) == [(1, 1, 20, 3), (2, 1, 5, 8)]
    layout = await _layout(w2)
    assert layout == [("superset", [dips, pull]), ("single", [variant])]
    assert await _sets(b2, pull) == [(1, 1, 0, 8)]
    new_block = (await db.list_blocks_for_workout(w2))[1]["id"]
    assert await _sets(new_block, variant) == [(1, 0, 10, 5)]


# ---------- что не трогается ----------


async def test_other_users_and_exercises_are_untouched(fresh_db):
    user = await _user()
    other = await _user(222)
    pull = await _fork(user, PULL)
    dips = await _fork(user, "Отжимания на брусьях")  # своим весом, варианта нет
    bench = await _fork(user, "Жим штанги лёжа")
    other_pull = await _fork(other, PULL)
    w1 = await _workout(user, 7)
    await _block(w1, {dips: [(10, 8)]})
    await _block(w1, {bench: [(80, 8)]})
    await _block(w1, {pull: [(0, 10)]})
    w2 = await _workout(other, 7)
    await _block(w2, {other_pull: [(0, 9), (0, 8)]})
    before_sets = [tuple(r) for r in await (await db.conn().execute("SELECT * FROM sets ORDER BY id")).fetchall()]
    before_blocks = [tuple(r) for r in await (await db.conn().execute("SELECT * FROM block_exercises")).fetchall()]

    stats = await db._move_weighted_sets_to_weighted_variant()

    assert stats["sets"] == 0 and stats["forked"] == 0
    assert await _copies(user, WEIGHTED) == [] and await _copies(other, WEIGHTED) == []
    after_sets = [tuple(r) for r in await (await db.conn().execute("SELECT * FROM sets ORDER BY id")).fetchall()]
    after_blocks = [tuple(r) for r in await (await db.conn().execute("SELECT * FROM block_exercises")).fetchall()]
    assert after_sets == before_sets and after_blocks == before_blocks
    assert await _weighted_left(dips) == 1


async def test_only_the_owners_weighted_sets_move(fresh_db):
    user = await _user()
    other = await _user(222, lang="en")
    pull = await _fork(user, PULL)
    other_pull = await _fork(other, PULL)
    await _block(await _workout(user, 8), {pull: [(10, 5)]})
    other_block = await _block(await _workout(other, 8), {other_pull: [(0, 9)]})

    await db._move_weighted_sets_to_weighted_variant()

    assert len(await _copies(user, WEIGHTED)) == 1
    assert await _copies(other, WEIGHTED) == []
    assert await _sets(other_block, other_pull) == [(1, 0, 0, 9)]


# ---------- импорт, открытая тренировка, снимок нагрузки, события ----------


async def test_imported_moves_active_is_skipped_load_kept_no_events(fresh_db):
    user = await _user()
    await db.add_bodyweight_log(user, 80.0, logged_at="2026-02-01T08:00:00")
    pull = await _fork(user, PULL)
    imported = await _workout(user, 9, import_batch_id="batch-1")
    imported_block = await _block(imported, {pull: [(10, 5)]})
    active = await db.create_workout(user)
    active_block = await _block(active, {pull: [(10, 4)]})
    cur = await db.conn().execute("SELECT load_weight FROM sets WHERE block_id = ?", (imported_block,))
    assert (await cur.fetchone())["load_weight"] == 90.0
    events_before = (await (await db.conn().execute("SELECT COUNT(*) FROM analytics_events")).fetchone())[0]

    stats = await db._move_weighted_sets_to_weighted_variant()

    variant = (await _copies(user, WEIGHTED))[0]
    assert variant["import_batch_id"] is None  # заведена миграцией, не импортом
    assert (await db.get_workout(imported))["import_batch_id"] == "batch-1"
    cur = await db.conn().execute(
        "SELECT exercise_id, load_weight FROM sets WHERE block_id = ?", (imported_block,)
    )
    row = await cur.fetchone()
    assert (row["exercise_id"], row["load_weight"]) == (variant["id"], 90.0)
    # Открытая тренировка — не трогаем: её блоки держит живой экран.
    assert await _sets(active_block, pull) == [(1, 0, 10, 4)]
    assert stats["skipped_active"] == 1
    events_after = (await (await db.conn().execute("SELECT COUNT(*) FROM analytics_events")).fetchone())[0]
    assert events_after == events_before


async def test_second_run_is_a_no_op(fresh_db):
    user = await _user()
    pull = await _fork(user, PULL)
    wid = await _workout(user, 10)
    await _block(wid, {pull: [(0, 10), (10, 5)]})
    first = await db._move_weighted_sets_to_weighted_variant()
    layout = await _layout(wid)

    second = await db._move_weighted_sets_to_weighted_variant()

    assert first["sets"] == 1 and second["sets"] == 0 and second["forked"] == 0
    assert len(await _copies(user, WEIGHTED)) == 1
    assert await _layout(wid) == layout
    assert await _weighted_left(pull) == 0


async def test_runs_once_on_startup_from_v17(fresh_db):
    user = await _user()
    pull = await _fork(user, PULL)
    await _block(await _workout(user, 11), {pull: [(10, 5)]})
    await db.conn().execute("PRAGMA user_version = 17")
    await db.conn().commit()

    await db._run_one_shot_migrations()

    assert await _weighted_left(pull) == 0
    assert len(await _copies(user, WEIGHTED)) == 1
    (version,) = await (await db.conn().execute("PRAGMA user_version")).fetchone()
    assert version == db._SCHEMA_VERSION == 18


# ---------- прогресс обоих упражнений через /v1 ----------


async def _linked(telegram_id: int) -> httpx.AsyncClient:
    code = await db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def test_progress_of_both_exercises_is_clean_after_the_move(fresh_db):
    user = await _user()
    pull = await _fork(user, PULL)
    await _block(await _workout(user, 12), {pull: [(0, 12), (0, 10)]})
    await _block(await _workout(user, 14), {pull: [(0, 11), (10, 6), (10, 5)]})
    await _block(await _workout(user, 16), {pull: [(15, 4), (15, 3)]})

    await db._move_weighted_sets_to_weighted_variant()

    variant = (await _copies(user, WEIGHTED))[0]["id"]
    client = await _linked(user)
    plain = (await client.get(f"/exercises/{pull}/progress/sessions")).json()
    assert plain["metric"] == "reps"
    assert [p["date"] for p in plain["points"]] == ["2026-03-12", "2026-03-14"]
    assert all(p["max_weight"] == 0 for p in plain["points"])
    assert [p["max_reps"] for p in plain["points"]] == [12, 11]

    loaded = (await client.get(f"/exercises/{variant}/progress/sessions")).json()
    assert loaded["metric"] == "e1rm"
    assert [p["date"] for p in loaded["points"]] == ["2026-03-14", "2026-03-16"]
    assert [p["max_weight"] for p in loaded["points"]] == [10, 15]
    assert [p["sets"] for p in loaded["points"]] == [2, 2]
    assert loaded["records"] is not None
    await client.aclose()
