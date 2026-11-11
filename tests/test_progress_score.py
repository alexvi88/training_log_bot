"""Оценка прогресса по неделям: формула (progress_score — чистые функции над
строками подходов) и GET /v1/progress/score."""

from __future__ import annotations

import datetime as dt
import re

import httpx
import pytest

import api_v1
import progress_score as ps
import timeutil

TODAY = dt.date(2026, 11, 11)  # среда
LIFTS = {
    1: ("Присед со штангой", "Ноги", 100.0),
    2: ("Жим штанги лёжа", "Грудь", 80.0),
    3: ("Тяга штанги в наклоне", "Спина", 70.0),
}


def athlete(
    *,
    weeks=20,
    per_week=3,
    monthly_pct=2.0,
    sets=4,
    skip_weeks=(),
    deload_weeks=(),
    lifts=LIFTS,
    today=TODAY,
    first_offset_weeks=None,
):
    """Атлет, который тренируется `per_week` раз в неделю (пн/ср/пт) и
    прибавляет `monthly_pct` процентов e1RM в месяц по всем движениям.
    `skip_weeks`/`deload_weeks` — номера недель от текущей (0 — текущая)."""
    rows = []
    this_monday = ps.week_monday(today)
    wid = 0
    start_week = weeks if first_offset_weeks is None else first_offset_weeks
    first_day = this_monday - dt.timedelta(weeks=start_week)
    for back in range(start_week, -1, -1):
        if back in skip_weeks:
            continue
        monday = this_monday - dt.timedelta(weeks=back)
        for k in range(per_week):
            day = monday + dt.timedelta(days=(0, 2, 4, 1, 3, 5)[k])
            if day > today:
                continue
            wid += 1
            months = (day - first_day).days / ps.DAYS_PER_MONTH
            for ex, (name, group, base) in lifts.items():
                # e1RM Эпли на 5 повторах: w·(1+5/30) — растим вес.
                weight = round(base * (1 + monthly_pct / 100) ** months, 1)
                n = 2 if back in deload_weeks else sets
                for _ in range(n):
                    rows.append(ps.ScoredSet(wid, day, ex, name, name, group, weight, weight, 5))
    return rows


def score_of(rows, **kw):
    weeks = ps.score_weeks(rows, kw.pop("today", TODAY), **kw)
    return weeks


def last_finished(weeks):
    return [w for w in weeks if not w.provisional][-1]


# ---------- кирпичи ----------

def test_theil_sen_ignores_one_wild_point():
    pts = [(0, 100), (7, 101), (14, 102), (21, 140), (28, 104), (35, 105)]
    assert ps.theil_sen_slope(pts) == pytest.approx(1 / 7, rel=0.05)


def test_expected_growth_slows_with_training_age():
    assert ps.expected_monthly_pct(0) == pytest.approx(6.0)
    assert ps.expected_monthly_pct(12) < ps.expected_monthly_pct(6) < ps.expected_monthly_pct(1)
    assert ps.expected_monthly_pct(60) == pytest.approx(1.0, abs=0.01)


@pytest.mark.parametrize("score, tier", [
    (0.0, "warmup"), (3.9, "warmup"), (4.0, "in_game"), (6.0, "in_shape"),
    (8.0, "machine"), (9.5, "legend"), (10.0, "legend"), (None, None),
])
def test_tiers(score, tier):
    assert ps.tier_for(score) == tier


# ---------- формула ----------

def test_steady_athlete_lands_in_shape():
    w = last_finished(score_of(athlete(monthly_pct=3.0)))
    assert w.score is not None and 6.0 <= w.score <= 9.5, w
    assert {p.key for p in w.parts} == {"strength", "consistency", "overload", "volume"}
    assert w.score == pytest.approx(round(sum(p.points for p in w.parts), 1), abs=0.11)


def test_faster_growth_scores_higher():
    slow = last_finished(score_of(athlete(monthly_pct=0.0))).score
    fast = last_finished(score_of(athlete(monthly_pct=4.0))).score
    assert fast > slow + 1.5


