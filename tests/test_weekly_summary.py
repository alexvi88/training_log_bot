"""«Итог недели»: weekly_summary (правила вердикта, окно пн–вс, сбор) и
GET /v1/weekly-summary, плюс то, что к нему ведёт: маршрут воскресного пуша с
`week` и сохранённый разбор тренера (weekly_digests).

Даты в тестах — от настоящего «сегодня» атлета (tz_offset = 0): целевая неделя
— прошлая законченная, так что все тренировки в ней уже в прошлом.
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

import account_deletion
import api_v1
import config
import engagement
import i18n
import product_metrics
import push_ios
import push_texts
import timeutil
import weekly_summary
from weekly_summary import WeekFacts, verdict_kind

# ---------- правила вердикта: чистая функция ----------

def _facts(**kw) -> WeekFacts:
    base = dict(
        this=3, last=3, usual=3, records=0, tonnage=10_000.0, last_tonnage=10_000.0,
        is_first=False, gap_days=2,
    )
    base.update(kw)
    return WeekFacts(**base)


@pytest.mark.parametrize(
    "facts, kind",
    [
        # 1. first — вся история в этой неделе; рекорды и «обычно» не важны.
        (_facts(is_first=True, last=None, usual=None, gap_days=None, records=2), "first"),
        # 2. comeback — перерыв ≥ 14 дней перед первой тренировкой недели.
        (_facts(gap_days=14, this=1, records=1), "comeback"),
        (_facts(gap_days=13, this=3), "steady"),
        # 3. empty — ноль тренировок (даже при «обычных» трёх).
        (_facts(this=0, gap_days=None, tonnage=0.0), "empty"),
        # 4–5. рекорды: не меньше обычного — record, меньше — record_fewer.
        (_facts(records=2, this=3), "record"),
        (_facts(records=2, this=4), "record"),
        (_facts(records=1, this=2), "record_fewer"),
        # 6. short — меньше обычного без рекордов.
        (_facts(this=1), "short"),
        # 7. strong — больше обычного, или тоннаж ≥ +10% к прошлой.
        (_facts(this=4), "strong"),
        (_facts(tonnage=11_000.0), "strong"),
        (_facts(tonnage=10_999.0), "steady"),
        # 8. steady — всё остальное.
        (_facts(), "steady"),
        # Нет «обычного» (история < 3 недель) — сравниваем с прошлой неделей.
        (_facts(usual=None, last=4, this=2), "short"),
        (_facts(usual=None, last=1, this=2), "strong"),
        (_facts(usual=None, last=2, this=2, last_tonnage=0.0), "steady"),
    ],
)
def test_verdict_rules_in_order(facts, kind):
    assert verdict_kind(facts) == kind


def test_rule_order_matches_the_design():
    assert weekly_summary.KINDS == (
        "first", "comeback", "empty", "record", "record_fewer", "short", "strong", "steady",
    )


def test_usual_needs_three_weeks_and_starts_at_the_first_workout():
    monday = dt.date(2026, 9, 28)
    # Две недели истории — «обычного» ещё нет.
    dates = [monday - dt.timedelta(days=d) for d in (3, 10)]
    assert weekly_summary._usual(dates, monday) is None
    # Четыре недели по три тренировки — обычно 3, хоть окно и 8 недель: недели
    # до первой тренировки в среднее не входят.
    dates = [monday - dt.timedelta(weeks=w) + dt.timedelta(days=d) for w in range(1, 5) for d in (0, 2, 4)]
    assert weekly_summary._usual(dates, monday) == 3


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_every_kind_renders_in_both_languages(lang):
    """Каждый вид вердикта собирается на обоих языках — и при «обычном», и
    при сравнении с прошлой неделей; приветствие без запятой."""
    cases = [
        _facts(is_first=True, last=None, usual=None, gap_days=None),
        _facts(gap_days=21, this=2),
        _facts(this=0, gap_days=None, tonnage=0.0),
        _facts(records=5, this=4),
        _facts(records=2, this=2),
        _facts(this=1),
        _facts(this=1, usual=None, last=3),
        _facts(this=4, tonnage=14_200.0, last_tonnage=11_600.0),
        _facts(),
    ]
    with i18n.use_lang(lang):
        for facts in cases:
            for closed in (True, False):
                v = weekly_summary.build_verdict(facts, unit="kg", streak=5, closed=closed, seed="1:x")
                assert v.title and v.body
                assert "," not in v.greeting
                assert "{" not in v.body and "#" not in v.body


def test_phrases_claim_only_what_the_data_shows():
    with i18n.use_lang("ru"):
        # Тренировок столько же, сколько обычно, — «вместо обычных» неправда.
        v = weekly_summary.build_verdict(
            _facts(records=2, this=3), unit="kg", streak=1, closed=True, seed="s"
        )
        assert v.kind == "record"
        assert "обычных" not in v.body and "2 рекорда" in v.body
        # Больше обычного — сравнение появляется.
        v = weekly_summary.build_verdict(
            _facts(records=5, this=4), unit="kg", streak=1, closed=True, seed="s"
        )
        assert "4 тренировки вместо твоих обычных 3 и 5 рекордов" in v.body
        # Нет «обычного» — сравнение с прошлой неделей, а не с «обычно».
        v = weekly_summary.build_verdict(
            _facts(this=1, usual=None, last=3), unit="kg", streak=1, closed=True, seed="s"
        )
        assert v.kind == "short" and v.title == "1 из 3."
        assert "прошлой неделе" in v.body and "Обычно" not in v.body
        # Тоннаж вырос — прирост в тоннах, единица вплотную.
        v = weekly_summary.build_verdict(
            _facts(this=3, tonnage=14_200.0, last_tonnage=11_600.0),
            unit="kg", streak=1, closed=True, seed="s",
        )
        assert v.kind == "strong" and "14.2т" in v.body and "2.6т" in v.body


def test_empty_week_streak_line_depends_on_whether_sunday_is_over():
    facts = _facts(this=0, gap_days=None, tonnage=0.0)
    with i18n.use_lang("ru"):
        open_week = weekly_summary.build_verdict(facts, unit="kg", streak=5, closed=False, seed="s")
        closed_week = weekly_summary.build_verdict(facts, unit="kg", streak=0, closed=True, seed="s")
    assert "5 недель" in open_week.body and "до полуночи" in open_week.body
    assert "заново" in closed_week.body or "новую неделю" in closed_week.body


def test_the_same_week_reads_the_same_on_every_open():
    facts = _facts(records=1, this=3)
    with i18n.use_lang("en"):
        a = weekly_summary.build_verdict(facts, unit="kg", streak=1, closed=True, seed="111:2026-09-28")
        b = weekly_summary.build_verdict(facts, unit="kg", streak=1, closed=True, seed="111:2026-09-28")
    assert a == b


# ---------- сбор и /v1 ----------

@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _client(fresh_db, client_factory, lang="ru", unit="kg"):
    await fresh_db.get_or_create_user(telegram_id=111, username="tester")
    await fresh_db.set_user_lang(111, lang)
    await fresh_db.update_user(111, tz_offset=0, unit=unit)
    code = await fresh_db.issue_oauth_link_code(111, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def _today(db) -> dt.date:
    return timeutil.user_today(await db.get_user(111))


async def _exercise(db, name="Жим лёжа") -> int:
    group_id = (await db.list_muscle_groups(None, global_only=True))[0]["id"]
    return await db.create_exercise(111, name, group_id)


async def _train(db, day: dt.date, ex_id: int, weight=100.0, reps=5, sets=3, hour=10) -> int:
    start = dt.datetime.combine(day, dt.time(hour))
    workout_id = await db.create_finished_workout(
        111, start.isoformat(), (start + dt.timedelta(hours=1)).isoformat()
    )
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    for _ in range(sets):
        await db.append_set(block_id, ex_id, 0, weight, reps)
    return workout_id


async def test_requires_auth(fresh_db, client_factory):
    resp = await client_factory().get("/weekly-summary")
    assert resp.status_code == 401


async def test_null_without_any_workouts(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    resp = await client.get("/weekly-summary")
    assert resp.status_code == 200 and resp.json() is None


async def test_default_is_the_last_completed_week(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    ex = await _exercise(fresh_db)
    today = await _today(fresh_db)
    await _train(fresh_db, today - dt.timedelta(days=30), ex)
    body = (await client.get("/weekly-summary")).json()
    expected = weekly_summary.week_monday(today) - dt.timedelta(days=7)
    assert body["week_start"] == expected.isoformat()
    assert body["week_end"] == (expected + dt.timedelta(days=6)).isoformat()
    assert body["closed"] is True


async def test_any_day_of_the_week_maps_to_its_monday_and_future_is_refused(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory, lang="en")
    ex = await _exercise(fresh_db)
    today = await _today(fresh_db)
    await _train(fresh_db, today - dt.timedelta(days=30), ex)
    wednesday = weekly_summary.week_monday(today) - dt.timedelta(days=5)
    body = (await client.get(f"/weekly-summary?week={wednesday.isoformat()}")).json()
    assert body["week_start"] == weekly_summary.week_monday(wednesday).isoformat()
    future = (weekly_summary.week_monday(today) + dt.timedelta(days=7)).isoformat()
    resp = await client.get(f"/weekly-summary?week={future}")
    assert resp.status_code == 400
    assert resp.json()["message"] == i18n.t_in("en", "api.error.weekly_future")


async def test_empty_week(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    ex = await _exercise(fresh_db)
    today = await _today(fresh_db)
    week = weekly_summary.default_week(today)
    # Тренировки две недели подряд до целевой, а в ней — ни одной.
    await _train(fresh_db, week - dt.timedelta(days=4), ex)
    await _train(fresh_db, week - dt.timedelta(days=11), ex)
    body = (await client.get(f"/weekly-summary?week={week.isoformat()}")).json()
    assert body["verdict"]["kind"] == "empty"
    assert body["verdict"]["greeting"] == "ПРИВЕТ АТЛЕТ!"
    assert body["workouts"]["this"] == 0 and body["workouts"]["last"] == 1
    assert body["workouts"]["days"] == [False] * 7
    assert body["volume"]["rows"] == []  # объём из нулей не рисуем
    assert body["records"] == {"count": 0, "items": []}
    assert body["top_lift"] is None and body["hint"] is None
    assert body["streak_weeks"] == 0  # закрытая пустая неделя серию оборвала
    assert body["tonnage"]["this"] == 0


async def test_first_week(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    ex = await _exercise(fresh_db)
    week = weekly_summary.default_week(await _today(fresh_db))
    await _train(fresh_db, week, ex, weight=100)
    await _train(fresh_db, week + dt.timedelta(days=3), ex, weight=110)
    body = (await client.get(f"/weekly-summary?week={week.isoformat()}")).json()
    assert body["verdict"]["kind"] == "first"
    assert "2 тренировки" in body["verdict"]["body"]
    assert body["workouts"]["last"] is None and body["workouts"]["usual"] is None
    # Первая попытка движения — не рекорд, и роста не с чем сравнить.
    assert body["records"]["count"] == 0
    assert body["top_lift"] is None
    assert body["workouts"]["days"] == [True, False, False, True, False, False, False]
    assert body["volume"]["rows"][0]["sets"] == 6
    assert body["tonnage"]["label"] == "3.1т"


async def test_records_are_listed_with_best_set_and_gain(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    ex = await _exercise(fresh_db)
    week = weekly_summary.default_week(await _today(fresh_db))
    for w in range(1, 5):  # четыре недели по три тренировки — «обычно» 3
        for d in (0, 2, 4):
            await _train(fresh_db, week - dt.timedelta(weeks=w) + dt.timedelta(days=d), ex, weight=60)
    for d in (0, 2, 4, 5):
        await _train(fresh_db, week + dt.timedelta(days=d), ex, weight=60)
    await _train(fresh_db, week + dt.timedelta(days=6), ex, weight=66, reps=6, sets=1)
    # Подход после недели — не её рекорд, даже если тяжелее.
    await _train(fresh_db, week + dt.timedelta(days=8), ex, weight=90, sets=1)

    body = (await client.get(f"/weekly-summary?week={week.isoformat()}")).json()
    assert body["verdict"]["kind"] == "record"
    assert body["workouts"] == {
        "this": 5, "last": 3, "usual": 3, "days": [True, False, True, False, True, True, True],
    }
    assert body["records"]["count"] == 1
    item = body["records"]["items"][0]
    assert item["exercise_id"] == ex
    assert item["best"] == "66кг × 6"
    assert item["e1rm"] == pytest.approx(79.2)
    assert item["gain"] == "+9.2кг"  # 79.2 против 70 у 60×5
    assert "5 тренировок вместо твоих обычных 3 и 1 рекорд" in body["verdict"]["body"]


async def test_week_window_is_seven_days_not_eight(fresh_db, client_factory):
    """Воскресенье прошлой недели не входит в тоннаж этой — граница одна на все
    агрегаты (раньше дайджест брал today − 7 с `>=`, то есть восемь суток)."""
    client = await _client(fresh_db, client_factory)
    ex = await _exercise(fresh_db)
    week = weekly_summary.default_week(await _today(fresh_db))
    await _train(fresh_db, week - dt.timedelta(days=1), ex, weight=100, reps=10, sets=1)  # вс прошлой
    await _train(fresh_db, week, ex, weight=50, reps=10, sets=1)  # пн этой
    await _train(fresh_db, week + dt.timedelta(days=6), ex, weight=50, reps=10, sets=1, hour=23)  # вс этой
    await _train(fresh_db, week + dt.timedelta(days=7), ex, weight=200, reps=10, sets=1)  # пн следующей
    body = (await client.get(f"/weekly-summary?week={week.isoformat()}")).json()
    assert body["tonnage"]["this"] == 1000
    assert body["tonnage"]["last"] == 1000
    assert body["tonnage"]["delta_pct"] == 0
    assert body["workouts"]["this"] == 2


async def test_lb_tonnage_has_no_metric_tons(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory, lang="en", unit="lb")
    ex = await _exercise(fresh_db, "Bench")
    week = weekly_summary.default_week(await _today(fresh_db))
    await _train(fresh_db, week + dt.timedelta(days=1), ex, weight=235, reps=10, sets=3)
    body = (await client.get(f"/weekly-summary?week={week.isoformat()}")).json()
    assert body["tonnage"]["label"] == "7,050lb"


async def test_next_day_is_from_the_last_trained_program(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    ex = await _exercise(fresh_db)
    week = weekly_summary.default_week(await _today(fresh_db))
    program_id = await fresh_db.create_program(111, "Всё тело")
    day1 = await fresh_db.create_routine(111, "День 1", program_id=program_id)
    day2 = await fresh_db.create_routine(111, "День 2", program_id=program_id)
    await fresh_db.add_routine_exercise(day2, ex, 0)
    wid = await _train(fresh_db, week + dt.timedelta(days=1), ex)
    await fresh_db.conn().execute("UPDATE workouts SET routine_id = ? WHERE id = ?", (day1, wid))
    await fresh_db.conn().commit()
    body = (await client.get(f"/weekly-summary?week={week.isoformat()}")).json()
    assert body["next_day"] == {
        "program_id": program_id, "program_name": "Всё тело", "routine_id": day2,
        "name": "День 2", "exercises": ["Жим лёжа"],
    }


async def test_no_program_means_no_next_day(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    ex = await _exercise(fresh_db)
    week = weekly_summary.default_week(await _today(fresh_db))
    await _train(fresh_db, week, ex)
    body = (await client.get(f"/weekly-summary?week={week.isoformat()}")).json()
    assert body["next_day"] is None


async def test_coach_text_shown_in_own_language_hidden_otherwise_and_marked_stale(
    fresh_db, client_factory
):
    client = await _client(fresh_db, client_factory)
    ex = await _exercise(fresh_db)
    week = weekly_summary.default_week(await _today(fresh_db))
    await _train(fresh_db, week, ex)
    await fresh_db.save_weekly_digest(111, week.isoformat(), "ru", "ПРИВЕТ АТЛЕТ! Неделя крепкая.")
    # Разбор написан «сейчас» — после всех тренировок недели.
    body = (await client.get(f"/weekly-summary?week={week.isoformat()}")).json()
    assert body["coach_text"]["text"] == "ПРИВЕТ АТЛЕТ! Неделя крепкая."
    assert body["coach_text"]["stale"] is False
    # Тренировка недели, закрытая позже разбора, — разбор о ней не знает.
    await fresh_db.conn().execute(
        "UPDATE weekly_digests SET created_at = ? WHERE user_id = 111",
        ((dt.datetime.combine(week, dt.time(5))).isoformat(),),
    )
    await fresh_db.conn().commit()
    body = (await client.get(f"/weekly-summary?week={week.isoformat()}")).json()
    assert body["coach_text"]["stale"] is True
    # Сменил язык — чужой текст не показываем.
    await fresh_db.set_user_lang(111, "en")
    body = (await client.get(f"/weekly-summary?week={week.isoformat()}")).json()
    assert body["coach_text"] is None
    assert "," not in body["verdict"]["greeting"] and body["verdict"]["greeting"] == "HEY ATHLETE!"


async def test_hint_names_the_most_lagging_group_that_has_sets(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    ex = await _exercise(fresh_db)
    week = weekly_summary.default_week(await _today(fresh_db))
    await _train(fresh_db, week, ex, sets=5)
    body = (await client.get(f"/weekly-summary?week={week.isoformat()}")).json()
    group = body["volume"]["rows"][0]["group"]
    assert body["volume"]["rows"][0] == {"group": group, "sets": 5, "last": 0, "status": "low"}
    assert body["hint"] == f"{group}: добавь 1 подход — и будет норма."
    assert body["volume"]["min"] == 6 and body["volume"]["max"] == 12


# ---------- разбор тренера: хранение, чистка, снос ----------

async def test_sunday_ai_digest_is_stored_and_push_route_carries_the_week(
    fresh_db, user_id, monkeypatch
):
    sunday = dt.date(2026, 7, 19)
    start = "2026-07-18T10:00:00"
    wid = await fresh_db.create_finished_workout(user_id, start, start)
    block = await fresh_db.create_block(wid, "single")
    ex = await _exercise(fresh_db)
    await fresh_db.add_block_exercise(block, ex, 0)
    await fresh_db.append_set(block, ex, 0, 100.0, 8)
    monkeypatch.setattr(engagement.ai_trainer, "is_configured", lambda: True)

    async def fake_digest(uid):
        return "ПРИВЕТ АТЛЕТ! Неделя выдалась крепкой."

    monkeypatch.setattr(engagement.ai_trainer, "weekly_digest", fake_digest)
    decision = await engagement.build_daily_push(user_id, sunday)
    assert decision.category == push_texts.AI_WEEKLY
    assert decision.ios_route_params == {"week": "2026-07-13"}
    assert push_ios.ios_route(decision.category, **decision.ios_route_params) == {
        "screen": "dashboard", "week": "2026-07-13",
    }
    row = await fresh_db.get_weekly_digest(user_id, "2026-07-13")
    assert row["text"] == "ПРИВЕТ АТЛЕТ! Неделя выдалась крепкой." and row["lang"] == "ru"


async def test_static_digest_also_routes_to_its_week(fresh_db, user_id, monkeypatch):
    start = "2026-07-18T10:00:00"
    wid = await fresh_db.create_finished_workout(user_id, start, start)
    block = await fresh_db.create_block(wid, "single")
    ex = await _exercise(fresh_db)
    await fresh_db.add_block_exercise(block, ex, 0)
    await fresh_db.append_set(block, ex, 0, 100.0, 8)
    monkeypatch.setattr(config, "AI_WEEKLY_DIGEST_ENABLED", False)
    decision = await engagement.build_daily_push(user_id, dt.date(2026, 7, 19))
    assert decision.category == push_texts.WEEKLY_DIGEST
    assert decision.ios_route_params == {"week": "2026-07-13"}
    assert await fresh_db.get_weekly_digest(user_id, "2026-07-13") is None


def test_week_key_only_on_the_weekly_digest_route():
    assert push_ios.ios_route(push_texts.WEEKLY_DIGEST) == {"screen": "dashboard"}
    assert push_ios.ios_route(push_texts.AI_WEEKLY, week="2026-09-28") == {
        "screen": "dashboard", "week": "2026-09-28",
    }
    assert push_ios.ios_route(push_texts.WIN_BACK, week="2026-09-28") == {"screen": "workout"}


async def test_weekly_digests_are_pruned_and_wiped_with_the_account(fresh_db, user_id):
    old = (dt.date.today() - dt.timedelta(days=config.WEEKLY_DIGEST_RETENTION_DAYS + 7)).isoformat()
    fresh = weekly_summary.week_monday(dt.date.today()).isoformat()
    await fresh_db.save_weekly_digest(user_id, old, "ru", "старый")
    await fresh_db.save_weekly_digest(user_id, fresh, "ru", "свежий")
    assert await fresh_db.prune_old_weekly_digests(config.WEEKLY_DIGEST_RETENTION_DAYS) == 1
    assert await fresh_db.get_weekly_digest(user_id, old) is None
    assert (await fresh_db.get_weekly_digest(user_id, fresh))["text"] == "свежий"
    await account_deletion.delete_account(user_id)
    assert await fresh_db.get_weekly_digest(user_id, fresh) is None


# ---------- аналитика ----------

def test_weekly_summary_events_are_whitelisted():
    assert product_metrics.clean_props(
        "weekly_summary_opened", {"source": "push", "verdict": "record", "text": "secret"}
    ) == {"source": "push", "verdict": "record"}
    assert product_metrics.clean_props(
        "weekly_summary_action", {"action": "start", "source": "card", "verdict": "short"}
    ) == {"action": "start", "source": "card", "verdict": "short"}
