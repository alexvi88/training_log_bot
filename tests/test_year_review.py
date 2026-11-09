"""«Итоги года» (year_review.py): правила «личности года», какие слайды
попадают в колоду, язык и глифы того, что уезжает в PNG, сбор из базы,
GET /v1/year-review и картинка слайда, команда /year в боте и раскрытие
после импорта."""

from __future__ import annotations

import datetime as dt
import io
import re
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from PIL import Image

import api_v1
import finish_slides as fs
import i18n
import timeutil
import year_review as yr
from handlers import year_review as year_handlers

CYRILLIC = re.compile(r"[А-Яа-яЁё]")
LANGS = ("ru", "en", "es", "pt", "pt-PT", "de", "fr", "tr")


def setup_function(_):
    i18n.reload()


def _facts(**kw) -> yr.YearFacts:
    base = dict(
        year=2026, workouts=120, active_weeks=44, best_streak=6, sets=2900, tonnage=380_000.0,
        records=4, top_record={"exercise": "Bench press", "weight": 110.0, "reps": 5},
        lift=yr.Lift("Squat", 100.0, 130.0),
        groups=(yr.Group("Back", "back", 700), yr.Group("Chest", "chest", 650), yr.Group("Legs", "legs", 400)),
        weekday=0, hour=18,
    )
    base.update(kw)
    return yr.YearFacts(**base)


# ---------- «личность года»: первое правило, которое подтверждают данные ----------


@pytest.mark.parametrize(
    "kw, kind",
    [
        (dict(workouts=7), "start"),
        (dict(workouts=7, hour=6, records=40), "start"),  # короткая история главнее всего
        (dict(hour=7), "early"),
        (dict(hour=22), "night"),
        (dict(groups=(yr.Group("Legs", "legs", 900),)), "legs"),
        (dict(records=10), "records"),
        (dict(records=9, best_streak=8), "iron"),
        (dict(), "regular"),
        (dict(hour=None, weekday=None), "regular"),
    ],
)
def test_persona_rules_in_order(kw, kind):
    assert yr.persona_kind(_facts(**kw)) == kind


def test_usual_hour_needs_a_habit():
    assert yr.usual_hour([18, 18, 19]) is None  # мало тренировок
    assert yr.usual_hour([18, 18, 18, 7, 9, 12]) == 18
    assert yr.usual_hour([6, 8, 10, 12, 14, 16, 18, 20, 22, 23]) is None  # разброс — привычки нет


# ---------- колода ----------


def _deck(lang="en", **kw) -> yr.Deck:
    with i18n.use_lang(lang):
        return yr.assemble(_facts(**kw))


def test_full_deck_order():
    deck = _deck()
    kinds = [type(s).__name__ for s in deck.slides]
    assert kinds == ["StatSlide", "BarbellSlide", "StatSlide", "BodySlide", "StatSlide", "StatSlide"]
    cover, final = deck.slides[0], deck.slides[-1]
    assert cover.value == "120" and cover.chip == "Year in review"
    assert final.headline == deck.persona.title and not final.value


def test_slides_without_data_are_left_out():
    deck = _deck(tonnage=0.0, records=0, top_record=None, lift=None, groups=(), weekday=None, hour=None)
    assert [type(s).__name__ for s in deck.slides] == ["StatSlide", "StatSlide"]


def test_growth_slide_when_no_records():
    deck = _deck(records=0, top_record=None)
    growth = deck.slides[2]
    assert growth.value == "+30%" and growth.chip == "Growth this year"


def test_short_form_for_a_new_athlete():
    deck = _deck(workouts=3, lang="ru")
    assert deck.slides[0].chip == "Первые недели"
    assert deck.persona.kind == "start"


@pytest.mark.parametrize("lang", LANGS)
def test_every_language_renders_its_own_text_with_glyphs(lang):
    deck = _deck(lang=lang)
    for slide in deck.slides:
        for text in yr.slide_texts(slide, deck.signature):
            assert "&" not in text and "{" not in text, text
            assert not fs.missing_glyphs(text), (lang, text)
            if lang != "ru":
                assert not CYRILLIC.search(text), (lang, text)


