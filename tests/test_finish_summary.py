"""Главный факт тренировки на листе итогов — `rewards.summary` в ответе finish
(finish_summary.py, разбор UI A-04).

Правило одно: заголовок — первый по списку факт, который подтверждают данные.
Тесты держат и порядок фактов, и то, что утверждений без данных нет.
"""

import datetime as dt
import re

import httpx
import pytest

import api_v1
import finish_summary
import i18n
from formatting import ExerciseBlockView


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _linked_client(fresh_db, client_factory, telegram_id=111):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def _past(db, user_id: int, exercise_id: int, when: dt.date, sets):
    """Законченная тренировка в прошлом с тем же упражнением — «прошлый раз»."""
    workout_id = await db.create_workout(user_id, started_at=f"{when.isoformat()}T10:00:00")
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, exercise_id, 0)
    for weight, reps in sets:
        await db.append_set(block_id, exercise_id, 0, weight, reps)
    await db.finish_workout(workout_id, finished_at=f"{when.isoformat()}T11:00:00")


async def _finish_today(client, exercise_id: int, sets) -> dict:
    workout_id = (await client.post("/workouts/active")).json()["id"]
    for weight, reps in sets:
        resp = await client.post(
            f"/workouts/{workout_id}/sets",
            json={"exercise_id": exercise_id, "weight": weight, "reps": reps},
        )
        assert resp.status_code in (200, 201), resp.text
    resp = await client.post(f"/workouts/{workout_id}/finish", json={})
    assert resp.status_code == 200, resp.text
    return resp.json()["rewards"]["summary"]


def _block(sets, prev=None, **kw) -> ExerciseBlockView:
    return ExerciseBlockView(
        group_name="грудь", exercise_name="Жим лёжа", sets=sets, prev_sets=prev, exercise_id=1, **kw
    )


def _head(blocks, **kw):
    args = dict(
        unit="kg", show_extra=True, total_finished=5, gap_days=2,
        this_week_count=2, week_streak=3, backfill=False,
    )
    args.update(kw)
    with i18n.use_lang("ru"):
        return finish_summary.headline(blocks, **args)


# ---------- порядок фактов ----------


def test_record_beats_everything():
    head = _head([_block([(75.0, 5)], prev=[(70.0, 5)], record_e1rm_delta=4.0)])
    assert head["kind"] == "record"
    assert head["title"] == "НОВЫЙ РЕКОРД. 75 НА 5"
    assert head["kicker"] == "ЖИМ ЛЁЖА"


def test_e1rm_record_stays_silent_without_extra_stats():
    """Рекорд e1RM при выключенных доп. цифрах молчит, как в карточке бота —
    тогда главный факт уже прибавка к прошлому разу."""
    head = _head(
        [_block([(75.0, 5)], prev=[(70.0, 5)], record_e1rm_delta=4.0)], show_extra=False
    )
    assert head["kind"] == "gain"
    assert head["title"] == "+5КГ К ПРОШЛОМУ РАЗУ"
    assert "75×5" in head["text"] and "70×5" in head["text"]


def test_more_reps_at_same_weight_is_a_gain():
    head = _head([_block([(60.0, 10), (60.0, 8)], prev=[(60.0, 8), (60.0, 8)])])
    assert head["title"] == "+2 ПОВТОРА К ПРОШЛОМУ РАЗУ"


def test_heavier_but_fewer_reps_is_not_called_a_gain():
    """100×3 против 95×8 — не «прибавка»: без расчёта это не доказать."""
    head = _head([_block([(100.0, 3)], prev=[(95.0, 8)])])
    assert head["kind"] == "plain"


def test_streak_when_this_workout_opens_the_week():
    head = _head([_block([(60.0, 10)], prev=[(60.0, 10)])], this_week_count=1, week_streak=6)
    assert head["kind"] == "streak"
    assert head["title"] == "6 НЕДЕЛЬ ПОДРЯД"


def test_comeback_after_a_week_off_is_support_only():
    head = _head([_block([(50.0, 10)], prev=[(60.0, 10)])], gap_days=12, week_streak=1)
    assert head["kind"] == "comeback"
    assert "12 дней" in head["text"]


