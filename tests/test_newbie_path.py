"""Путь новичка: что видит человек в первые дни — и чего видеть не должен.

Каждый тест — найденный на проходе новичка случай, где продукт говорил то,
чего данные не подтверждают:

- «Итог недели» за неделю, когда атлета ещё не было («Неделя мимо»);
- подколка skip_3 тому, кто просто ходит по графику пн/ср/пт;
- «Суббота — твой самый продуктивный день» после одной тренировки;
- «дневник пока пустой» посреди первой тренировки и день регистрации по UTC;
- разбор AI-тренера, который не знает, что тренировка или неделя — первая.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import ai_trainer
import analytics
import api_v1
import coach_greeting
import config
import engagement
import i18n
import push_texts
import timeutil
import weekly_summary

MON = dt.date(2026, 7, 6)  # понедельник


def _day(weeks: int, weekday: int) -> dt.date:
    return MON + dt.timedelta(weeks=weeks, days=weekday)


async def _finished(db, user_id: int, day: dt.date, hour: int = 10) -> int:
    start = dt.datetime.combine(day, dt.time(hour))
    return await db.create_finished_workout(
        user_id, start.isoformat(), (start + dt.timedelta(hours=1)).isoformat()
    )


# ---------- «Итог недели»: недели до первой тренировки нет ----------


def test_default_week_is_current_when_last_week_is_before_the_first_workout():
    tuesday = _day(1, 1)
    wednesday = tuesday + dt.timedelta(days=1)
    # Первая тренировка во вторник этой недели — прошлой недели у атлета нет.
    assert weekly_summary.default_week(wednesday, tuesday) == weekly_summary.week_monday(tuesday)
    # Первая тренировка в воскресенье прошлой недели — прошлая неделя его.
    sunday_before = weekly_summary.week_monday(tuesday) - dt.timedelta(days=1)
    assert weekly_summary.default_week(wednesday, sunday_before) == MON
    # Без истории — прежнее правило.
    assert weekly_summary.default_week(wednesday) == MON


def test_week_before_history_has_no_summary():
    first = _day(1, 1)
    assert weekly_summary.before_history(MON, first) is True
    assert weekly_summary.before_history(_day(1, 0), first) is False
    assert weekly_summary.before_history(MON, None) is False


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _api_client(db, client_factory, lang="ru"):
    await db.get_or_create_user(telegram_id=111, username="tester")
    await db.set_user_lang(111, lang)
    await db.update_user(111, tz_offset=0)
    code = await db.issue_oauth_link_code(111, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_signup_on_tuesday_first_workout_default_summary_is_this_week(
    fresh_db, client_factory, monkeypatch, lang
):
    """Пришёл во вторник, потренировался, открыл «Итог недели» без `week`:
    видит свою первую неделю, а не «Неделя мимо» за прошлую, где его не было."""
    # Пояс атлета — UTC (_api_client ставит tz_offset=0), и «сегодня» берём в нём же.
    real_today = timeutil.user_today({"tz_offset": 0})
    tuesday = weekly_summary.week_monday(real_today) - dt.timedelta(days=13)
    assert tuesday.weekday() == 1
    monkeypatch.setattr(timeutil, "user_today", lambda user: tuesday + dt.timedelta(days=1))
    client = await _api_client(fresh_db, client_factory, lang)
    await _finished(fresh_db, 111, tuesday)

    body = (await client.get("/weekly-summary")).json()
    assert body is not None
    assert body["week_start"] == weekly_summary.week_monday(tuesday).isoformat()
    assert body["verdict"]["kind"] == "first"
    assert body["workouts"]["this"] == 1
    for key in ("weekly.verdict.empty.title", "weekly.verdict.empty.body.1"):
        assert i18n.t_in(lang, key) not in (body["verdict"]["title"] + body["verdict"]["body"])

    # Прошлая неделя явно — атлета в ней не было; `null` клиент прочёл бы как
    # «тренировок нет вовсе», поэтому ответ тот же, что без параметра.
    last = weekly_summary.week_monday(tuesday) - dt.timedelta(days=7)
    resp = await client.get(f"/weekly-summary?week={last.isoformat()}")
    assert resp.status_code == 200
    assert resp.json() == body
    # Явная неделя с тренировкой — та самая неделя.
    resp = await client.get(f"/weekly-summary?week={tuesday.isoformat()}")
    assert resp.json()["week_start"] == body["week_start"]


async def test_weekly_summary_is_null_only_without_any_workouts(fresh_db, client_factory):
    client = await _api_client(fresh_db, client_factory)
    week = weekly_summary.default_week(timeutil.user_today({"tz_offset": 0}))
    for path in ("/weekly-summary", f"/weekly-summary?week={week.isoformat()}"):
        resp = await client.get(path)
        assert resp.status_code == 200 and resp.json() is None


async def test_collect_returns_none_for_a_week_before_the_first_workout(fresh_db, user_id):
    await fresh_db.update_user(user_id, tz_offset=0)
    await _finished(fresh_db, user_id, _day(1, 1))
    assert await weekly_summary.collect(user_id, MON) is None
    assert await weekly_summary.collect(user_id, _day(1, 0)) is not None


# ---------- skip_3: подколка только про настоящий пропуск ----------


def _mwf(weeks: int) -> list[dt.date]:
    return [_day(w, d) for w in range(weeks) for d in (0, 2, 4)]


def test_usual_gap_follows_a_weekly_schedule():
    dates = _mwf(3)  # последняя — пятница: обычный перерыв после неё три дня
    assert engagement.usual_gap_days(dates) == 3
    dates = _mwf(3) + [_day(3, 0), _day(3, 2)]  # последняя — среда: два дня
    assert engagement.usual_gap_days(dates) == 2
    assert engagement.usual_gap_days([MON, _day(0, 2)]) is None  # мало истории


def test_skip_milestone_rules():
    mwf = _mwf(3)
    # Понедельник после пятницы — обычный перерыв, не пропуск.
    assert engagement.skip_milestone(3, mwf) is None
    # Суббота после среды — пятница пропущена.
    assert engagement.skip_milestone(3, mwf + [_day(3, 0), _day(3, 2)]) == 3
    # Два раза в неделю (вт/пт): пятница после вторника — по графику.
    tue_fri = [_day(w, d) for w in range(3) for d in (1, 4)]
    assert engagement.skip_milestone(3, tue_fri[:-1]) is None
    # Меньше пяти тренировок — первая подколка не раньше пятого дня.
    three = [MON, _day(0, 1), _day(0, 2)]
    # Порог новичка — по дням: шесть тренировок в три дня — всё ещё три дня.
    assert engagement.skip_milestone(3, three + three) is None
    assert engagement.skip_milestone(3, three) is None
    assert engagement.skip_milestone(5, three) == 5
    # Длинные вехи от графика не зависят.
    weekly = [_day(w, 0) for w in range(6)]
    assert engagement.skip_milestone(5, weekly) is None
    assert engagement.skip_milestone(7, weekly) == 7
    assert engagement.skip_milestone(14, three) == 14
    # Без истории — прежнее правило точного дня.
    assert engagement.skip_milestone(3) == 3


async def test_mon_wed_fri_monday_gets_no_skip_3_but_a_real_skip_does(fresh_db, user_id):
    for day in _mwf(3):
        await _finished(fresh_db, user_id, day)
    monday = _day(3, 0)
    decision = await engagement.build_daily_push(user_id, monday)
    assert decision is None or decision.category not in push_texts.SKIP_CATEGORY_BY_DAY.values()

    # Понедельник и среда есть, пятница пропущена — в субботу подколка уходит.
    await _finished(fresh_db, user_id, monday)
    await _finished(fresh_db, user_id, _day(3, 2))
    decision = await engagement.build_daily_push(user_id, _day(3, 5))
    assert decision is not None and decision.category == push_texts.SKIP_3


async def test_newbie_with_few_workouts_gets_first_jab_on_day_five(fresh_db, user_id):
    for day in (MON, _day(0, 2)):
        await _finished(fresh_db, user_id, day)
    assert await engagement.build_daily_push(user_id, _day(0, 5)) is None  # 3 дня
    decision = await engagement.build_daily_push(user_id, _day(1, 0))  # 5 дней
    assert decision is not None and decision.category == push_texts.SKIP_5


# ---------- «самый продуктивный день» — только при явной привычке ----------


def test_weekday_leader_needs_history_and_a_clear_lead():
    saturday = _day(0, 5)
    assert analytics.most_frequent_weekday([saturday]) is None
    assert analytics.most_frequent_weekday([saturday, _day(1, 5)]) is None
    assert analytics.most_frequent_weekday([_day(w, 5) for w in range(3)]) is None
    # Ходит только по субботам — суббота и есть его день.
    assert analytics.most_frequent_weekday([_day(w, 5) for w in range(5)]) == 5
    # Шесть тренировок, субботы явно впереди.
    dates = [_day(w, 5) for w in range(4)] + [_day(0, 1), _day(1, 3)]
    assert analytics.most_frequent_weekday(dates) == 5


async def test_sunday_static_digest_after_one_workout_claims_no_best_day(
    fresh_db, user_id, monkeypatch
):
    monkeypatch.setattr(config, "AI_WEEKLY_DIGEST_ENABLED", False)
    seen = {}

    async def pick_text(telegram_id, category, **params):
        seen[category] = params
        return "ПРИВЕТ АТЛЕТ! текст"

    monkeypatch.setattr(push_texts, "pick_text", pick_text)
    sunday = _day(0, 6)
    workout_id = await _finished(fresh_db, user_id, _day(0, 5))
    group_id = (await fresh_db.list_muscle_groups(None, global_only=True))[0]["id"]
    ex = await fresh_db.create_exercise(user_id, "Жим лёжа", group_id)
    block = await fresh_db.create_block(workout_id, "single")
    await fresh_db.add_block_exercise(block, ex, 0)
    await fresh_db.append_set(block, ex, 0, 60.0, 8)

    decision = await engagement.build_daily_push(user_id, sunday)
    assert decision is not None and decision.category == push_texts.WEEKLY_DIGEST
    assert seen[push_texts.WEEKLY_DIGEST]["best_day"] is None


# ---------- напоминание новичку ----------


async def test_newbie_nudge_skips_someone_mid_first_workout(fresh_db, user_id):
    assert [uid for uid, *_ in await fresh_db.list_newbie_user_ids()] == [user_id]
    await fresh_db.create_workout(user_id)  # status='active', начата сейчас
    assert await fresh_db.list_newbie_user_ids() == []


async def test_newbie_nudge_returns_after_an_abandoned_active_workout(fresh_db, user_id):
    """Нажал «Начать» и ушёл: брошенная активная тренировка сама не закрывается,
    и без срока он выпал бы из напоминаний навсегда."""
    stale = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
             - dt.timedelta(hours=fresh_db.NEWBIE_ACTIVE_WORKOUT_HOURS + 1))
    await fresh_db.create_workout(user_id, started_at=stale.isoformat(timespec="seconds"))
    assert [uid for uid, *_ in await fresh_db.list_newbie_user_ids()] == [user_id]


def test_signup_day_is_local():
    # 21:00 UTC у атлета с UTC+5 — это уже 02:00 следующего дня по его часам.
    assert engagement.local_signup_date("2026-07-11T21:00:00", 5) == dt.date(2026, 7, 12)
    assert engagement.local_signup_date("2026-07-11 21:00:00", 0) == dt.date(2026, 7, 11)
    assert engagement.local_signup_date("2026-07-11T01:00:00", -3) == dt.date(2026, 7, 10)


async def test_newbie_nudge_counts_days_from_local_signup(fresh_db, user_id):
    created = "2026-07-11T21:00:00"
    # По местным часам он завёлся 12-го — 12-го напоминать рано.
    assert await engagement.build_newbie_push(user_id, created, dt.date(2026, 7, 12), tz_offset=5) is None
    decision = await engagement.build_newbie_push(user_id, created, dt.date(2026, 7, 13), tz_offset=5)
    assert decision is not None and decision.category == push_texts.NEWBIE_NUDGE


# ---------- фраза после первой тренировки ----------


def test_first_workout_greeting_supports_instead_of_warning():
    assert i18n.t_in("ru", "coach_greeting.first_workout.text") == "Одна есть. Вторая закрепляет — жду тебя."
    en = i18n.t_in("en", "coach_greeting.first_workout.text")
    assert "quit" not in en and "second" in en.lower()
    assert coach_greeting.Kind  # ключ живёт в общем модуле — его же отдаёт /v1/dashboard


# ---------- AI-тренер знает, что это первая тренировка или неделя ----------


def _fake_client(text: str):
    message = SimpleNamespace(content=text, tool_calls=None)
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)
    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=response)))
    )


def _user_content(client) -> str:
    return client.chat.completions.create.await_args.kwargs["messages"][1]["content"]


def _system_content(client) -> str:
    return client.chat.completions.create.await_args.kwargs["messages"][0]["content"]


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_comment_on_first_workout_says_it_is_the_first(fresh_db, user_id, monkeypatch, lang):
    await fresh_db.set_user_lang(user_id, lang)
    client = _fake_client("ok")
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)
    first = await _finished(fresh_db, user_id, MON)

    await ai_trainer.comment_on_workout(user_id, first)
    note = i18n.t_in(lang, "ai.comment.first_workout_note")
    assert note in _user_content(client)
    assert note not in _system_content(client)  # префикс кэша не трогаем

    second = await _finished(fresh_db, user_id, _day(0, 2))
    await ai_trainer.comment_on_workout(user_id, second)
    assert note not in _user_content(client)


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_weekly_digest_of_the_first_week_says_so(fresh_db, user_id, monkeypatch, lang):
    await fresh_db.set_user_lang(user_id, lang)
    monkeypatch.setattr(config, "XAI_API_KEY", "test-key")
    client = _fake_client("ok")
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)
    sunday = _day(1, 6)
    monkeypatch.setattr(timeutil, "user_today", lambda user: sunday)
    await _finished(fresh_db, user_id, _day(1, 1))

    await ai_trainer.weekly_digest(user_id)
    note = i18n.t_in(lang, "ai.digest.first_week_note")
    summary = _user_content(client)
    assert note in summary and "на прошлой: 0" not in summary
    assert note not in _system_content(client)

    await _finished(fresh_db, user_id, _day(0, 3))
    await ai_trainer.weekly_digest(user_id)
    summary = _user_content(client)
    assert note not in summary and "на прошлой: 1" in summary


# ---------- «Новое звание: Новичок» на первой тренировке ----------


async def test_first_workout_announces_no_rank_but_the_first_real_promotion_does(fresh_db, user_id):
    import dashboard_data

    await fresh_db.update_user(user_id, tz_offset=0)
    today = timeutil.user_today(None)
    await _finished(fresh_db, user_id, today)
    user = await fresh_db.get_user(user_id)
    assert user["rank_level_seen"] == -1
    assert await dashboard_data.rank_promotion(user_id, user) is None
    assert (await fresh_db.get_user(user_id))["rank_level_seen"] == 0

    # Уровень 1 (5 тренировок, 5 т, 0.5 в неделю) — объявляется как раньше.
    group_id = (await fresh_db.list_muscle_groups(None, global_only=True))[0]["id"]
    ex = await fresh_db.create_exercise(user_id, "Жим лёжа", group_id)
    for i in range(1, 6):
        workout_id = await _finished(fresh_db, user_id, today - dt.timedelta(days=i * 3))
        block = await fresh_db.create_block(workout_id, "single")
        await fresh_db.add_block_exercise(block, ex, 0)
        for _ in range(6):
            await fresh_db.append_set(block, ex, 0, 100.0, 10)
    user = await fresh_db.get_user(user_id)
    promoted = await dashboard_data.rank_promotion(user_id, user)
    assert promoted is not None and promoted.level >= 1


async def test_no_workouts_no_rank_announcement(fresh_db, user_id):
    import dashboard_data

    await fresh_db.update_user(user_id, tz_offset=0, rank_level_seen=-1)
    assert await dashboard_data.rank_promotion(user_id, await fresh_db.get_user(user_id)) is None
