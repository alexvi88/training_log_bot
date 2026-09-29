"""Тревоги админу (ops_alerts.py): склейка повторов, что попадает в тревоги из
лога, новые сбои iOS, часовые проверки."""

import asyncio
import logging

import httpx
import pytest

import ai_limits
import api_v1
import api_v1_diagnostics
import config
import ops_alerts

T0 = 1_000_000.0


def _record(msg, *args, level=logging.ERROR, name="handlers.workout", exc=None, **extra):
    exc_info = (type(exc), exc, exc.__traceback__) if exc is not None else None
    record = logging.LogRecord(name, level, __file__, 1, msg, args, exc_info)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def _raised(exc):
    try:
        raise exc
    except Exception as caught:
        return caught


# --- склейка ---------------------------------------------------------------------


def test_first_error_goes_now_repeats_wait_for_summary():
    agg = ops_alerts.Aggregator()
    assert agg.observe("a", "KeyError: x", T0)
    assert not agg.observe("a", "KeyError: x", T0 + 1)
    assert not agg.observe("a", "KeyError: x", T0 + 2)
    assert agg.take_repeats() == [("KeyError: x", 2)]
    # Сводка обнулила счётчик: пустой сводки не бывает.
    assert agg.take_repeats() == []


def test_error_quiet_for_hours_is_news_again():
    agg = ops_alerts.Aggregator()
    assert agg.observe("a", "x", T0)
    assert agg.observe("a", "x", T0 + ops_alerts.QUIET_RESET_SECONDS + 1)


def test_cascade_of_distinct_errors_is_capped_per_window():
    agg = ops_alerts.Aggregator()
    sent = [agg.observe(f"s{i}", f"e{i}", T0) for i in range(ops_alerts.MAX_IMMEDIATE_PER_WINDOW + 3)]
    assert sent.count(True) == ops_alerts.MAX_IMMEDIATE_PER_WINDOW
    # Не отправленные сразу не теряются — они в сводке, с первым вхождением.
    assert sorted(agg.take_repeats()) == [(f"e{i}", 1) for i in (10, 11, 12)]
    # Новое окно — снова сразу.
    assert agg.observe("new", "e", T0 + ops_alerts.REPEAT_FLUSH_SECONDS)


def test_signature_groups_same_template_and_same_raise_site():
    a = _record("APNs: пуш пользователю %s не доставлен", 1)
    b = _record("APNs: пуш пользователю %s не доставлен", 2)
    assert ops_alerts.signature(a) == ops_alerts.signature(b)

    def boom(value):
        raise KeyError(value)

    e1, e2 = _raised_call(boom, "x"), _raised_call(boom, "y")
    assert ops_alerts.signature(_record("fail", exc=e1)) == ops_alerts.signature(_record("fail", exc=e2))
    assert ops_alerts.signature(_record("fail", exc=e1)) != ops_alerts.signature(
        _record("fail", exc=_raised(ValueError("z")))
    )


def _raised_call(fn, arg):
    try:
        fn(arg)
    except Exception as caught:
        return caught


# --- что считается тревогой ---------------------------------------------------------


def test_only_errors_and_not_our_own_or_muted_records():
    assert ops_alerts.should_alert(_record("x"))
    assert ops_alerts.should_alert(_record("x", level=logging.CRITICAL))
    assert not ops_alerts.should_alert(_record("x", level=logging.WARNING))
    assert not ops_alerts.should_alert(_record("x", name="ops_alerts"))
    assert not ops_alerts.should_alert(_record("x", ops_alert=False))
    assert not ops_alerts.should_alert(
        _record("Failed to fetch updates - %s: %s", "TelegramNetworkError", "timeout",
                name="aiogram.dispatcher")
    )


def test_record_text_is_html_safe_and_fits_telegram():
    exc = _raised(RuntimeError("<b>" + "x" * 10_000))
    text = ops_alerts.record_text(_record("api_v1: unhandled error on %s %s", "GET", "/v1/<me>", exc=exc))
    assert "&lt;me&gt;" in text and "&lt;b&gt;" in text
    assert "RuntimeError" in text
    assert len(text) <= ops_alerts.MAX_MESSAGE_CHARS + 20
    assert text.count("<pre>") == text.count("</pre>")


# --- очередь и лог-хендлер -------------------------------------------------------------


@pytest.fixture
def alerts(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 42)
    monkeypatch.setattr(config, "OPS_ALERTS_ENABLED", True)
    monkeypatch.setattr(ops_alerts, "_aggregator", ops_alerts.Aggregator())
    handler = None

    async def _install():
        nonlocal handler
        handler = ops_alerts.install()
        return ops_alerts._queue

    yield _install
    if handler is not None:
        logging.getLogger().removeHandler(handler)
    monkeypatch.setattr(ops_alerts, "_loop", None)
    monkeypatch.setattr(ops_alerts, "_queue", None)


