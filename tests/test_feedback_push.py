"""iOS-пуш «Как тебе дневник?» (engagement._send_feedback_pushes): кому, когда,
что в нём написано и куда он ведёт; плюс аналитика ответа в поддержку.

Правила, которые держит этот файл:
- первый — не раньше config.FEEDBACK_PUSH_DELAY_HOURS после первой законченной
  тренировки и только тому, кто тренировался в последние два месяца;
- дальше — не чаще раза в config.FEEDBACK_PUSH_INTERVAL_DAYS и только если с
  прошлого запроса была тренировка;
- ночью не уходит: окно — от часа обычных пушей до тихих часов по местному,
  без пояса — безопасный час UTC; пропущенное окно переносится на следующее;
- только iOS (нет токена — нет пуша, в Telegram ничего), с уважением к
  тумблеру пушей и к «один пуш в день»;
- не владельцу поддержки и никогда не демо-аккаунту App Review.
"""

import datetime as dt
import json
import re

import httpx
import pytest

import api_v1
import api_v1_feedback
import apns
import config
import engagement
import i18n
import push_ios
import push_texts

USER = 111
TZ = 3  # UTC+3: местные 19:00 — это 16:00 UTC
DELAY = dt.timedelta(hours=config.FEEDBACK_PUSH_DELAY_HOURS)
INTERVAL = dt.timedelta(days=config.FEEDBACK_PUSH_INTERVAL_DAYS)


def _utc(local: str, tz: int = TZ) -> dt.datetime:
    """Местное время атлета (YYYY-MM-DDTHH:MM) → naive UTC."""
    return dt.datetime.fromisoformat(local) - dt.timedelta(hours=tz)


def _iso(moment: dt.datetime) -> str:
    return moment.isoformat(timespec="seconds")


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.setattr(config, "ENGAGEMENT_HOUR", 19)
    monkeypatch.setattr(config, "ADMIN_ID", 999)
    monkeypatch.setattr(apns, "is_configured", lambda: True)
    # Метка запуска — отдельные тесты ниже; остальным она не мешает.
    monkeypatch.setattr(config, "FEEDBACK_PUSH_LAUNCH_UTC", None)


@pytest.fixture
def sent(monkeypatch):
    calls: list[dict] = []

    async def fake_send_alert(user_id, device_token, title, body, *, category=None, route=None, **_kw):
        calls.append({
            "user_id": user_id, "token": device_token, "title": title, "body": body,
            "category": category, "route": route,
        })
        return True

    monkeypatch.setattr(apns, "send_alert", fake_send_alert)
    return calls


def _at(monkeypatch, moment: dt.datetime) -> None:
    monkeypatch.setattr(engagement, "_utc_now", lambda: moment)


async def _athlete(db, telegram_id: int = USER, *, lang: str = "ru", tz: int = TZ, token: bool = True):
    await db.get_or_create_user(telegram_id=telegram_id, username=f"u{telegram_id}")
    await db.set_user_lang(telegram_id, lang)
    await db.update_user(telegram_id, tz_offset=tz)
    if token:
        await db.register_push_token(telegram_id, "ios", f"tok{telegram_id}")
    return telegram_id


async def _workout(db, telegram_id: int, finished_utc: dt.datetime) -> None:
    started = finished_utc - dt.timedelta(hours=1)
    await db.create_finished_workout(telegram_id, _iso(started), _iso(finished_utc))


async def _events(db, name: str) -> list[dict]:
    cur = await db.conn().execute(
        "SELECT user_id, props, platform FROM analytics_events WHERE event = ?", (name,)
    )
    return [
        {"user_id": r["user_id"], "props": json.loads(r["props"]) if r["props"] else None, "platform": r["platform"]}
        for r in await cur.fetchall()
    ]


async def _run(monkeypatch, moment: dt.datetime) -> None:
    _at(monkeypatch, moment)
    await engagement._send_feedback_pushes(config.ENGAGEMENT_HOUR)


# ---------- чистые правила ----------


def test_first_push_waits_for_the_delay_after_the_first_workout():
    first = dt.datetime(2026, 10, 1, 12, 0)
    assert not engagement.feedback_push_due(first + DELAY - dt.timedelta(minutes=1), first, first, None)
    assert engagement.feedback_push_due(first + DELAY, first, first, None)


