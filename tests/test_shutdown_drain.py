"""Мягкая остановка на деплое (shutdown_drain.py): идущие ходы AI-тренера
договаривают, новые `/v1/ai/*` получают 503 на языке атлета, база закрывается
последней."""

import asyncio
import logging

import httpx
import pytest

import ai_trainer
import api_v1
import i18n
import shutdown_drain

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clean_registry():
    shutdown_drain.reset()
    yield
    shutdown_drain.reset()


async def _linked_client(fresh_db, telegram_id=111, lang=None):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    if lang is not None:
        await fresh_db.set_user_lang(telegram_id, lang)
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


# ---------- реестр ----------


async def test_track_counts_and_releases_on_error():
    assert shutdown_drain.in_flight() == 0
    async with shutdown_drain.track("bot"), shutdown_drain.track("v1"):
        assert shutdown_drain.in_flight() == 2
        assert shutdown_drain.in_flight_by_kind() == {"bot": 1, "v1": 1}
    assert shutdown_drain.in_flight() == 0

    with pytest.raises(RuntimeError):
        async with shutdown_drain.track("bot"):
            raise RuntimeError("boom")
    assert shutdown_drain.in_flight() == 0


async def test_paid_call_inside_turn_is_not_a_second_turn():
    async with shutdown_drain.track_unless_inside("paid"):
        assert shutdown_drain.in_flight_by_kind() == {"paid": 1}
    async with shutdown_drain.track("bot"), shutdown_drain.track_unless_inside("paid"):
        assert shutdown_drain.in_flight_by_kind() == {"bot": 1}
    assert shutdown_drain.in_flight() == 0


async def test_tracked_decorator_counts_the_whole_call():
    seen = []

    @shutdown_drain.tracked("bot")
    async def turn(x):
        seen.append(shutdown_drain.in_flight())
        return x * 2

    assert await turn(21) == 42
    assert seen == [1]
    assert shutdown_drain.in_flight() == 0


async def test_paid_call_is_tracked(monkeypatch):
    seen = []

    async def fake_log(*args, **kwargs):
        seen.append(("cost", shutdown_drain.in_flight()))

    monkeypatch.setattr(ai_trainer, "_log_llm_cost", fake_log)

    async def call():
        seen.append(("call", shutdown_drain.in_flight()))
        return object()

    await ai_trainer.paid_call(1, None, call)
    # И сам вызов, и запись его цены — внутри хода: остановка ждёт обоих.
    assert seen == [("call", 1), ("cost", 1)]
    assert shutdown_drain.in_flight() == 0


# ---------- порядок остановки ----------


async def test_shutdown_waits_for_turn_then_closes_db(caplog):
    order = []
    release = asyncio.Event()

    async def turn():
        async with shutdown_drain.track("bot"):
            await release.wait()
            order.append("turn finished")

    async def stop_http():
        order.append("http stopped")

    async def close_session():
        order.append("session closed")

    async def close_db():
        order.append("db closed")

    turn_task = asyncio.create_task(turn())
    await asyncio.sleep(0)
    background = asyncio.create_task(asyncio.sleep(3600))

    with caplog.at_level(logging.WARNING, logger="shutdown_drain"):
        stopping = asyncio.create_task(shutdown_drain.shutdown(
            grace=5, stop_http=stop_http, background=[background],
            closers=[close_session], close_db=close_db,
        ))
        await asyncio.sleep(0.2)
        assert shutdown_drain.is_draining()
        assert order == []  # ход ещё идёт — ничего не гасим
        release.set()
        left = await stopping

    await turn_task
    assert left == 0
    assert order == ["turn finished", "http stopped", "session closed", "db closed"]
    assert background.cancelled() or background.cancelling()
    assert "draining 1 AI turns, up to 5 s" in caplog.text
    assert "drained in" in caplog.text