def _drain(queue):
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


@pytest.mark.asyncio
async def test_logged_error_lands_in_queue_once(alerts):
    queue = await alerts()
    log = logging.getLogger("handlers.test_ops_alerts")
    for _ in range(3):
        log.error("Фоновая задача %s умерла", "engagement")
    log.warning("это не тревога")
    await asyncio.sleep(0)
    (text,) = _drain(queue)
    assert "Фоновая задача engagement умерла" in text


@pytest.mark.asyncio
async def test_sender_delivers_and_survives_telegram_failure(alerts):
    queue = await alerts()

    class Bot:
        def __init__(self):
            self.sent = []

        async def send_message(self, **kwargs):
            if not self.sent:
                self.sent.append(None)
                raise RuntimeError("telegram down")
            self.sent.append(kwargs)

    bot = Bot()
    ops_alerts.enqueue_text("one")
    ops_alerts.enqueue_text("two")
    task = asyncio.create_task(ops_alerts.run_alert_sender(bot))
    for _ in range(20):
        await asyncio.sleep(0)
    task.cancel()
    assert bot.sent[1]["text"] == "two"
    assert bot.sent[1]["chat_id"] == 42
    assert bot.sent[1]["disable_notification"] is False
    assert queue.empty()


def test_disabled_without_admin(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", None)
    assert not ops_alerts.enabled()
    monkeypatch.setattr(config, "ADMIN_ID", 42)
    monkeypatch.setattr(config, "OPS_ALERTS_ENABLED", False)
    assert not ops_alerts.enabled()


# --- новые сбои iOS ------------------------------------------------------------------


def _crash(exc_type=1, signal=11, build="57"):
    return {
        "callStackTree": {"callStacks": []},
        "diagnosticMetaData": {
            "appVersion": "1.4", "appBuildVersion": build, "osVersion": "iPhone OS 17.5.1",
            "deviceType": "iPhone15,2", "exceptionType": exc_type, "signal": signal,
            "terminationReason": "Namespace SIGNAL, Code 11",
        },
    }


@pytest.mark.asyncio
async def test_only_first_crash_of_its_kind_in_build_alerts(fresh_db, alerts):
    queue = await alerts()
    api_v1_diagnostics.reset_rate_limit()
    transport = httpx.ASGITransport(app=api_v1.build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for payload in (_crash(), _crash(), _crash(signal=6), _crash(build="58")):
            resp = await client.post("/diagnostics", json={"kind": "crash", "payload": payload})
            assert resp.status_code == 201, resp.text
        resp = await client.post(
            "/diagnostics", json={"kind": "cpu_exception", "payload": _crash(build="99")}
        )
        assert resp.status_code == 201
    await asyncio.sleep(0)
    texts = _drain(queue)
    assert len(texts) == 3
    assert "1.4 (57)" in texts[0] and "sig 11" in texts[0] and "id = 1" in texts[0]
    assert "sig 6" in texts[1]
    assert "1.4 (58)" in texts[2]


# --- часовые проверки --------------------------------------------------------------


def test_apns_alert_only_when_most_pushes_fail():
    assert ops_alerts.apns_alert_text(4, 4) is None
    assert ops_alerts.apns_alert_text(10, 4) is None
    assert "5 из 10" in ops_alerts.apns_alert_text(10, 5)


def test_apns_counts_reset_after_take():
    ops_alerts.take_apns_counts()
    ops_alerts.record_apns_result(True)
    ops_alerts.record_apns_result(False)
    assert ops_alerts.take_apns_counts() == (2, 1)
    assert ops_alerts.take_apns_counts() == (0, 0)


def test_spend_alert_once_per_level_per_day(monkeypatch):
    monkeypatch.setattr(ops_alerts, "_spend_alerted", {})
    assert ops_alerts.spend_alert_text(None, 1.0, "2026-09-29") is None
    assert "мягком" in ops_alerts.spend_alert_text(ai_limits.KIND_SPEND_SOFT, 11.0, "2026-09-29")
    assert ops_alerts.spend_alert_text(ai_limits.KIND_SPEND_SOFT, 12.0, "2026-09-29") is None
    assert "замолчал" in ops_alerts.spend_alert_text(ai_limits.KIND_SPEND_HARD, 26.0, "2026-09-29")
    assert ops_alerts.spend_alert_text(ai_limits.KIND_SPEND_SOFT, 11.0, "2026-09-30") is not None