def test_no_workout_no_push():
    now = dt.datetime(2026, 10, 1, 12, 0)
    assert not engagement.feedback_push_due(now, None, None, None)


def test_first_push_skips_someone_who_disappeared():
    """Был в дневнике давно и пропал — первый запрос ему не шлём."""
    last = dt.datetime(2026, 1, 1, 12, 0)
    assert not engagement.feedback_push_due(last + INTERVAL + dt.timedelta(days=1), last, last, None)
    assert engagement.feedback_push_due(last + INTERVAL, last, last, None)


def test_repeat_needs_sixty_days_and_a_workout_since():
    asked = dt.datetime(2026, 6, 1, 16, 0)
    first = dt.datetime(2026, 5, 1, 12, 0)
    trained = asked + dt.timedelta(days=10)
    assert not engagement.feedback_push_due(asked + INTERVAL - dt.timedelta(hours=1), first, trained, asked)
    assert engagement.feedback_push_due(asked + INTERVAL, first, trained, asked)
    # Два месяца прошло, но с прошлого запроса ни одной тренировки — молчим.
    assert not engagement.feedback_push_due(asked + INTERVAL * 2, first, asked - dt.timedelta(days=1), asked)


@pytest.mark.parametrize(
    ("local_hour", "expected"),
    [(23, False), (2, False), (8, False), (9, False), (14, False), (18, False), (19, True), (21, True), (22, False)],
)
def test_send_window_is_evening_and_never_at_night(monkeypatch, local_hour, expected):
    _at(monkeypatch, _utc(f"2026-10-14T{local_hour:02d}:30"))
    assert engagement.should_send_feedback_now(TZ, 19) is expected


def test_unknown_timezone_uses_the_safe_utc_hour(monkeypatch):
    _at(monkeypatch, dt.datetime(2026, 10, 14, engagement.UNKNOWN_TZ_SEND_HOUR_UTC, 5))
    assert engagement.should_send_feedback_now(0, 19)
    _at(monkeypatch, dt.datetime(2026, 10, 14, 16, 5))
    assert not engagement.should_send_feedback_now(0, 19)


# ---------- рассылка ----------


async def test_first_push_after_first_workout_goes_to_support_route(fresh_db, monkeypatch, sent):
    db = fresh_db
    uid = await _athlete(db)
    await _workout(db, uid, _utc("2026-10-14T13:00"))

    await _run(monkeypatch, _utc("2026-10-14T19:10"))

    assert len(sent) == 1
    push = sent[0]
    assert push["category"] == push_texts.FEEDBACK_ASK == "feedback_ask"
    assert push["route"] == {"screen": "support", "topic": "feedback"}
    assert push["title"] == "КАК ТЕБЕ ДНЕВНИК?"
    assert push["body"].startswith("ПРИВЕТ АТЛЕТ! ")
    user = await db.get_user(uid)
    assert user["feedback_push_at"] == _iso(_utc("2026-10-14T19:10"))
    # Запись в pushes — на местную дату: в этот день других пушей уже не будет.
    assert await db.has_push_today(uid, "2026-10-14")
    assert await _events(db, "feedback_push_sent") == [
        {"user_id": uid, "props": {"round": "first"}, "platform": "ios"}
    ]

    # Следующий тик того же вечера и через неделю — тишина.
    await _run(monkeypatch, _utc("2026-10-14T20:10"))
    await _run(monkeypatch, _utc("2026-10-21T19:10"))
    assert len(sent) == 1


async def test_too_soon_after_the_first_workout_then_night_shifts_to_next_evening(fresh_db, monkeypatch, sent):
    db = fresh_db
    uid = await _athlete(db)
    await _workout(db, uid, _utc("2026-10-14T20:00"))

    # 21:00 — окно, но с тренировки прошёл час.
    await _run(monkeypatch, _utc("2026-10-14T21:00"))
    # 23:00 и 07:00 — три часа прошло, но ночь.
    await _run(monkeypatch, _utc("2026-10-14T23:00"))
    await _run(monkeypatch, _utc("2026-10-15T07:00"))
    # Днём до часа обычных пушей — тоже нет: сначала решается сигнальный пуш дня.
    await _run(monkeypatch, _utc("2026-10-15T13:00"))
    assert sent == []

    await _run(monkeypatch, _utc("2026-10-15T19:00"))
    assert len(sent) == 1


