"""iOS-пуш о вехе «смотри, как ты вырос» (engagement._send_you_video_pushes):
неделя, месяц, 3 месяца, полгода, год с первой законченной тренировки — банер
ведёт в видео «как ты рос».

Правила, которые держит этот файл:
- веха — по первому дню видео (с перенесёнными тренировками), в окне
  YOU_VIDEO_GRACE_DAYS после неё; каждая — один раз;
- к дню вехи — не меньше порога тренировок; свежая тренировка, записанная у
  нас, — не старше YOU_VIDEO_ACTIVE_DAYS;
- веха, «перешагнутая» переносом истории, молчит: она должна наступить не
  раньше YOU_VIDEO_IMPORT_SETTLE_DAYS после импорта;
- только iOS: нет токена — нет пуша, в Telegram ничего;
- текст в роде атлета, без пола — нейтрально; оба каталога.
"""

import datetime as dt

import pytest

import apns
import config
import engagement
import i18n
import push_ios
import push_texts

USER = 222
TZ = 3


def _utc(local: str, tz: int = TZ) -> dt.datetime:
    return dt.datetime.fromisoformat(local) - dt.timedelta(hours=tz)


def _d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def _days(first: str, n: int, step: int = 1) -> list[dt.date]:
    start = _d(first)
    return [start + dt.timedelta(days=i * step) for i in range(n)]


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.setattr(config, "ENGAGEMENT_HOUR", 19)
    monkeypatch.setattr(apns, "is_configured", lambda: True)


@pytest.fixture
def sent(monkeypatch):
    calls: list[dict] = []

    async def fake_send_alert(user_id, device_token, title, body, *, category=None, route=None, **_kw):
        calls.append({"user_id": user_id, "title": title, "body": body, "category": category, "route": route})
        return True

    monkeypatch.setattr(apns, "send_alert", fake_send_alert)
    return calls


async def _athlete(db, telegram_id=USER, *, lang="ru", gender=None, token=True):
    await db.get_or_create_user(telegram_id=telegram_id, username=f"u{telegram_id}")
    await db.set_user_lang(telegram_id, lang)
    await db.update_user(telegram_id, tz_offset=TZ)
    if gender:
        await db.set_user_gender(telegram_id, gender)
    if token:
        await db.register_push_token(telegram_id, "ios", f"tok{telegram_id}")
    return telegram_id


async def _workout(db, telegram_id, local_day: str, *, import_batch_id=None):
    start = _utc(f"{local_day}T12:00")
    await db.create_finished_workout(
        telegram_id, start.isoformat(timespec="seconds"),
        (start + dt.timedelta(hours=1)).isoformat(timespec="seconds"),
        source="import" if import_batch_id else "manual", import_batch_id=import_batch_id,
    )


async def _run(monkeypatch, local: str):
    monkeypatch.setattr(engagement, "_utc_now", lambda: _utc(local))
    await engagement._send_you_video_pushes(config.ENGAGEMENT_HOUR)


# ---------- чистое правило ----------


def test_each_milestone_fires_inside_its_window_only():
    first = "2026-01-01"
    dates = _days(first, 200, step=2)  # через день — порог любой вехи закрыт
    for days, _need in engagement.YOU_VIDEO_MILESTONES:
        day = _d(first) + dt.timedelta(days=days)
        recent = [d for d in dates if d <= day] or dates
        last = max(recent)
        kw = dict(last_in_app=last, last_import=None)
        assert engagement.you_video_milestone(recent, day - dt.timedelta(days=1), **kw) != days
        assert engagement.you_video_milestone(recent, day, **kw) == days
        late = day + dt.timedelta(days=engagement.YOU_VIDEO_GRACE_DAYS)
        assert engagement.you_video_milestone(recent, late, **kw) == days
        assert engagement.you_video_milestone(recent, late + dt.timedelta(days=1), **kw) is None


