"""Фраза тренера на заставке приложения — coach_greeting.py и поле
`coach_greeting` в GET /v1/dashboard.

Выбор проверяется на синтетических фактах (каждое условие, приоритет), а
стабильность в течение дня, пояс и контракт ответа — через базу и сам API.
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

import analytics
import api_v1
import coach_greeting as cg
import db
import i18n
import timeutil
from tests.test_api_v1_language_invariant import language_violations

# Среда, 2026-09-16, полдень — не утро, не ночь, не понедельник и не пятница:
# время суток и день недели ничего не подмешивают, пока тест их не попросит.
WED = dt.datetime(2026, 9, 16, 12, 0)


def _facts(
    now: dt.datetime = WED,
    days_ago: tuple[int, ...] = (),
    *,
    dates: tuple[dt.date, ...] | None = None,
    record: bool = False,
    rank_up: int | None = None,
) -> cg.Facts:
    if dates is None:
        dates = tuple(sorted(now.date() - dt.timedelta(days=d) for d in days_ago))
    return cg.Facts(now, dates, analytics.compute_dashboard(dates, now.date()), record, rank_up)


def _kind(facts: cg.Facts, kept: str | None = None, yesterday: str | None = None) -> str | None:
    choice = cg.pick(facts, kept, yesterday)
    return choice[0].code if choice else None


# ---------- каждое условие ----------

def test_week_goal_is_the_rank_ladder_frequency():
    assert cg.WEEK_GOAL == 3


@pytest.mark.parametrize("gap,kind", [
    (3, "skip_3"), (4, "skip_3"), (5, "skip_5"), (6, "skip_5"),
    (7, "skip_7"), (13, "skip_7"), (14, "long_break"), (60, "long_break"),
])
def test_breaks(gap, kind):
    assert _kind(_facts(days_ago=(gap, gap + 3))) == kind


def test_break_needs_a_habit_before_it():
    """«А раньше ходил»: у одной-единственной тренировки — своя фраза."""
    assert _kind(_facts(days_ago=(10,))) == "first_workout"


def test_yesterday():
    assert _kind(_facts(days_ago=(1, 9))) == "yesterday"


def test_week_almost_and_closed():
    # Пн 14 и вт 15 сентября — две из трёх.
    assert _kind(_facts(days_ago=(1, 2, 30))) == "week_almost"
    assert _kind(_facts(days_ago=(0, 1, 2, 30))) == "week_closed"
    # Четвёртая на неделе — «три из трёх» уже неправда, фраза уходит.
    assert _kind(_facts(days_ago=(0, 0, 1, 2, 30))) is None


def test_streak_speaks_in_the_week_it_grew():
    weeks = (1, 7, 14, 21, 28, 35)  # шесть недель подряд, на этой — одна
    assert _kind(_facts(days_ago=weeks)) == "streak"
    choice = cg.pick(_facts(days_ago=weeks))
    assert choice[1] == {"n": 6}
    # Короче трёх недель — не серия.
    assert _kind(_facts(days_ago=(1, 7))) == "yesterday"


def test_record_rank_hundredth_only_while_fresh():
    assert _kind(_facts(days_ago=(1, 5), record=True)) == "record"
    assert _kind(_facts(days_ago=(1, 5), rank_up=4)) == "new_rank"
    # Тренировка трёхдневной давности — тренер говорит уже о пропуске.
    assert _kind(_facts(days_ago=(3, 5), record=True)) == "skip_3"
    hundred = tuple([1] + [10 + i for i in range(99)])
    assert _kind(_facts(days_ago=hundred)) == "hundredth"
    assert _kind(_facts(days_ago=hundred[:-1])) != "hundredth"


def test_first_workout():
    assert _kind(_facts(days_ago=(0,))) == "first_workout"


def test_anniversary():
    first = dt.date(2025, 9, 16)
    facts = _facts(dates=(first, WED.date() - dt.timedelta(days=1)))
    assert _kind(facts) == "anniversary"
    assert cg.pick(facts)[1] == {"years": 1}


def test_year_end_unless_trained_today():
    eve = dt.datetime(2026, 12, 31, 12, 0)
    assert _kind(_facts(eve, (2, 9))) == "year_end"
    assert _kind(_facts(eve, (0, 9))) != "year_end"


@pytest.mark.parametrize("hour,kind", [
    (4, "night"), (5, "morning"), (7, "morning"), (8, None), (21, None), (22, "night"), (23, "night"),
])
def test_time_of_day(hour, kind):
    assert _kind(_facts(WED.replace(hour=hour), (2, 10))) == kind


def test_weekdays():
    monday = dt.datetime(2026, 9, 14, 12, 0)
    friday = dt.datetime(2026, 9, 18, 12, 0)
    assert _kind(_facts(monday, (2, 9))) == "monday"
    # «А ты продолжаешь» — только если на прошлой неделе ходил.
    assert _kind(_facts(monday, (2,), dates=(dt.date(2026, 8, 1), dt.date(2026, 9, 12)))) == "monday"
    assert _kind(_facts(monday, (), dates=(dt.date(2026, 8, 1), dt.date(2026, 8, 2)))) == "long_break"
    assert _kind(_facts(friday, (2, 10))) == "friday"


def test_nothing_fits_means_title_only():
    choice = cg.pick(_facts(days_ago=(2, 10)))
    assert choice is None
    with i18n.use_lang("ru"):
        out = cg.render(choice, _facts(days_ago=(2, 10)), 0)
    assert out == {"kind": None, "title": "ПРИВЕТ АТЛЕТ!", "text": None, "until": "2026-09-17T00:00:00Z"}


# ---------- приоритет и стабильность ----------

def test_rare_events_beat_breaks_and_breaks_beat_the_clock():
    early_monday = dt.datetime(2026, 9, 14, 6, 0)
    assert _kind(_facts(early_monday, (1, 8), record=True)) == "record"
    assert _kind(_facts(early_monday, (5, 12))) == "skip_5"
    assert _kind(_facts(early_monday, (1, 8))) == "yesterday"
    assert _kind(_facts(early_monday, (2, 9))) == "morning"


def test_kept_phrase_survives_while_it_is_true():
    # Утром выбрали «понедельник»; к вечеру появился рекорд — фраза дня та же.
    evening = dt.datetime(2026, 9, 14, 19, 0)
    assert _kind(_facts(evening, (0, 7), record=True), kept="monday") == "monday"
    # Утро кончилось — «раннее утро» уже неправда, выбирается заново.
    assert _kind(_facts(dt.datetime(2026, 9, 16, 9, 0), (2, 9)), kept="morning") is None
    # «Три дня тишины», а потом тренировка — пропуска больше нет.
    assert _kind(_facts(days_ago=(0, 3, 10)), kept="skip_3") != "skip_3"


def test_yesterdays_phrase_is_not_repeated_when_another_is_true():
    friday = dt.datetime(2026, 9, 18, 12, 0)
    facts = _facts(friday, (1, 8))  # «вчера» и «пятница» — обе правда
    assert _kind(facts) == "yesterday"
    assert _kind(facts, yesterday="yesterday") == "friday"
    # Правда только она — говорим её и второй день.
    assert _kind(_facts(days_ago=(1, 8)), yesterday="yesterday") == "yesterday"


# ---------- тексты на обоих языках ----------

def _every_kind_facts() -> list[cg.Facts]:
    hundred = tuple([1] + [10 + i for i in range(99)])
    return [
        _facts(dates=(dt.date(2024, 9, 16), dt.date(2026, 9, 15))),  # 2 года
        _facts(dates=(dt.date(2025, 9, 16), dt.date(2026, 9, 15))),
        _facts(days_ago=hundred),
        _facts(days_ago=(1, 5), rank_up=4),
        _facts(days_ago=(1, 5), record=True),
        _facts(days_ago=(0,)),
        _facts(dt.datetime(2026, 12, 31, 12), (2, 9)),
        _facts(days_ago=(20, 30)), _facts(days_ago=(8, 30)), _facts(days_ago=(5, 30)),
        _facts(days_ago=(3, 30)),
        _facts(days_ago=(0, 1, 2, 30)),
        _facts(days_ago=(1, 7, 14, 21, 28, 35)),
        _facts(days_ago=(1, 7, 14, 21, 28, 35, 42, 49, 56, 63, 70, 77)),
        _facts(days_ago=(1, 2, 30)),
        _facts(days_ago=(1, 9)),
        _facts(WED.replace(hour=6), (2, 9)), _facts(WED.replace(hour=23), (2, 9)),
        _facts(dt.datetime(2026, 9, 14, 12), (2, 9)), _facts(dt.datetime(2026, 9, 18, 12), (2, 9)),
    ]


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_every_kind_renders_in_the_athletes_language(lang):
    seen = set()
    for facts in _every_kind_facts():
        choice = cg.pick(facts)
        assert choice is not None
        seen.add(choice[0].code)
        with i18n.use_lang(lang):
            out = cg.render(choice, facts, 3)
        assert out["title"] and out["text"]
        assert not language_violations({"title": out["title"], "text": out["text"]}, lang), out
        # Бренд-формула — без запятой, после АТЛЕТ только «!» или «.».
        assert "АТЛЕТ," not in out["title"] and "ATHLETE," not in out["title"]
    assert seen == {k.code for k in cg.KINDS}


def test_approved_russian_wording():
    with i18n.use_lang("ru"):
        def say(facts):
            return cg.render(cg.pick(facts), facts, 0)

        streak = say(_facts(days_ago=(1, 7, 14, 21, 28, 35)))
        assert streak["title"] == "6 НЕДЕЛЬ ПОДРЯД!"
        assert streak["text"] == "6 недель без пропуска. Это уже не настрой — режим."
        assert say(_facts(days_ago=(1, 7, 14)))["title"] == "3 НЕДЕЛИ ПОДРЯД!"
        assert say(_facts(days_ago=(1, 7, 14, 21, 28, 35, 42, 49, 56, 63, 70)))["title"] == "11 НЕДЕЛЬ ПОДРЯД!"
        assert say(_facts(days_ago=(1, 2, 30)))["text"] == "Два из трёх. Третья на этой неделе — за тобой."
        closed = say(_facts(days_ago=(0, 1, 2, 30)))
        assert (closed["title"], closed["text"]) == ("НЕДЕЛЯ ЗАКРЫТА!", "Три из трёх. Можно и отдохнуть, я разрешаю.")
        rank = say(_facts(days_ago=(1, 5), rank_up=4))
        assert rank["text"] == "Звание — Тяжеловес. Я сразу понял, что дойдёшь."
        first_rank = say(_facts(days_ago=(1, 5), rank_up=1))
        assert first_rank["text"] == "Втянулся. Я так и думал."
        hundred = say(_facts(days_ago=tuple([1] + [10 + i for i in range(99)])))
        assert (hundred["title"], hundred["text"]) == ("СОТАЯ!", "Сто тренировок. Записал золотом.")
        skip5 = say(_facts(days_ago=(5, 30)))
        assert (skip5["title"], skip5["text"]) == ("ПРИВЕТ АТЛЕТ.", "Почти неделя без зала. Я не ругаюсь. Я записываю.")
        long_break = say(_facts(days_ago=(20, 30)))
        assert (long_break["title"], long_break["text"]) == (
            "ПРИВЕТ АТЛЕТ!", "Пришёл — уже полдела. Начнём с лёгкого."
        )
        assert say(_facts(dates=(dt.date(2025, 9, 16), dt.date(2026, 9, 15))))["text"] == (
            "Год с первой записи. Торт не дам, дам штангу."
        )
        assert say(_facts(dates=(dt.date(2024, 9, 16), dt.date(2026, 9, 15))))["text"].startswith("2 года ")


def test_until_is_the_end_of_the_window_in_utc():
    with i18n.use_lang("en"):
        morning = _facts(WED.replace(hour=6), (2, 9))
        assert cg.render(cg.pick(morning), morning, 3)["until"] == "2026-09-16T05:00:00Z"
        late = _facts(WED.replace(hour=2), (2, 9))
        assert cg.render(cg.pick(late), late, -5)["until"] == "2026-09-16T10:00:00Z"
        day = _facts(days_ago=(1, 9))
        assert cg.render(cg.pick(day), day, 3)["until"] == "2026-09-16T21:00:00Z"


# ---------- база и API ----------

def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")


async def _linked(fresh_db, telegram_id=111, lang="ru", tz=0):
    _EXERCISE.clear()
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    await fresh_db.set_user_lang(telegram_id, lang)
    await fresh_db.update_user(telegram_id, tz_offset=tz)
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = _client()
    resp = await client.post("/auth/link", json={"code": code})
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


_EXERCISE: dict[int, int] = {}


async def _train_at(user_id: int, utc: dt.datetime, weight: float = 100.0) -> None:
    group_id = (await db.list_muscle_groups(None, global_only=True))[0]["id"]
    ex_id = _EXERCISE.get(user_id)
    if ex_id is None:
        ex_id = _EXERCISE[user_id] = await db.create_exercise(user_id, "Bench Press", group_id)
    wid = await db.create_finished_workout(
        user_id, utc.isoformat(), (utc + dt.timedelta(hours=1)).isoformat()
    )
    block_id = await db.create_block(wid, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.append_set(block_id, ex_id, 0, weight, 5)


def _freeze(monkeypatch, utc_now: dt.datetime):
    def user_now(user):
        return utc_now + dt.timedelta(hours=timeutil.offset_hours(user))

    monkeypatch.setattr(timeutil, "user_now", user_now)


@pytest.mark.asyncio
async def test_dashboard_carries_the_greeting(fresh_db, monkeypatch):
    now = dt.datetime(2026, 9, 16, 12, 0)
    _freeze(monkeypatch, now)
    client = await _linked(fresh_db, lang="en")
    await _train_at(111, now - dt.timedelta(days=1))
    await _train_at(111, now - dt.timedelta(days=9))

    body = (await client.get("/dashboard")).json()
    assert body["coach_greeting"] == {
        "kind": "yesterday",
        "title": "HEY ATHLETE!",
        "text": "You put in work yesterday. Rest today counts too.",
        "until": "2026-09-17T00:00:00Z",
    }
    # Прежние ключи на месте — старые клиенты не ломаются.
    assert {"headline", "rank", "tiles", "volume", "lifts"} <= set(body)


@pytest.mark.asyncio
async def test_one_phrase_per_day_and_a_new_one_tomorrow(fresh_db, monkeypatch):
    now = dt.datetime(2026, 9, 14, 6, 30)  # понедельник, раннее утро
    _freeze(monkeypatch, now)
    client = await _linked(fresh_db)
    await _train_at(111, now - dt.timedelta(days=2))
    await _train_at(111, now - dt.timedelta(days=9))

    first = (await client.get("/dashboard")).json()["coach_greeting"]
    assert first["kind"] == "morning"
    # 7:50 — всё ещё утро, фраза та же.
    _freeze(monkeypatch, now.replace(hour=7, minute=50))
    assert (await client.get("/dashboard")).json()["coach_greeting"]["kind"] == "morning"
    # Утро кончилось — выбрал заново, и дальше день держится на «понедельнике»,
    # хотя вечером «ночная смена» подошла бы.
    _freeze(monkeypatch, now.replace(hour=9))
    assert (await client.get("/dashboard")).json()["coach_greeting"]["kind"] == "monday"
    _freeze(monkeypatch, now.replace(hour=23))
    assert (await client.get("/dashboard")).json()["coach_greeting"]["kind"] == "monday"
    user = await db.get_user(111)
    assert (user["coach_greeting_day"], user["coach_greeting_kind"]) == ("2026-09-14", "monday")
    # Назавтра — новый день, новый выбор: третий день без зала.
    _freeze(monkeypatch, dt.datetime(2026, 9, 15, 23, 30))
    assert (await client.get("/dashboard")).json()["coach_greeting"]["kind"] == "skip_3"


@pytest.mark.asyncio
async def test_time_and_day_are_the_athletes_own(fresh_db, monkeypatch):
    # 20:00 UTC в воскресенье — у атлета с UTC+5 уже час ночи понедельника.
    now = dt.datetime(2026, 9, 13, 20, 0)
    _freeze(monkeypatch, now)
    client = await _linked(fresh_db, tz=5)
    await _train_at(111, now - dt.timedelta(days=3, hours=-2))
    await _train_at(111, now - dt.timedelta(days=4))
    greeting = (await client.get("/dashboard")).json()["coach_greeting"]
    # Местно: последняя тренировка — в чт 10-го, сегодня пн 14-го → 4 дня.
    assert greeting["kind"] == "skip_3"
    assert greeting["text"] == "Три дня тишины. Штанга уже спрашивала, где ты."
    assert greeting["until"] == "2026-09-14T19:00:00Z"


@pytest.mark.asyncio
async def test_new_rank_and_record_from_the_last_workout(fresh_db, monkeypatch):
    now = dt.datetime(2026, 9, 16, 12, 0)
    _freeze(monkeypatch, now)
    client = await _linked(fresh_db)
    # Четыре тренировки за две недели, пятая — вчера и с рекордом: звание 1
    # требует 5 тренировок, 5 т и 0.5 в неделю.
    for d in (12, 9, 6, 3):
        await _train_at(111, now - dt.timedelta(days=d), weight=600.0)
    await _train_at(111, now - dt.timedelta(days=1), weight=650.0)
    greeting = (await client.get("/dashboard")).json()["coach_greeting"]
    assert greeting["kind"] == "new_rank"
    assert greeting["text"] == "Втянулся. Я так и думал."

    record, rank_up = await cg._last_workout_events(
        111, await db.get_user(111),
        [dt.date.fromisoformat(d) for d in await db.list_finished_workout_dates(111)], 0, (),
    )
    assert record is True and rank_up == 1


@pytest.mark.asyncio
async def test_no_workouts_keeps_dashboard_null(fresh_db):
    client = await _linked(fresh_db)
    assert (await client.get("/dashboard")).json() is None
