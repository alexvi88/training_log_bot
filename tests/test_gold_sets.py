"""Золотые подходы: 🥇 в трекере на лучшем сете за всю историю упражнения
и «Золотая книга» на экране прогресса.

Механика голд-сплита из LiveSplit: лучший сегмент отмечается независимо от
того, насколько хорош «ран» целиком — плохая тренировка всё равно может унести
рекорд, и именно там боты обычно молчат.
"""
import pytest

import analytics
import formatting
import view_builder

pytestmark = pytest.mark.asyncio


async def _exercise(db, user_id: int, name: str = "Жим лёжа"):
    gid = await db.create_muscle_group(user_id, "Грудь")
    return await db.create_exercise(user_id, name, gid)


async def _finished(db, user_id: int, ex_id: int, sets, started_at: str):
    workout_id = await db.create_workout(user_id, started_at=started_at)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    for weight, reps in sets:
        await db.append_set(block_id, ex_id, 0, weight, reps)
    await db.finish_workout(workout_id, finished_at=started_at)
    return workout_id


async def _live(db, user_id: int, ex_id: int, sets):
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    for weight, reps in sets:
        await db.append_set(block_id, ex_id, 0, weight, reps)
    return workout_id


# ---------- отметка в трекере ----------


async def test_set_beating_all_time_best_gets_the_gold_mark(fresh_db, user_id):
    db = fresh_db
    ex_id = await _exercise(db, user_id)
    await _finished(db, user_id, ex_id, [(100.0, 6)], "2026-05-01T10:00:00")
    workout_id = await _live(db, user_id, ex_id, [(95.0, 8), (105.0, 6)])

    blocks = await view_builder.build_block_views(workout_id, "epley", mark_golds=True)

    assert blocks[0].gold_index == 1
    text = formatting.build_live_session_text(blocks, active_exercise_id=ex_id)
    assert "105×6 🥇" in text
    assert "95×8 🥇" not in text


async def test_no_gold_when_nothing_beats_history(fresh_db, user_id):
    db = fresh_db
    ex_id = await _exercise(db, user_id)
    await _finished(db, user_id, ex_id, [(140.0, 5)], "2026-05-01T10:00:00")
    workout_id = await _live(db, user_id, ex_id, [(100.0, 5), (100.0, 5)])

    blocks = await view_builder.build_block_views(workout_id, "epley", mark_golds=True)

    assert blocks[0].gold_index is None
    assert "🥇" not in formatting.build_live_session_text(blocks, active_exercise_id=ex_id)


async def test_first_ever_session_earns_a_gold(fresh_db, user_id):
    """Нет истории — первый же осмысленный сет и есть лучший за всё время."""
    db = fresh_db
    ex_id = await _exercise(db, user_id)
    workout_id = await _live(db, user_id, ex_id, [(60.0, 10), (80.0, 8)])

    blocks = await view_builder.build_block_views(workout_id, "epley", mark_golds=True)

    assert blocks[0].gold_index == 1


async def test_only_the_best_set_of_the_session_is_marked(fresh_db, user_id):
    """Два 🥇 в одном упражнении читались бы как баг; рекордом остаётся лучший."""
    db = fresh_db
    ex_id = await _exercise(db, user_id)
    await _finished(db, user_id, ex_id, [(90.0, 5)], "2026-05-01T10:00:00")
    workout_id = await _live(db, user_id, ex_id, [(100.0, 5), (110.0, 5), (105.0, 5)])

    blocks = await view_builder.build_block_views(workout_id, "epley", mark_golds=True)

    assert blocks[0].gold_index == 1
    assert formatting.build_live_session_text(blocks, active_exercise_id=ex_id).count("🥇") == 1


async def test_gold_ignores_other_users_and_other_exercises(fresh_db, user_id):
    db = fresh_db
    ex_id = await _exercise(db, user_id)
    other_ex = await db.create_exercise(user_id, "Присед", None)
    stranger = (await db.get_or_create_user(telegram_id=999, username="s"))["telegram_id"]
    stranger_ex = await _exercise(db, stranger, "Жим лёжа")
    await _finished(db, stranger, stranger_ex, [(200.0, 5)], "2026-05-01T10:00:00")
    await _finished(db, user_id, other_ex, [(300.0, 5)], "2026-05-01T10:00:00")

    workout_id = await _live(db, user_id, ex_id, [(60.0, 5)])
    blocks = await view_builder.build_block_views(workout_id, "epley", mark_golds=True)

    assert blocks[0].gold_index == 0  # чужие и соседние рекорды не считаются


