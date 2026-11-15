"""Часовой тик пушей (`engagement._send_daily_pushes`): все пулы и сбои внутри них.

Уже покрыт сбой доставки в основном пуле (`test_audit_silent_failures.py`).
Не исполнялись: второй проход «только во „Уведомления"» (приложение с
выключенными пушами — `inbox_only`, без вызова модели, без паузы между
отправками), сбои сборки и доставки в пуле новичков и сбои хвостовых задач
тика (веха «как ты вырос», запрос отзыва, сводка админу). Ошибка здесь
молча оставляет без пуша всех, кто в цикле после упавшего, — до следующего
дня, а не до следующего часа.
"""

from unittest.mock import MagicMock

import pytest

import engagement
import push_texts

pytestmark = pytest.mark.asyncio


@pytest.fixture
def tick(monkeypatch):
    calls = {"build": [], "digest": [], "newbie": [], "deliver": [], "sleep": 0}

    async def pool(*, inbox_only=False):
        return [(204, 3)] if inbox_only else [(201, 3), (202, 3), (203, 3)]

    async def newbies(*, inbox_only=False):
        return [(304, "2026-10-01T00:00:00", 3)] if inbox_only else [
            (301, "2026-10-01T00:00:00", 3),
            (302, "2026-10-01T00:00:00", 3),
            (303, "2026-10-01T00:00:00", 3),
        ]

    async def no_user(_telegram_id):
        return None

    async def build(telegram_id, today, **kwargs):
        calls["build"].append((telegram_id, kwargs))
        if telegram_id == 201:
            raise RuntimeError("сборка упала")
        return engagement.PushDecision(push_texts.SKIP_3, "текст")

    async def build_digest(telegram_id, today, **kwargs):
        calls["digest"].append((telegram_id, kwargs))
        return None

    async def build_newbie(telegram_id, created_at, today, tz_offset=0):
        calls["newbie"].append(telegram_id)
        if telegram_id == 301:
            raise RuntimeError("сборка новичку упала")
        return engagement.PushDecision(push_texts.NEWBIE_NUDGE, "текст")

    async def deliver(bot, telegram_id, decision, local_date, **kwargs):
        calls["deliver"].append((telegram_id, kwargs.get("inbox_only", False)))
        if telegram_id in (202, 302):
            raise RuntimeError("доставка упала")

    async def fake_sleep(_seconds):
        calls["sleep"] += 1

    async def boom(*_args, **_kwargs):
        raise RuntimeError("хвостовая задача упала")

    monkeypatch.setattr(engagement.db, "list_engagement_eligible_user_ids", pool)
    monkeypatch.setattr(engagement.db, "list_newbie_user_ids", newbies)
    monkeypatch.setattr(engagement.db, "get_user", no_user)
    monkeypatch.setattr(engagement, "build_daily_push", build)
    monkeypatch.setattr(engagement, "build_weekly_digest_push", build_digest)
    # Понедельник: наступили оба слота (should_send_now подменён ниже), дайджест
    # не вышел — каждый идёт дальше к сигналу дня.
    monkeypatch.setattr(engagement, "_utc_now", lambda: engagement.dt.datetime(2026, 10, 12, 9, 0))
    monkeypatch.setattr(engagement, "build_newbie_push", build_newbie)
    monkeypatch.setattr(engagement, "_deliver", deliver)
    monkeypatch.setattr(engagement, "should_send_now", lambda tz, hour: True)
    monkeypatch.setattr(engagement.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(engagement, "_send_you_video_pushes", boom)
    monkeypatch.setattr(engagement, "_send_feedback_pushes", boom)
    monkeypatch.setattr(engagement, "_maybe_send_admin_funnel_digest", boom)
    return calls


async def test_one_failure_never_skips_the_rest_of_any_pool(tick):
    await engagement._send_daily_pushes(MagicMock())

    delivered = [tid for tid, _ in tick["deliver"]]
    # Упавшая сборка (201, 301) не доходит до доставки, упавшая доставка
    # (202, 302) не обрывает цикл — все, кто после них, получили своё.
    assert delivered == [202, 203, 204, 302, 303, 304]
    assert [tid for tid, _ in tick["build"]] == [201, 202, 203, 204]
    assert tick["newbie"] == [301, 302, 303, 304]


async def test_inbox_only_pool_skips_ai_and_the_telegram_pause(tick):
    await engagement._send_daily_pushes(MagicMock())

    inbox = {tid for tid, inbox_only in tick["deliver"] if inbox_only}
    assert inbox == {204, 304}
    # Модель не зовётся ради записи в «Уведомления»: флаг уходит сборщику
    # дайджеста, сигнал дня модель не зовёт вовсе.
    assert dict(tick["digest"])[204] == {"allow_ai": False}
    assert dict(tick["digest"])[203] == {}
    assert dict(tick["build"])[204] == {}
    # Пауза — только между настоящими отправками в Telegram (203 и 303;
    # после упавших доставок 202/302 до паузы дело не доходит).
    assert tick["sleep"] == 2


async def test_tail_jobs_failing_do_not_break_the_tick(tick):
    # Все три хвостовые задачи падают — тик всё равно завершается без исключения.
    await engagement._send_daily_pushes(MagicMock())


@pytest.fixture
def morning(monkeypatch):
    """Настоящие should_send_now/due_slots, подменены только пулы и сборщики:
    атлет 401 с поясом UTC+3, новичок 501 там же, атлет 402 без пояса."""
    calls = {"digest": [], "daily": [], "newbie": [], "deliver": []}

    async def pool(*, inbox_only=False):
        return [] if inbox_only else [(401, 3), (402, 0)]

    async def newbies(*, inbox_only=False):
        return [] if inbox_only else [(501, "2026-07-01T00:00:00", 3)]

    async def no_user(_telegram_id):
        return None

    async def build_digest(telegram_id, today, **kwargs):
        calls["digest"].append((telegram_id, today))
        return engagement.PushDecision(push_texts.WEEKLY_DIGEST, "итоги", with_cta=False)

    async def build_daily(telegram_id, today, **kwargs):
        calls["daily"].append(telegram_id)
        return engagement.PushDecision(push_texts.SKIP_3, "пропуск")

    async def build_newbie(telegram_id, created_at, today, tz_offset=0):
        calls["newbie"].append(telegram_id)
        return engagement.PushDecision(push_texts.NEWBIE_NUDGE, "новичок")

    async def deliver(bot, telegram_id, decision, local_date, **kwargs):
        calls["deliver"].append((telegram_id, decision.category))

    async def nothing(*_args, **_kwargs):
        return None

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(engagement.config, "ENGAGEMENT_HOUR", 19)
    monkeypatch.setattr(engagement.config, "WEEKLY_DIGEST_HOUR", 9)
    monkeypatch.setattr(engagement.db, "list_engagement_eligible_user_ids", pool)
    monkeypatch.setattr(engagement.db, "list_newbie_user_ids", newbies)
    monkeypatch.setattr(engagement.db, "get_user", no_user)
    monkeypatch.setattr(engagement, "build_weekly_digest_push", build_digest)
    monkeypatch.setattr(engagement, "build_daily_push", build_daily)
    monkeypatch.setattr(engagement, "build_newbie_push", build_newbie)
    monkeypatch.setattr(engagement, "_deliver", deliver)
    monkeypatch.setattr(engagement.asyncio, "sleep", fake_sleep)
    for tail in ("_send_you_video_pushes", "_send_year_review_pushes", "_send_feedback_pushes",
                 "_maybe_send_admin_funnel_digest"):
        monkeypatch.setattr(engagement, tail, nothing)
    return calls


def _clock(monkeypatch, *args):
    monkeypatch.setattr(engagement, "_utc_now", lambda: engagement.dt.datetime(*args))


async def test_monday_morning_tick_sends_only_the_digest(morning, monkeypatch):
    _clock(monkeypatch, 2026, 7, 20, 6, 0)  # 09:00 понедельника в UTC+3
    await engagement._send_daily_pushes(MagicMock())
    assert morning["digest"] == [(401, engagement.dt.date(2026, 7, 20))]
    # Ни сигнала дня, ни новичка утром.
    assert morning["daily"] == [] and morning["newbie"] == []
    assert morning["deliver"] == [(401, push_texts.WEEKLY_DIGEST)]


async def test_monday_evening_tick_has_no_digest(morning, monkeypatch):
    _clock(monkeypatch, 2026, 7, 20, 16, 0)  # 19:00 понедельника в UTC+3
    await engagement._send_daily_pushes(MagicMock())
    assert morning["digest"] == []
    assert morning["daily"] == [401] and morning["newbie"] == [501]


async def test_sunday_ticks_have_no_digest(morning, monkeypatch):
    for hour in range(24):
        _clock(monkeypatch, 2026, 7, 19, hour, 0)
        await engagement._send_daily_pushes(MagicMock())
    assert morning["digest"] == []


async def test_unknown_tz_digest_first_then_daily_in_the_safe_tick(morning, monkeypatch):
    _clock(monkeypatch, 2026, 7, 20, engagement.UNKNOWN_TZ_SEND_HOUR_UTC, 0)
    await engagement._send_daily_pushes(MagicMock())
    assert [tid for tid, _ in morning["digest"]] == [402]
    # Дайджест вышел — сигнал дня ему в тот же тик не собирается.
    assert morning["daily"] == []
    assert (402, push_texts.WEEKLY_DIGEST) in morning["deliver"]


async def test_unknown_tz_never_gets_the_digest_at_night_hours(morning, monkeypatch):
    """Неизвестный пояс за все 24 тика понедельника получает дайджест один раз
    — в UNKNOWN_TZ_SEND_HOUR_UTC, когда от Лондона до Камчатки не ночь."""
    hours = []
    for hour in range(24):
        _clock(monkeypatch, 2026, 7, 20, hour, 0)
        before = len(morning["digest"])
        await engagement._send_daily_pushes(MagicMock())
        if any(tid == 402 for tid, _ in morning["digest"][before:]):
            hours.append(hour)
    assert hours == [engagement.UNKNOWN_TZ_SEND_HOUR_UTC]
    for offset in range(0, 13):
        assert not engagement.is_quiet_hour((hours[0] + offset) % 24)