def test_female_athlete_gets_her_verb():
    deck = _deck(lang="ru", female=True)
    barbell = next(s for s in deck.slides if isinstance(s, fs.BarbellSlide))
    assert barbell.lifted == "Подняла за год"


def test_render_is_a_story_frame():
    deck = _deck()
    pngs = yr.render_deck(deck)
    assert len(pngs) == len(deck.slides)
    for png in pngs:
        assert Image.open(io.BytesIO(png)).size == (fs.WIDTH, fs.HEIGHT)


# ---------- сбор из базы и /v1 ----------


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _client(db, client_factory, lang="en"):
    await db.get_or_create_user(telegram_id=111, username="tester")
    await db.set_user_lang(111, lang)
    await db.update_user(111, tz_offset=0, unit="kg")
    code = await db.issue_oauth_link_code(111, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def _exercise(db, name="Жим лёжа") -> int:
    group_id = (await db.list_muscle_groups(None, global_only=True))[0]["id"]
    return await db.create_exercise(111, name, group_id)


async def _train(db, day: dt.date, ex_id: int, weight=100.0, reps=5, sets=3, hour=18) -> int:
    start = dt.datetime.combine(day, dt.time(hour))
    workout_id = await db.create_finished_workout(
        111, start.isoformat(), (start + dt.timedelta(hours=1)).isoformat()
    )
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    for _ in range(sets):
        await db.append_set(block_id, ex_id, 0, weight, reps)
    return workout_id


async def _this_year_days(db, count: int) -> list[dt.date]:
    today = timeutil.user_today(await db.get_user(111))
    start = dt.date(today.year, 1, 1)
    days = [today - dt.timedelta(days=3 * i) for i in range(count)]
    return [d for d in days if d >= start]


async def test_collect_none_without_workouts_this_year(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    resp = await client.get("/year-review")
    assert resp.status_code == 200 and resp.json() is None


async def test_collect_counts_the_year(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    ex = await _exercise(fresh_db)
    days = await _this_year_days(fresh_db, 10)
    for index, day in enumerate(sorted(days)):
        await _train(fresh_db, day, ex, weight=100.0 + index * 2.5)
    body = (await client.get("/year-review")).json()
    assert body["workouts"] == len(days)
    assert body["sets"] == 3 * len(days)
    assert body["hour"] == 18
    assert body["slides"][0] == {"index": 0, "kind": "stat"}
    if len(days) >= yr.LIFT_MIN_SESSIONS:
        assert body["top_lift"]["growth_pct"] > 0
    # Англоязычному — без кириллицы, даже имя упражнения из каталога локализовано.
    assert not CYRILLIC.search(body["persona"]["title"] + body["persona"]["body"] + body["share_text"])

    slide = await client.get("/year-review/slides/0.png")
    assert slide.status_code == 200 and slide.headers["content-type"] == "image/png"
    # Адрес один на всех: кэш телефона не должен отдать слайд прежнего аккаунта.
    assert slide.headers["cache-control"] == "no-store"
    assert Image.open(io.BytesIO(slide.content)).size == (fs.WIDTH, fs.HEIGHT)
    assert (await client.get(f"/year-review/slides/{len(body['slides'])}.png")).status_code == 404


async def test_bad_year_is_refused_in_the_athletes_language(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory, lang="ru")
    future = dt.date.today().year + 2
    resp = await client.get(f"/year-review?year={future}")
    assert resp.status_code == 400
    assert resp.json()["message"] == i18n.t_in("ru", "api.error.year_invalid")


async def test_requires_auth(fresh_db, client_factory):
    assert (await client_factory().get("/year-review")).status_code == 401


# ---------- бот ----------


def _bot():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    bot.send_media_group = AsyncMock()
    bot.me = AsyncMock(return_value=MagicMock(username="test_bot"))
    return bot


async def test_bot_sends_album_and_share_button(fresh_db, monkeypatch):
    await fresh_db.get_or_create_user(telegram_id=111, username="tester")
    await fresh_db.update_user(111, tz_offset=0)
    ex = await _exercise(fresh_db)
    for day in await _this_year_days(fresh_db, 3):
        await _train(fresh_db, day, ex)
    monkeypatch.setattr(year_handlers.sharing, "get_bot_username", AsyncMock(return_value="test_bot"))
    bot = _bot()
    assert await year_handlers.send_year(bot, 111, 111, source="command")
    bot.send_media_group.assert_awaited_once()
    text_call = bot.send_message.await_args
    markup = text_call.kwargs["reply_markup"]
    url = markup.inline_keyboard[0][0].url
    assert url.startswith("https://t.me/share/url?url=") and "ref_111" in url


async def test_bot_empty_year_invites_but_import_stays_quiet(fresh_db):
    await fresh_db.get_or_create_user(telegram_id=111, username="tester")
    bot = _bot()
    assert not await year_handlers.send_year(bot, 111, 111, source="command")
    bot.send_message.assert_awaited_once()
    bot = _bot()
    assert not await year_handlers.send_year(bot, 111, 111, source="import", quiet_if_empty=True)
    bot.send_message.assert_not_awaited()


# ---------- рассылка в первую неделю декабря ----------

import engagement  # noqa: E402
import push_texts  # noqa: E402


@pytest.mark.parametrize(
    "day, due",
    [
        (dt.date(2026, 11, 30), False),
        (dt.date(2026, 12, 1), True),
        (dt.date(2026, 12, 7), True),
        (dt.date(2026, 12, 8), False),
    ],
)
def test_year_push_window_is_the_first_week_of_december(day, due):
    assert engagement.year_review_due(day) is due


async def test_year_push_sends_once_a_year(fresh_db, monkeypatch):
    await fresh_db.get_or_create_user(telegram_id=111, username="tester")
    await fresh_db.update_user(111, tz_offset=0)
    ex = await _exercise(fresh_db)
    december = dt.date(dt.date.today().year, 12, 2)
    for day in (december - dt.timedelta(days=d) for d in (5, 12, 20)):
        await _train(fresh_db, day, ex)
    sent = AsyncMock(return_value=4242)
    monkeypatch.setattr(year_handlers, "send_year", sent)
    user = await fresh_db.get_user(111)
    monkeypatch.setattr(timeutil, "user_today", lambda _user: december)
    with i18n.use_lang("ru"):
        assert await engagement._send_year_review_push(MagicMock(), 111, user, december)
        assert not await engagement._send_year_review_push(MagicMock(), 111, user, december + dt.timedelta(days=1))
    sent.assert_awaited_once()
    assert sent.await_args.kwargs["source"] == "push"
    assert await fresh_db.has_push_on(111, push_texts.YEAR_REVIEW, engagement.year_review_dedup_key(december.year))


async def test_year_push_skips_an_empty_year(fresh_db, monkeypatch):
    await fresh_db.get_or_create_user(telegram_id=111, username="tester")
    sent = AsyncMock(return_value=1)
    monkeypatch.setattr(year_handlers, "send_year", sent)
    december = dt.date(dt.date.today().year, 12, 2)
    with i18n.use_lang("ru"):
        assert not await engagement._send_year_review_push(MagicMock(), 111, await fresh_db.get_user(111), december)
    sent.assert_not_awaited()


@pytest.mark.parametrize("lang", LANGS)
def test_year_push_text_in_every_language(lang):
    import push_ios

    with i18n.use_lang(lang):
        title, body = push_ios.year_review_alert(2026)
    assert "2026" in body and title
    if lang != "ru":
        assert not CYRILLIC.search(title + body)
    assert push_ios.year_review_route(2026) == {"screen": "year_review", "year": 2026}