async def test_unknown_timezone_gets_it_at_the_safe_hour(fresh_db, monkeypatch, sent):
    db = fresh_db
    uid = await _athlete(db, tz=0)
    await _workout(db, uid, dt.datetime(2026, 10, 14, 18, 0))

    await _run(monkeypatch, dt.datetime(2026, 10, 14, 23, 0))
    await _run(monkeypatch, dt.datetime(2026, 10, 15, 3, 0))
    assert sent == []
    await _run(monkeypatch, dt.datetime(2026, 10, 15, engagement.UNKNOWN_TZ_SEND_HOUR_UTC, 0))
    assert [p["user_id"] for p in sent] == [uid]


async def test_no_ios_token_no_push(fresh_db, monkeypatch, sent):
    """Только приложение: у атлета из одного Telegram пуш не уходит никуда —
    рассылка даже не получает бота, писать в Telegram ей нечем."""
    db = fresh_db
    uid = await _athlete(db, token=False)
    await _workout(db, uid, _utc("2026-10-14T10:00"))

    await _run(monkeypatch, _utc("2026-10-14T19:00"))

    assert sent == []
    assert (await db.get_user(uid))["feedback_push_at"] is None
    assert not await db.has_push_today(uid, "2026-10-14")


async def test_no_finished_workout_no_push(fresh_db, monkeypatch, sent):
    db = fresh_db
    await _athlete(db)
    await _run(monkeypatch, _utc("2026-10-14T19:00"))
    assert sent == []


async def test_pushes_turned_off_no_push(fresh_db, monkeypatch, sent):
    db = fresh_db
    uid = await _athlete(db)
    await db.update_user(uid, pushes_enabled=0)
    await _workout(db, uid, _utc("2026-10-14T10:00"))

    await _run(monkeypatch, _utc("2026-10-14T19:00"))
    assert sent == []


async def test_not_on_a_day_with_another_push(fresh_db, monkeypatch, sent):
    db = fresh_db
    uid = await _athlete(db)
    await _workout(db, uid, _utc("2026-10-14T10:00"))
    await db.record_push(uid, push_texts.SKIP_3, "ПРИВЕТ АТЛЕТ! …", "2026-10-14")

    await _run(monkeypatch, _utc("2026-10-14T19:30"))
    assert sent == []

    await _run(monkeypatch, _utc("2026-10-15T19:30"))
    assert len(sent) == 1


async def test_admin_and_review_accounts_never_get_it(fresh_db, monkeypatch, sent):
    db = fresh_db
    admin = await _athlete(db, config.ADMIN_ID)
    await _workout(db, admin, _utc("2026-10-14T10:00"))
    # Сам id из App Store Connect — даже без привязки к review_demo.
    await _athlete(db, -1000000000)
    await _workout(db, -1000000000, _utc("2026-10-14T10:00"))
    # Демо-аккаунт по привязке: app-only, провайдер review_demo.
    demo = (await db.create_app_only_user(language_code="en"))["telegram_id"]
    assert demo != -1000000000
    await db.link_auth_identity(demo, "review_demo", "reviewer")
    await db.update_user(demo, tz_offset=TZ)
    await db.register_push_token(demo, "ios", "demotok")
    await _workout(db, demo, _utc("2026-10-14T10:00"))

    await _run(monkeypatch, _utc("2026-10-14T19:00"))
    assert sent == []


async def test_sixty_days_and_activity_survive_restart(fresh_db, monkeypatch, sent):
    """Интервал держится по колонке в users, а не по памяти процесса."""
    db = fresh_db
    uid = await _athlete(db)
    await _workout(db, uid, _utc("2026-06-01T10:00"))
    asked = _utc("2026-06-01T19:00")
    await db.mark_feedback_push_sent(uid, _iso(asked))

    # 59 дней спустя, тренировка была — рано.
    await _workout(db, uid, _utc("2026-07-20T10:00"))
    await _run(monkeypatch, asked + INTERVAL - dt.timedelta(days=1))
    assert sent == []

    await _run(monkeypatch, _utc("2026-08-01T19:00"))
    assert len(sent) == 1
    assert (await _events(db, "feedback_push_sent"))[-1]["props"] == {"round": "repeat"}