def test_milestone_goes_once():
    dates = _days("2026-01-01", 10, step=2)
    today = _d("2026-01-31")
    kw = dict(last_in_app=_d("2026-01-19"), last_import=None)
    assert engagement.you_video_milestone(dates, today, **kw) == 30
    assert engagement.you_video_milestone(dates, today, already=30, **kw) is None
    # Большая веха после меньшей — да; меньшая после большей — нет.
    assert engagement.you_video_milestone(dates, today, already=7, **kw) == 30
    assert engagement.you_video_milestone(dates, today, already=90, **kw) is None


def test_too_few_workouts_by_the_milestone_day():
    # Две тренировки за первую неделю — «как ты вырос» было бы неправдой.
    assert engagement.you_video_milestone(
        _days("2026-01-01", 2), _d("2026-01-08"), last_in_app=_d("2026-01-02"), last_import=None
    ) is None
    assert engagement.you_video_milestone(
        _days("2026-01-01", 3), _d("2026-01-08"), last_in_app=_d("2026-01-03"), last_import=None
    ) == 7
    # Тренировки после дня вехи в порог не идут.
    dates = _days("2026-01-01", 2) + [_d("2026-01-09")]
    assert engagement.you_video_milestone(dates, _d("2026-01-09"), last_in_app=_d("2026-01-09"),
                                          last_import=None) is None


def test_quiet_account_is_skipped():
    dates = _days("2026-01-01", 8)
    today = _d("2026-01-31")
    assert engagement.you_video_milestone(dates, today, last_in_app=_d("2026-01-08"), last_import=None) is None
    assert engagement.you_video_milestone(dates, today, last_in_app=None, last_import=None) is None
    assert engagement.you_video_milestone(dates, today, last_in_app=_d("2026-01-17"), last_import=None) == 30


def test_import_does_not_fire_a_milestone_the_next_day():
    # Перенёс полгода из Hevy 1 июля; веха 180 наступает 2 июля.
    hevy = _days("2026-01-03", 60, step=3)
    today = _d("2026-07-02")
    assert engagement.you_video_milestone(
        hevy + [today], today, last_in_app=today, last_import=_d("2026-07-01")
    ) is None
    # Перенёс за 8 дней до вехи и с тех пор тренируется у нас — веха его.
    assert engagement.you_video_milestone(
        hevy + [_d("2026-06-30")], today, last_in_app=_d("2026-06-30"), last_import=_d("2026-06-24")
    ) == 180
    # Только перенесённые тренировки, своих нет — тоже молчим.
    assert engagement.you_video_milestone(hevy, _d("2026-07-02"), last_in_app=None, last_import=_d("2026-06-01")) \
        is None


# ---------- тексты ----------


@pytest.mark.parametrize("lang", ["ru", "en"])
@pytest.mark.parametrize("gender", ["male", "female", None])
def test_copy_fits_the_banner_and_keeps_the_formula(lang, gender):
    assert {d for d, _ in engagement.YOU_VIDEO_MILESTONES} == set(push_ios.YOU_VIDEO_COPY_KEYS)
    head = "ПРИВЕТ АТЛЕТ! " if lang == "ru" else "HEY ATHLETE! "
    with i18n.use_lang(lang):
        for days in push_ios.YOU_VIDEO_COPY_KEYS:
            title, body = push_ios.you_video_alert(days, gender)
            assert title == title.upper()
            assert len(title) <= push_ios.TITLE_LIMIT
            assert len(body) <= push_ios.BODY_LIMIT
            assert body.startswith(head)
            assert "{" not in body and "}" not in body


def test_gendered_wording_in_russian():
    with i18n.use_lang("ru"):
        assert "ты вырос " in push_ios.you_video_alert(30, "male")[1]
        assert "ты выросла " in push_ios.you_video_alert(30, "female")[1]
        neutral = push_ios.you_video_alert(30, None)[1]
        assert "вырос" not in neutral
        for days in push_ios.YOU_VIDEO_COPY_KEYS:
            body = push_ios.you_video_alert(days, None)[1]
            for gendered in ("вырос ", "выросла", "начал ", "начала"):
                assert gendered not in body, (days, body)
    with i18n.use_lang("en"):
        assert push_ios.you_video_alert(30, "male") == push_ios.you_video_alert(30, None)


# ---------- рассылка ----------


