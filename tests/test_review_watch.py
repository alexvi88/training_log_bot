"""Зеркало действий ревьюера админу (review_watch.py)."""

import asyncio
import datetime as dt

import httpx
import pytest

import api_v1
import config
import db
import review_demo
import review_watch

USERNAME = "appreview"
PASSWORD = "correct horse battery staple"
ADMIN = 777


@pytest.fixture(autouse=True)
def watch_config(monkeypatch):
    monkeypatch.setattr(config, "REVIEW_DEMO_USERNAME", USERNAME)
    monkeypatch.setattr(config, "REVIEW_DEMO_PASSWORD", PASSWORD)
    monkeypatch.setattr(config, "WALK_DEMO_USERNAME", "walker")
    monkeypatch.setattr(config, "WALK_DEMO_PASSWORD", "walk-pass")
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN)
    monkeypatch.setattr(config, "REVIEW_WATCH_ENABLED", True)
    review_demo.reset_failures()
    review_watch.reset()
    yield
    review_watch.reset()
    review_demo.reset_failures()


@pytest.fixture
def no_task(monkeypatch):
    """Очередь копится, фоновую задачу не запускаем."""
    monkeypatch.setattr(review_watch, "_ensure_task", lambda: None)


def _item(kind="api_action", content="записал подход", loud=False, source="ios"):
    return review_watch._Item(dt.datetime(2026, 10, 5, 12, 0, 5), source, kind, content, loud)


def test_format_line_uses_admin_clock_and_one_line():
    line = review_watch.format_line(_item(content="а\nб   в"))
    assert line == "15:00:05 ios · api_action · а б в"  # ADMIN_TZ_OFFSET по умолчанию 3


def test_format_line_truncates_ai_reply():
    line = review_watch.format_line(_item(kind="ai_reply", content="x" * 1000))
    assert len(line.split(" · ", 2)[2]) == review_watch.AI_REPLY_LIMIT
    assert line.endswith("…")


def test_build_messages_splits_under_telegram_limit():
    lines = ["y" * 290] * 40
    messages = review_watch.build_messages(lines)
    assert len(messages) > 1
    assert all(len(m) <= 4096 and m.startswith(review_watch.HEADER) for m in messages)
    assert sum(m.count("y" * 290) for m in messages) == 40


def test_build_messages_empty_and_dropped():
    assert review_watch.build_messages([]) == []
    assert "пропущено событий: 3" in review_watch.build_messages(["a"], dropped=3)[0]


@pytest.mark.parametrize("flag,admin,username", [
    (False, ADMIN, USERNAME), (True, None, USERNAME), (True, ADMIN, ""),
])
@pytest.mark.asyncio
async def test_disabled_touches_neither_db_nor_queue(monkeypatch, no_task, flag, admin, username):
    monkeypatch.setattr(config, "REVIEW_WATCH_ENABLED", flag)
    monkeypatch.setattr(config, "ADMIN_ID", admin)
    monkeypatch.setattr(config, "REVIEW_DEMO_USERNAME", username)

    async def boom(*a, **k):
        raise AssertionError("db must not be touched")

    monkeypatch.setattr(db, "resolve_auth_identity", boom)
    await review_watch.on_event(-5, "message", "x", "ios")
    assert review_watch._queue == []


@pytest.mark.asyncio
async def test_positive_telegram_ids_never_hit_db(monkeypatch, no_task):
    async def boom(*a, **k):
        raise AssertionError("db must not be touched")

    monkeypatch.setattr(db, "resolve_auth_identity", boom)
    await review_watch.on_event(111, "message", "x", "tg")
    assert review_watch._queue == []


@pytest.mark.asyncio
async def test_identity_cache_and_invalidate(fresh_db, monkeypatch):
    calls = []
    real = db.resolve_auth_identity

    async def counting(provider, ident):
        calls.append((provider, ident))
        return await real(provider, ident)

    monkeypatch.setattr(db, "resolve_auth_identity", counting)
    assert await review_watch._reviewer_user_id() is None
    assert await review_watch._reviewer_user_id() is None
    assert calls == [("review_demo", USERNAME)]
    review_watch.invalidate_cache()
    await review_watch._reviewer_user_id()
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_log_user_event_queues_only_reviewer(fresh_db, no_task):
    reviewer = (await fresh_db.create_app_only_user(language_code="en"))["telegram_id"]
    await fresh_db.link_auth_identity(reviewer, "review_demo", USERNAME)
    walker = (await fresh_db.create_app_only_user(language_code="en"))["telegram_id"]
    await fresh_db.link_auth_identity(walker, "review_demo", "walker")
    # «Обычный» app-only аккаунт, чьё имя совпало с логином, — не ревьюер.
    lookalike = (await fresh_db.create_app_only_user(language_code="en"))["telegram_id"]
    real = (await fresh_db.get_or_create_user(telegram_id=111, username=USERNAME))["telegram_id"]

    for uid in (walker, lookalike, real):
        await fresh_db.log_user_event(uid, "message", "привет", source="ios")
    assert review_watch._queue == []

    await fresh_db.log_user_event(reviewer, "message", "привет", "secret-payload", source="ios")
    assert [(i.kind, i.content, i.source) for i in review_watch._queue] == [("message", "привет", "ios")]
    assert "secret-payload" not in review_watch.format_line(review_watch._queue[0])
    assert await fresh_db.count_user_events(reviewer) == 1


