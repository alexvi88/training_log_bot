"""Прогресс по упражнению, сменившему режим (с весом ↔ своим весом), терял
данные одного из режимов на графике и на текстовом экране.

`format_progress_screen` (formatting.py) и `_render_progress_view`
(handlers/history.py) решали режим ВСЕГО экрана/графика по последней сессии
(`sessions[-1].is_bodyweight_mode`), а не по каждой сессии отдельно:

- точка тяжёлой сессии на графике подписывалась как «повторы», если
  последняя сессия в истории была без веса (и наоборот) — число на графике
  переставало быть тем, что реально произошло в той тренировке;
- строка «Рекорд» показывала только один из двух рекордов, хотя
  `analytics.compute_personal_records` считает оба честно и независимо от
  режима сессии — настоящий e1RM-рекорд мог пропасть с экрана целиком.
"""

from unittest.mock import MagicMock, patch

import pytest

import analytics
import formatting

pytestmark = pytest.mark.asyncio


def _session(workout_id: int, started_at: str, weight: float, reps: int) -> analytics.SessionStats:
    return analytics.SessionStats(
        workout_id=workout_id,
        started_at=started_at,
        sets=[analytics.SetRow(weight=weight, reps=reps, workout_id=workout_id, started_at=started_at)],
    )


async def test_record_line_shows_both_modes_when_history_has_both():
    # Сессия 1: тяжёлая (100x5, e1RM≈116.7) — тяжелее, чем позволяет
    # округление bodyweight-логики. Сессия 2 (последняя): своим весом, 12
    # повторов.
    heavy = _session(1, "2026-01-01T12:00:00", 100.0, 5)
    bw = _session(2, "2026-02-01T12:00:00", 0.0, 12)
    sessions = [heavy, bw]
    records = analytics.compute_personal_records(sessions)

    text = formatting.format_progress_screen(
        "Подтягивания", sessions, comparison=None, records=records, limit=8, unit="kg",
    )

    assert "e1RM" in text and f"{records.max_e1rm:.1f}" in text  # тяжёлый рекорд не потерялся
    assert "Рекорд повторов в подходе: 12" in text  # и рекорд повторов тоже на месте


async def test_each_session_block_uses_its_own_mode_label():
    heavy = _session(1, "2026-01-01T12:00:00", 100.0, 5)
    bw = _session(2, "2026-02-01T12:00:00", 0.0, 12)
    sessions = [heavy, bw]
    records = analytics.compute_personal_records(sessions)

    text = formatting.format_progress_screen(
        "Подтягивания", sessions, comparison=None, records=records, limit=8, unit="kg",
    )

    # Тяжёлая сессия подписана e1RM (а не «всего повторов»), сессия своим
    # весом — «всего повторов» (а не e1RM).
    assert "всего повторов 12" in text
    assert "e1RM 116.7" in text  # 100x5 по Epley = 100*(1+5/30) = 116.666...


async def test_pure_weighted_history_is_unchanged():
    """История без bodyweight-сессий вообще не должна обзаводиться второй,
    пустой строкой рекорда."""
    a = _session(1, "2026-01-01T12:00:00", 100.0, 5)
    b = _session(2, "2026-02-01T12:00:00", 105.0, 5)
    sessions = [a, b]
    records = analytics.compute_personal_records(sessions)

    text = formatting.format_progress_screen(
        "Присед", sessions, comparison=None, records=records, limit=8, unit="kg",
    )

    assert "Рекорд повторов в сете" not in text
    assert text.count("Рекорд") == 1


# ---------- handler-level: chart points per-session, not per-last-session ----------


async def test_chart_plots_one_metric_and_skips_the_other_mode(fresh_db, user_id):
    """График остаётся про одну величину. Килограммы e1RM и голые повторы на
    одной оси читаются как обвал силы (110 и 12 рядом), а раньше сессии с весом
    вообще подписывались как «повторы». Сессии другого режима просто не
    попадают на график — оба рекорда всё равно показаны текстом выше."""
    from handlers import history

    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    ex_id = await db.create_exercise(user_id, "Подтягивания", group_id)

    heavy_workout = await db.create_finished_workout(user_id, "2026-01-01T10:00:00", "2026-01-01T10:30:00")
    block1 = await db.create_block(heavy_workout, "single")
    await db.add_block_exercise(block1, ex_id, 0)
    await db.add_set(block1, ex_id, round_index=1, order_in_round=0, weight=100.0, reps=5)

    bw_workout = await db.create_finished_workout(user_id, "2026-02-01T10:00:00", "2026-02-01T10:30:00")
    block2 = await db.create_block(bw_workout, "single")
    await db.add_block_exercise(block2, ex_id, 0)
    await db.add_set(block2, ex_id, round_index=1, order_in_round=0, weight=0.0, reps=12)

    user = await db.get_user(user_id)

    real_render = MagicMock(return_value=b"fake-png")
    with patch("charts.render_metric_over_sessions", real_render):
        await history._render_progress_view(ex_id, user, 8)

    points = real_render.call_args.args[0]
    # Последняя тренировка — своим весом, значит график про повторы, и тяжёлая
    # сессия в него не идёт: её 116.7 кг рядом с 12 повторами были бы обрывом.
    assert [p[1] for p in points] == [12.0]
    assert real_render.call_args.args[2] == "повторы"