async def test_no_activity_since_last_ask_no_repeat(fresh_db, monkeypatch, sent):
    db = fresh_db
    uid = await _athlete(db)
    await _workout(db, uid, _utc("2026-06-01T10:00"))
    await db.mark_feedback_push_sent(uid, _iso(_utc("2026-06-01T19:00")))

    await _run(monkeypatch, _utc("2026-09-01T19:00"))
    assert sent == []


async def test_english_athlete_gets_english_copy(fresh_db, monkeypatch, sent):
    db = fresh_db
    uid = await _athlete(db, lang="en")
    await _workout(db, uid, _utc("2026-10-14T10:00"))

    await _run(monkeypatch, _utc("2026-10-14T19:00"))

    assert sent[0]["title"] == "HOW'S THE DIARY?"
    assert sent[0]["body"].startswith("HEY ATHLETE! ")
    assert not re.search("[А-Яа-яЁё]", sent[0]["title"] + sent[0]["body"])


async def test_rejected_by_apple_still_lands_in_inbox_once(fresh_db, monkeypatch):
    """Apple баннер не принял — вопрос всё равно лёг во «Уведомления»
    приложения (inbox.deliver): человек его увидит, поэтому считается
    заданным, и следующий тик окна второй строки не кладёт."""
    db = fresh_db
    uid = await _athlete(db)
    await _workout(db, uid, _utc("2026-10-14T10:00"))

    async def reject(*_args, **_kwargs):
        return False

    monkeypatch.setattr(apns, "send_alert", reject)
    await _run(monkeypatch, _utc("2026-10-14T19:00"))
    await _run(monkeypatch, _utc("2026-10-14T20:00"))

    assert (await db.get_user(uid))["feedback_push_at"] is not None
    assert await db.has_push_today(uid, "2026-10-14")
    rows = await db.list_inbox(uid, "ru")
    assert [r["category"] for r in rows] == [push_texts.FEEDBACK_ASK]


async def test_daily_tick_runs_the_feedback_track_after_signal_pushes(fresh_db, monkeypatch, sent):
    """В общем часовом тике: Telegram-бот не трогается (атлет app-only)."""
    db = fresh_db
    # Первый синтетический id — -1000000000, его занимает аккаунт App Review.
    await db.create_app_only_user(language_code="en")
    uid = (await db.create_app_only_user(language_code="ru"))["telegram_id"]
    assert uid not in engagement.REVIEW_ACCOUNT_IDS
    await db.update_user(uid, tz_offset=TZ)
    await db.register_push_token(uid, "ios", "apptok")
    await _workout(db, uid, _utc("2026-10-14T10:00"))
    _at(monkeypatch, _utc("2026-10-14T19:00"))

    class NoBot:
        def __getattr__(self, name):
            raise AssertionError(f"Telegram не нужен: {name}")

    await engagement._send_daily_pushes(NoBot())

    assert [p["category"] for p in sent] == [push_texts.FEEDBACK_ASK]


# ---------- тексты ----------


def test_copy_in_both_catalogs_follows_the_push_rules():
    for lang, hello in (("ru", "ПРИВЕТ АТЛЕТ! "), ("en", "HEY ATHLETE! ")):
        title = push_ios.TITLES_BY_LANG[lang][push_texts.FEEDBACK_ASK]
        bodies = push_ios.BODY_POOLS_BY_LANG[lang][push_texts.FEEDBACK_ASK]
        # Заголовок пуша — капсом (TONE_OF_VOICE: капс — приём заголовков и пушей).
        assert title == title.upper(), title
        assert len(title) <= push_ios.TITLE_LIMIT
        assert bodies
        for body in bodies:
            assert body.startswith(hello), body
            assert "АТЛЕТ," not in body and "ATHLETE," not in body
            assert len(body) <= push_ios.BODY_LIMIT
            if lang == "en":
                assert not re.search("[А-Яа-яЁё]", title + body)
                assert "please" not in body.lower()
    with i18n.use_lang("en"):
        assert i18n.t("push.ios.feedback_ask.title") == "HOW'S THE DIARY?"