def test_same_growth_counts_more_for_a_veteran():
    """+1% в месяц — норма опытного и мало для новичка: сила у ветерана выше."""
    veteran = athlete(monthly_pct=1.0, weeks=20)
    vet_first = ps.week_monday(TODAY) - dt.timedelta(weeks=160)
    vet = last_finished(ps.score_weeks(veteran, TODAY, first_day=vet_first))
    newbie = last_finished(score_of(athlete(monthly_pct=1.0, weeks=8)))
    strength = {w: next(p for p in x.parts if p.key == "strength").share for w, x in (("v", vet), ("n", newbie))}
    assert strength["v"] > strength["n"] + 0.2


def test_week_without_workouts_is_low_and_says_come_back():
    weeks = score_of(athlete(skip_weeks=(1,)))
    skipped = weeks[-2]
    assert skipped.sessions == 0
    assert skipped.score is not None and skipped.score < weeks[-3].score - 2
    assert skipped.lever is not None and skipped.lever.kind == "comeback"


def test_too_little_data_gives_no_score():
    weeks = score_of(athlete(weeks=1))
    assert weeks and all(w.score is None for w in weeks)


def test_weeks_before_history_are_not_returned():
    weeks = score_of(athlete(weeks=3))
    assert len(weeks) == 4  # три полные недели + текущая
    assert weeks[0].week_start == ps.week_monday(TODAY) - dt.timedelta(weeks=3)


def test_typo_does_not_inflate_strength():
    rows = athlete(monthly_pct=0.0)
    clean = last_finished(score_of(rows)).score
    # «800» вместо «80» в одном подходе две недели назад.
    victim = next(r for r in rows if r.exercise_id == 2 and r.day == ps.week_monday(TODAY) - dt.timedelta(weeks=2))
    rows = [r for r in rows if r is not victim] + [
        ps.ScoredSet(victim.workout_id, victim.day, 2, victim.name, victim.identity, victim.group, 800, 800, 5)
    ]
    assert last_finished(score_of(rows)).score == pytest.approx(clean, abs=0.6)


def test_deload_week_is_not_punished():
    weeks = score_of(athlete(deload_weeks=(1,)))
    deload = weeks[-2]
    assert deload.deload
    assert deload.score >= weeks[-3].score - 1.0


def test_current_week_is_provisional_and_paced():
    """В среду две тренировки (пн, ср) при обычных трёх — регулярность не
    проседает: неделя ещё идёт."""
    weeks = score_of(athlete())
    cur = weeks[-1]
    assert cur.provisional and cur.sessions == 2
    consistency = next(p for p in cur.parts if p.key == "consistency")
    assert consistency.share >= 0.9


def test_fewer_sessions_than_usual_points_at_sessions():
    rows = athlete(per_week=4)
    last_monday = ps.week_monday(TODAY) - dt.timedelta(weeks=1)
    week_days = sorted({r.day for r in rows if last_monday <= r.day < last_monday + dt.timedelta(days=7)})
    rows = [r for r in rows if r.day not in week_days[1:]]
    w = last_finished(score_of(rows))
    assert w.sessions == 1
    assert w.lever is not None and w.lever.kind == "sessions" and w.lever.params["n"] >= 2


def test_skipped_legs_cost_volume():
    upper = {k: v for k, v in LIFTS.items() if v[1] != "Ноги"}
    full = last_finished(score_of(athlete(monthly_pct=2.0)))
    no_legs = last_finished(score_of(athlete(monthly_pct=2.0, lifts=upper)))
    vol = {n: next(p for p in w.parts if p.key == "volume").share for n, w in (("f", full), ("u", no_legs))}
    assert vol["u"] < vol["f"]