async def test_grace_expiry_cuts_turn_and_still_closes_db(caplog):
    closed = []

    async def stuck_turn():
        async with shutdown_drain.track("v1_stream"):
            await asyncio.sleep(3600)

    async def close_db():
        closed.append(shutdown_drain.in_flight())

    task = asyncio.create_task(stuck_turn())
    await asyncio.sleep(0)
    with caplog.at_level(logging.WARNING, logger="shutdown_drain"):
        left = await shutdown_drain.shutdown(grace=0.2, close_db=close_db)
    assert left == 1
    assert closed == [1]  # база закрыта, не дожидаясь вечного хода
    assert "grace expired" in caplog.text
    assert "1 AI turns cut" in caplog.text
    task.cancel()


# ---------- /v1 на остановке ----------


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_new_ai_request_during_drain_gets_localized_503(fresh_db, monkeypatch, lang):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)

    async def must_not_run(*args, **kwargs):
        raise AssertionError("модель на остановке звать нельзя")

    monkeypatch.setattr(ai_trainer, "ask", must_not_run)
    client = await _linked_client(fresh_db, lang=lang)
    shutdown_drain.begin_drain()

    for path, body in [
        ("/ai/ask", {"question": "Как прогресс?"}),
        ("/ai/ask", {"question": "Как прогресс?", "stream": True}),
        ("/ai/questions/answer", {"answer": "3"}),
        ("/ai/video", {}),
        ("/ai/voice", {}),
    ]:
        resp = await client.post(path, json=body)
        assert resp.status_code == 503, (path, resp.text)
        assert resp.headers["Retry-After"] == str(shutdown_drain.RETRY_AFTER_SECONDS)
        payload = resp.json()
        assert payload["error"] == "shutting_down"
        assert payload["message"] == i18n.t_in(lang, "api.error.shutting_down")
        has_cyrillic = any("а" <= ch.lower() <= "я" for ch in payload["message"])
        assert has_cyrillic is (lang == "ru")

    # Чтение — не ход тренера: история открывается и на остановке.
    assert (await client.get("/ai/history")).status_code == 200
    assert shutdown_drain.in_flight() == 0


async def test_stream_in_flight_finishes_during_drain(fresh_db, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_ask(user_id, question, history, on_chunk=None, **kwargs):
        await on_chunk("Смотрю")
        started.set()
        await release.wait()
        return "Смотрю на твои тренировки: жим растёт."

    monkeypatch.setattr(ai_trainer, "ask", slow_ask)
    client = await _linked_client(fresh_db)

    request = asyncio.create_task(client.post("/ai/ask", json={"question": "Как прогресс?", "stream": True}))
    await asyncio.wait_for(started.wait(), 5)
    assert shutdown_drain.in_flight() >= 1

    shutdown_drain.begin_drain()
    draining = asyncio.create_task(shutdown_drain.drain(grace=5))
    await asyncio.sleep(0.2)
    assert not draining.done()  # стрим ещё пишется — ждём

    release.set()
    assert await asyncio.wait_for(draining, 5) == 0
    resp = await asyncio.wait_for(request, 5)
    assert resp.status_code == 200
    assert "event: done" in resp.text


# ---------- обвязка: uvicorn и fly.toml ----------


async def test_uvicorn_does_not_take_over_signals():
    """Свой capture_signals uvicorn ставил обработчики поверх main.py и гасил
    HTTP за 0.1 с — посреди стримов."""
    import signal

    import uvicorn

    import mcp_server

    before = signal.getsignal(signal.SIGTERM)
    server = mcp_server._Server(uvicorn.Config(app=None))
    with server.capture_signals():
        assert signal.getsignal(signal.SIGTERM) is before


async def test_fly_kill_timeout_covers_the_drain():
    import tomllib
    from pathlib import Path

    import config

    fly = tomllib.loads((Path(__file__).resolve().parent.parent / "fly.toml").read_text())
    assert fly["kill_signal"] in ("SIGTERM", "SIGINT")  # оба ловит main._install_shutdown_signals
    budget = config.SHUTDOWN_AI_GRACE_SECONDS + shutdown_drain.HTTP_STOP_SECONDS + 2
    assert int(fly["kill_timeout"]) >= budget + 5