def test_backfill_never_claims_streak_or_comeback():
    head = _head(
        [_block([(50.0, 10)], prev=[(60.0, 10)])],
        gap_days=30, this_week_count=1, week_streak=6, backfill=True,
    )
    assert head["kind"] == "plain"


def test_first_workout_ever():
    head = _head([_block([(60.0, 10)])], total_finished=1, gap_days=None)
    assert head["kind"] == "first"
    assert head["title"] == "ПЕРВАЯ ЗАПИСАНА"


def test_lighter_than_last_time_gets_support_not_a_jab():
    head = _head([_block([(60.0, 10), (60.0, 7)], prev=[(67.5, 8), (67.5, 6)])])
    assert head["kind"] == "plain"
    assert head["title"] == "60 НА 10. ЕСТЬ"
    assert "нормально" in head["text"]


def test_main_exercise_is_the_one_with_most_sets():
    small = ExerciseBlockView(
        group_name="спина", exercise_name="Тяга", sets=[(80.0, 5)], prev_sets=[(80.0, 5)], exercise_id=2
    )
    big = _block([(60.0, 10)] * 3, prev=[(60.0, 10)] * 3)
    head = _head([small, big])
    assert head["kicker"] == "ЖИМ ЛЁЖА"


def test_better_sets_are_strictly_better_only():
    block = _block([(60.0, 10), (60.0, 7), (65.0, 5)], prev=[(60.0, 8), (60.0, 7), (60.0, 6)])
    assert finish_summary.better_set_indexes(block) == [0]


def test_week_strip_marks_trained_days():
    today = dt.date(2026, 10, 1)  # четверг
    with i18n.use_lang("ru"):
        week = finish_summary.week_strip(
            [dt.date(2026, 9, 28), dt.date(2026, 10, 1), dt.date(2026, 9, 20)], today, 6
        )
    assert [d["label"] for d in week["days"]] == ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    assert [d["done"] for d in week["days"]] == [True, False, False, True, False, False, False]
    assert week["days"][3]["today"] is True
    assert week["text"] == "2 тренировки на этой неделе · 6 недель подряд"


# ---------- через /v1 ----------


@pytest.mark.asyncio
async def test_finish_carries_summary_with_gain_against_last_time(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    bench = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    await _past(fresh_db, 111, bench, dt.date.today() - dt.timedelta(days=3), [(60.0, 5), (60.0, 5)])

    summary = await _finish_today(client, bench, [(65.0, 5), (65.0, 5)])
    # Рекорд e1RM старше прибавки: 65×5 бьёт всё, что было.
    assert summary["kind"] == "record", summary
    assert summary["title"] == "НОВЫЙ РЕКОРД. 65 НА 5"
    assert summary["better_sets"] == [{"exercise_id": bench, "indexes": [0, 1]}]
    assert len(summary["week"]["days"]) == 7
    assert any(d["today"] and d["done"] for d in summary["week"]["days"])


@pytest.mark.asyncio
async def test_first_finish_is_first(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    bench = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    summary = await _finish_today(client, bench, [(60.0, 10)])
    assert summary["kind"] == "first"
    assert summary["better_sets"] == []


@pytest.mark.asyncio
async def test_summary_speaks_english_to_an_english_athlete(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    await fresh_db.set_user_lang(111, "en")
    bench = (await client.post("/exercises", json={"name": "Bench press"})).json()["id"]
    await _past(fresh_db, 111, bench, dt.date.today() - dt.timedelta(days=12), [(70.0, 8)])
    summary = await _finish_today(client, bench, [(60.0, 10), (60.0, 7)])
    texts = [summary["kicker"], summary["title"], summary["text"], summary["week"]["text"]]
    texts += [d["label"] for d in summary["week"]["days"]]
    if summary["next_target"]:
        texts.append(summary["next_target"])
    assert not any(re.search("[а-яёА-ЯЁ]", t or "") for t in texts), texts
    assert summary["kind"] == "comeback"
