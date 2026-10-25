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
    calls = {"build": [], "newbie": [], "deliver": [], "sleep": 0}

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
    # Модель не зовётся ради записи в «Уведомления».
    assert dict(tick["build"])[204] == {"allow_ai": False}
    assert dict(tick["build"])[203] == {}
    # Пауза — только между настоящими отправками в Telegram (203 и 303;
    # после упавших доставок 202/302 до паузы дело не доходит).
    assert tick["sleep"] == 2


async def test_tail_jobs_failing_do_not_break_the_tick(tick):
    # Все три хвостовые задачи падают — тик всё равно завершается без исключения.
    await engagement._send_daily_pushes(MagicMock())