async def test_marking_is_opt_in_so_history_views_stay_cheap(fresh_db, user_id):
    db = fresh_db
    ex_id = await _exercise(db, user_id)
    workout_id = await _live(db, user_id, ex_id, [(100.0, 5)])

    blocks = await view_builder.build_block_views(workout_id, "epley")

    assert blocks[0].gold_index is None


async def test_brzycki_threshold_matches_the_python_formula(fresh_db, user_id):
    """SQL-порог — зеркало analytics.e1rm: при brzycki выше 10 повторов
    формула падает обратно в epley, и SQL обязан делать то же самое."""
    db = fresh_db
    ex_id = await _exercise(db, user_id)
    await _finished(db, user_id, ex_id, [(100.0, 12)], "2026-05-01T10:00:00")

    threshold = await db.max_e1rm_before_workout(user_id, ex_id, 0, "brzycki")

    assert threshold == pytest.approx(analytics.e1rm(100.0, 12, "brzycki"))


# ---------- золотая книга ----------


async def test_gold_book_collects_three_categories_with_dates(fresh_db, user_id):
    db = fresh_db
    ex_id = await _exercise(db, user_id)
    await _finished(db, user_id, ex_id, [(120.0, 1)], "2026-05-14T10:00:00")  # самый тяжёлый
    await _finished(db, user_id, ex_id, [(105.0, 6)], "2026-08-02T10:00:00")  # лучший e1RM
    await _finished(db, user_id, ex_id, [(60.0, 20)], "2026-03-12T10:00:00")  # больше всех повторов

    sessions = await _sessions(db, user_id, ex_id)
    book = analytics.gold_book(sessions)

    assert (book.best_e1rm_weight, book.best_e1rm_reps) == (105.0, 6)
    assert book.best_e1rm_date == "2026-08-02"
    assert (book.max_weight, book.max_weight_reps) == (120.0, 1)
    assert book.max_reps == 20

    lines = formatting.build_gold_book_lines(book)
    assert "🥇 <b>Золотая книга</b>" in lines[0]
    assert any("2 августа" in ln for ln in lines)
    assert any("120×1" in ln for ln in lines)


async def test_gold_book_collapses_a_set_that_wins_two_categories(fresh_db, user_id):
    """Один сет может быть и самым тяжёлым, и лучшим по e1RM — печатать его
    дважды значит показать «три рекорда», которых на самом деле два."""
    db = fresh_db
    ex_id = await _exercise(db, user_id)
    await _finished(db, user_id, ex_id, [(140.0, 5)], "2026-06-01T10:00:00")

    book = analytics.gold_book(await _sessions(db, user_id, ex_id))
    lines = formatting.build_gold_book_lines(book)

    assert sum("140×5" in ln for ln in lines) == 1


async def test_gold_book_is_empty_without_history():
    assert analytics.gold_book([]) is None
    assert formatting.build_gold_book_lines(None) == []


async def test_bodyweight_book_shows_reps_not_weight(fresh_db, user_id):
    db = fresh_db
    ex_id = await _exercise(db, user_id, "Подтягивания")
    await _finished(db, user_id, ex_id, [(0.0, 15)], "2026-06-01T10:00:00")

    book = analytics.gold_book(await _sessions(db, user_id, ex_id))
    lines = formatting.build_gold_book_lines(book, is_bodyweight=True)

    assert len(lines) == 2
    assert "Повторы 15" in lines[1]
    assert not any("e1RM" in ln for ln in lines)


async def _sessions(db, user_id: int, ex_id: int):
    rows = await db.list_sets_for_exercise(ex_id)
    return analytics.group_sets_by_session(
        [
            analytics.SetRow(
                weight=r["weight"], reps=r["reps"], workout_id=r["workout_id"],
                started_at=r["started_at"], rpe=r["rpe"],
            )
            for r in rows
        ]
    )