@pytest.mark.asyncio
async def test_recreated_account_is_followed_after_cache_reset(fresh_db, no_task):
    old = (await fresh_db.create_app_only_user(language_code="en"))["telegram_id"]
    await fresh_db.link_auth_identity(old, "review_demo", USERNAME)
    await fresh_db.log_user_event(old, "message", "1", source="ios")
    assert len(review_watch._queue) == 1

    import account_deletion
    await account_deletion.delete_account(old)
    # Id синтетических аккаунтов переиспользуются, поэтому занимаем освободившийся.
    await fresh_db.create_app_only_user(language_code="en")
    new = await review_demo.ensure_demo_user("en", USERNAME)
    assert new != old
    await fresh_db.log_user_event(new, "message", "2", source="ios")
    assert [i.content for i in review_watch._queue] == ["1", "2"]
    await fresh_db.log_user_event(old, "message", "ghost", source="ios")
    assert len(review_watch._queue) == 2


def test_first_event_of_session_is_loud(no_task, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(review_watch.time, "monotonic", lambda: clock[0])
    a, b, c = _item(), _item(), _item()
    review_watch._enqueue(a)
    clock[0] += 60
    review_watch._enqueue(b)
    clock[0] += review_watch.SESSION_GAP_SECONDS + 1
    review_watch._enqueue(c)
    assert (a.loud, b.loud, c.loud) == (True, False, True)


@pytest.mark.asyncio
async def test_flush_coalesces_loud_first_and_rate_limits():
    review_watch._queue.extend([_item(loud=True), _item(content="2"), _item(content="3")])
    sent, sleeps = [], []

    async def send(text, silent):
        sent.append((text, silent))

    async def fake_sleep(s):
        sleeps.append(s)

    assert await review_watch.flush(send, fake_sleep) == 2
    assert [s for _, s in sent] == [False, True]
    assert sent[1][0].count("\n") == 2  # шапка + две строки в одном сообщении
    assert sleeps == [review_watch.MIN_SEND_INTERVAL_SECONDS]
    assert review_watch._queue == []


@pytest.mark.asyncio
async def test_send_failure_is_swallowed(fresh_db):
    review_watch._queue.append(_item(loud=True))

    async def send(text, silent):
        raise RuntimeError("telegram down")

    assert await review_watch.flush(send, lambda s: asyncio.sleep(0)) == 1


@pytest.mark.asyncio
async def test_event_is_recorded_even_if_watch_breaks(fresh_db, monkeypatch):
    uid = (await fresh_db.create_app_only_user(language_code="en"))["telegram_id"]
    await fresh_db.link_auth_identity(uid, "review_demo", USERNAME)

    def boom():
        raise RuntimeError("no loop")

    monkeypatch.setattr(review_watch, "_ensure_task", boom)
    await fresh_db.log_user_event(uid, "message", "x", source="ios")
    assert await fresh_db.count_user_events(uid) == 1


@pytest.mark.asyncio
async def test_background_task_sends_via_sender(fresh_db, monkeypatch):
    monkeypatch.setattr(review_watch, "COALESCE_SECONDS", 0.01)
    monkeypatch.setattr(review_watch, "MIN_SEND_INTERVAL_SECONDS", 0.0)
    got = []

    async def send(text, silent):
        got.append((text, silent))

    review_watch._sender = send
    uid = (await fresh_db.create_app_only_user(language_code="en"))["telegram_id"]
    await fresh_db.link_auth_identity(uid, "review_demo", USERNAME)
    await fresh_db.log_user_event(uid, "message", "a", source="ios")
    await fresh_db.log_user_event(uid, "message", "b", source="ios")
    for _ in range(100):
        if got:
            break
        await asyncio.sleep(0.02)
    assert got and got[0][1] is False and "a" in got[0][0]


@pytest.mark.asyncio
async def test_login_event_has_ip_and_only_for_reviewer(fresh_db, no_task):
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    headers = {"fly-client-ip": "203.0.113.9"}
    resp = await client.post("/auth/password", json={"username": "walker", "password": "walk-pass"}, headers=headers)
    assert resp.status_code == 200
    assert review_watch._queue == []
    resp = await client.post("/auth/password", json={"username": USERNAME, "password": PASSWORD}, headers=headers)
    assert resp.status_code == 200
    assert [i.content for i in review_watch._queue] == ["ревьюер вошёл в приложение (203.0.113.9)"]