async def test_month_push_goes_to_ios_with_the_video_route(fresh_db, monkeypatch, sent):
    db = fresh_db
    uid = await _athlete(db, gender="female")
    for day in _days("2026-09-20", 8, step=3):
        await _workout(db, uid, day.isoformat())

    await _run(monkeypatch, "2026-10-20T19:05")

    assert len(sent) == 1
    push = sent[0]
    assert push["category"] == push_texts.YOU_VIDEO == "you_video"
    assert push["route"] == {"screen": "you_video", "days": 30}
    assert push["title"] == "МЕСЯЦ В ПОДВАЛЕ"
    assert "ты выросла" in push["body"]
    assert (await db.get_user(uid))["you_video_milestone"] == 30
    assert await db.has_push_today(uid, "2026-10-20")
    inbox = await db.conn().execute("SELECT category FROM inbox WHERE user_id = ?", (uid,))
    assert [r[0] for r in await inbox.fetchall()] == ["you_video"]

    # Тот же вечер и следующий день окна — уже было.
    await _run(monkeypatch, "2026-10-20T20:05")
    await _run(monkeypatch, "2026-10-21T19:05")
    assert len(sent) == 1


async def test_english_athlete_gets_english_copy(fresh_db, monkeypatch, sent):
    db = fresh_db
    uid = await _athlete(db, lang="en")
    for day in _days("2026-10-13", 4):
        await _workout(db, uid, day.isoformat())
    await _run(monkeypatch, "2026-10-20T19:05")
    assert [(p["title"], p["route"]) for p in sent] == [
        ("ONE WEEK IN THE BASEMENT", {"screen": "you_video", "days": 7})
    ]
    assert sent[0]["body"].startswith("HEY ATHLETE! ")


async def test_no_ios_token_no_push_and_no_telegram(fresh_db, monkeypatch, sent):
    db = fresh_db
    uid = await _athlete(db, token=False)
    for day in _days("2026-10-13", 4):
        await _workout(db, uid, day.isoformat())
    await _run(monkeypatch, "2026-10-20T19:05")
    assert sent == []
    assert not await db.has_push_today(uid, "2026-10-20")
    assert (await db.get_user(uid))["you_video_milestone"] == 0


async def test_busy_day_moves_the_milestone_to_the_next_evening(fresh_db, monkeypatch, sent):
    db = fresh_db
    uid = await _athlete(db)
    for day in _days("2026-10-13", 4):
        await _workout(db, uid, day.isoformat())
    await db.record_push(uid, push_texts.SKIP_3, "x", "2026-10-20")
    await _run(monkeypatch, "2026-10-20T19:05")
    assert sent == []
    await _run(monkeypatch, "2026-10-21T19:05")
    assert len(sent) == 1


async def test_night_and_pushes_off_are_respected(fresh_db, monkeypatch, sent):
    db = fresh_db
    uid = await _athlete(db)
    for day in _days("2026-10-13", 4):
        await _workout(db, uid, day.isoformat())
    await _run(monkeypatch, "2026-10-20T23:05")
    await _run(monkeypatch, "2026-10-21T08:05")
    assert sent == []
    await db.update_user(uid, pushes_enabled=0)
    await _run(monkeypatch, "2026-10-21T19:05")
    assert sent == []


async def test_fresh_import_does_not_trigger_old_milestones(fresh_db, monkeypatch, sent):
    db = fresh_db
    uid = await _athlete(db)
    batch = await db.create_import_batch(uid, "hevy")
    for day in _days("2026-04-25", 40, step=4):  # 180-й день — 22 октября
        await _workout(db, uid, day.isoformat(), import_batch_id=batch)
    await db.finish_import_batch(batch, 40, 120)
    await db.conn().execute(
        "UPDATE import_batches SET created_at = ? WHERE id = ?", ("2026-10-21T10:00:00", batch)
    )
    await db.conn().commit()
    await _workout(db, uid, "2026-10-21")

    await _run(monkeypatch, "2026-10-22T19:05")
    await _run(monkeypatch, "2026-10-23T19:05")
    assert sent == []