def test_bodyweight_reps_count_as_strength():
    rows = []
    monday = ps.week_monday(TODAY)
    wid = 0
    for back in range(10, 0, -1):
        for k in range(3):
            wid += 1
            day = monday - dt.timedelta(weeks=back) + dt.timedelta(days=2 * k)
            reps = 5 + (10 - back)  # +1 повтор в неделю
            for _ in range(4):
                rows.append(ps.ScoredSet(wid, day, 9, "Подтягивания", "Подтягивания", "Спина", 80.0, 0.0, reps))
    w = last_finished(ps.score_weeks(rows, TODAY))
    assert w.score is not None
    assert next(p for p in w.parts if p.key == "strength").share > 0.8


# ---------- /v1/progress/score ----------

@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _client(fresh_db, client_factory, lang):
    await fresh_db.get_or_create_user(telegram_id=111, username="tester")
    await fresh_db.set_user_lang(111, lang)
    await fresh_db.update_user(111, tz_offset=0)
    code = await fresh_db.issue_oauth_link_code(111, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def _seed(db, today: dt.date) -> None:
    groups = {g["name"]: g["id"] for g in await db.list_muscle_groups(None, global_only=True)}
    ids = {}
    for ex, (name, group, _) in LIFTS.items():
        ids[ex] = await db.create_exercise(111, name, groups.get(group))
    for r in athlete(today=today, weeks=10, monthly_pct=3.0):
        if r.day >= today:
            continue
        key = (r.workout_id,)
        if key not in _seed.workouts:
            start = dt.datetime.combine(r.day, dt.time(10))
            _seed.workouts[key] = await db.create_finished_workout(
                111, start.isoformat(), (start + dt.timedelta(hours=1)).isoformat()
            )
        wid = _seed.workouts[key]
        block = _seed.blocks.get((wid, r.exercise_id))
        if block is None:
            block = _seed.blocks[(wid, r.exercise_id)] = await db.create_block(wid, "single")
            await db.add_block_exercise(block, ids[r.exercise_id], 0)
        await db.append_set(block, ids[r.exercise_id], 0, r.weight, r.reps)


async def test_api_requires_auth(fresh_db, client_factory):
    resp = await client_factory().get("/progress/score")
    assert resp.status_code == 401


async def test_api_empty_without_workouts(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory, "ru")
    body = (await client.get("/progress/score")).json()
    assert body == {"weeks": [], "current": None, "last": None, "delta": None, "best": None}


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_api_scores_and_speaks_the_athletes_language(fresh_db, client_factory, lang):
    client = await _client(fresh_db, client_factory, lang)
    _seed.workouts, _seed.blocks = {}, {}
    today = timeutil.user_today(await fresh_db.get_user(111))
    await _seed(fresh_db, today)
    resp = await client.get("/progress/score?weeks=8")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["weeks"]) == 8
    graded = [w for w in body["weeks"] if w["score"] is not None]
    assert graded and body["last"] is not None and body["best"] >= body["last"]
    week = graded[-1]
    assert week["tier_title"] and len(week["parts"]) == 4
    assert all(p["title"] and p["max"] > 0 for p in week["parts"])
    texts = " ".join(
        str(v) for w in body["weeks"]
        for v in (w["tier_title"], w["note"], w["lever"], *(p["title"] for p in w["parts"]))
        if v
    )
    has_cyrillic = bool(re.search("[а-яА-ЯёЁ]", texts))
    assert has_cyrillic == (lang == "ru"), texts


async def test_api_rejects_bad_weeks(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory, "en")
    for raw in ("0", "27", "abc"):
        resp = await client.get(f"/progress/score?weeks={raw}")
        assert resp.status_code == 400
        assert not re.search("[а-яА-Я]", resp.json()["message"])


def test_too_much_volume_says_cut_not_add():
    w = last_finished(score_of(athlete(per_week=5, sets=6)))
    vol = next(p for p in w.parts if p.key == "volume")
    assert vol.share < 1
    assert w.lever is not None and w.lever.kind == "volume_high"
    assert w.lever.params["n"] > ps.VOLUME_FULL_MAX