# ---------- ответ в поддержку после пуша ----------


async def _api_client(db, telegram_id: int):
    token = await db.issue_api_token(telegram_id)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    client.headers["Authorization"] = f"Bearer {token}"
    return client


async def test_support_message_after_the_push_is_counted_once(fresh_db, monkeypatch):
    db = fresh_db
    uid = await _athlete(db, token=False)
    monkeypatch.setattr(api_v1_feedback, "_daily_counts", {})

    async def fake_forward(user_id, text, photo):
        return 1, None

    monkeypatch.setattr(api_v1_feedback, "_send_feedback_to_admin", fake_forward)
    client = await _api_client(db, uid)

    # Без пуша — обычная реплика, события нет.
    assert (await client.post("/support/messages", json={"text": "Привет"})).status_code == 201
    assert await _events(db, "feedback_push_replied") == []
    # Та реплика была за час до пуша (в тесте всё случается в одну секунду).
    await db.conn().execute(
        "UPDATE support_messages SET created_at = ? WHERE user_id = ?",
        (_iso(db.utc_now_naive() - dt.timedelta(hours=1)), uid),
    )
    await db.conn().commit()

    await db.mark_feedback_push_sent(uid, db.now_iso())
    assert (await client.post("/support/messages", json={"text": "Не хватает таймера"})).status_code == 201
    assert (await client.post("/support/messages", json={"text": "И ещё одно"})).status_code == 201

    events = await _events(db, "feedback_push_replied")
    assert events == [{"user_id": uid, "props": {"after": "under_1h"}, "platform": "ios"}]
    # Текст человека в событие не попадает.
    assert "таймера" not in json.dumps(events, ensure_ascii=False)


# ---------- запуск: тем, кто тренировался до него, — с 12:00 по Москве ----------

LAUNCH = dt.datetime(2026, 10, 15, 9, 0)


async def test_nothing_before_launch_then_backlog_at_noon_msk(fresh_db, monkeypatch, sent):
    db = fresh_db
    monkeypatch.setattr(config, "FEEDBACK_PUSH_LAUNCH_UTC", LAUNCH)
    await _athlete(db)
    await _workout(db, USER, LAUNCH - dt.timedelta(days=3))
    # Вечер накануне, обычное окно 19:00 местного — но до запуска ничего.
    await _run(monkeypatch, LAUNCH - dt.timedelta(hours=14))
    assert sent == []
    # 12:00 по Москве в день запуска — вопрос уходит, не дожидаясь вечера.
    await _run(monkeypatch, LAUNCH)
    assert len(sent) == 1


async def test_backlog_respects_quiet_hours(fresh_db, monkeypatch, sent):
    db = fresh_db
    monkeypatch.setattr(config, "FEEDBACK_PUSH_LAUNCH_UTC", LAUNCH)
    # UTC-7: в 09:00 UTC там 02:00 — спит; дождётся утра.
    await _athlete(db, tz=-7)
    await _workout(db, USER, LAUNCH - dt.timedelta(days=3))
    await _run(monkeypatch, LAUNCH)
    assert sent == []
    await _run(monkeypatch, LAUNCH + dt.timedelta(hours=8))
    assert len(sent) == 1


async def test_newcomer_after_launch_keeps_evening_window(fresh_db, monkeypatch, sent):
    db = fresh_db
    monkeypatch.setattr(config, "FEEDBACK_PUSH_LAUNCH_UTC", LAUNCH)
    await _athlete(db)
    first = LAUNCH + dt.timedelta(hours=1)
    await _workout(db, USER, first)
    await _run(monkeypatch, first + DELAY + dt.timedelta(minutes=1))  # 16:01 МСК — рано
    assert sent == []
    await _run(monkeypatch, dt.datetime(2026, 10, 15, 16, 0))  # 19:00 МСК
    assert len(sent) == 1
