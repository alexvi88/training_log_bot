"""AI-trainer cost logging (db.cost_events) and the admin daily cost report."""

import datetime as dt

import pytest

import admin_tasks
import config


async def test_log_cost_event_and_get_llm_cost_breakdown_groups_by_model(fresh_db, user_id):
    db = fresh_db
    today = db.now_iso()[:10]
    await db.log_cost_event(user_id, "llm_call", model="grok-4-1-fast", prompt_tokens=100, completion_tokens=50)
    await db.log_cost_event(user_id, "llm_call", model="grok-4-1-fast", prompt_tokens=200, completion_tokens=80)
    await db.log_cost_event(user_id, "llm_call", model="grok-4.20-multi-agent", prompt_tokens=10, completion_tokens=5)

    breakdown = await db.get_llm_cost_breakdown(today)

    assert breakdown["grok-4-1-fast"] == {"calls": 2, "prompt_tokens": 300, "completion_tokens": 130}
    assert breakdown["grok-4.20-multi-agent"] == {"calls": 1, "prompt_tokens": 10, "completion_tokens": 5}


async def test_get_llm_cost_breakdown_ignores_other_days_and_event_types(fresh_db, user_id):
    db = fresh_db
    await db.log_cost_event(user_id, "transcription", model=config.OPENAI_TRANSCRIBE_MODEL)
    await db.conn().execute(
        "INSERT INTO cost_events (user_id, event_type, model, prompt_tokens, completion_tokens, created_at) "
        "VALUES (?, 'llm_call', 'grok-4-1-fast', 100, 50, ?)",
        (user_id, "2020-01-01T10:00:00"),
    )
    await db.conn().commit()

    today = db.now_iso()[:10]
    assert await db.get_llm_cost_breakdown(today) == {}
    assert await db.get_llm_cost_breakdown("2020-01-01") == {
        "grok-4-1-fast": {"calls": 1, "prompt_tokens": 100, "completion_tokens": 50}
    }


async def test_get_transcription_count(fresh_db, user_id):
    db = fresh_db
    today = db.now_iso()[:10]
    assert await db.get_transcription_count(today) == 0

    await db.log_cost_event(user_id, "transcription", model=config.OPENAI_TRANSCRIBE_MODEL)
    await db.log_cost_event(user_id, "transcription", model=config.OPENAI_TRANSCRIBE_MODEL)
    await db.log_cost_event(user_id, "llm_call", model="grok-4-1-fast", prompt_tokens=1, completion_tokens=1)

    assert await db.get_transcription_count(today) == 2


async def test_prune_old_cost_events_drops_only_stale_rows(fresh_db, user_id):
    db = fresh_db
    old_date = (dt.date.today() - dt.timedelta(days=200)).isoformat() + "T10:00:00"
    await db.conn().execute(
        "INSERT INTO cost_events (user_id, event_type, model, prompt_tokens, completion_tokens, created_at) "
        "VALUES (?, 'llm_call', 'grok-4-1-fast', 1, 1, ?)",
        (user_id, old_date),
    )
    await db.conn().commit()
    await db.log_cost_event(user_id, "llm_call", model="grok-4-1-fast", prompt_tokens=1, completion_tokens=1)

    deleted = await db.prune_old_cost_events(90)

    assert deleted == 1
    today = db.now_iso()[:10]
    assert await db.get_llm_cost_breakdown(today) == {
        "grok-4-1-fast": {"calls": 1, "prompt_tokens": 1, "completion_tokens": 1}
    }


def test_llm_cost_prices_by_model_with_default_fallback():
    breakdown = {
        "grok-4-1-fast": {"calls": 2, "prompt_tokens": 1000, "completion_tokens": 1000},
        "some-unpriced-model": {"calls": 1, "prompt_tokens": 1000, "completion_tokens": 1000},
    }

    cost, calls, tokens = admin_tasks._llm_cost(breakdown)

    inp, out = config.LLM_PRICES_USD_PER_1K["grok-4-1-fast"]
    default_inp, default_out = config.DEFAULT_LLM_PRICE_USD_PER_1K
    expected = (inp + out) + (default_inp + default_out)
    assert cost == pytest.approx(expected)
    assert calls == 3
    assert tokens == 4000


async def test_build_cost_report_includes_llm_and_transcription_lines(fresh_db, user_id):
    db = fresh_db
    today = db.now_iso()[:10]
    await db.log_cost_event(user_id, "llm_call", model="grok-4-1-fast", prompt_tokens=1000, completion_tokens=1000)
    await db.log_cost_event(user_id, "transcription", model=config.OPENAI_TRANSCRIBE_MODEL)

    report = await admin_tasks._build_cost_report(today)

    assert "LLM-вызовов: 1" in report
    assert "grok-4-1-fast: 1" in report
    assert "Голосовых распознано: 1" in report
    assert "Итого расходы" in report


async def test_build_cost_report_omits_transcription_line_when_none(fresh_db, user_id):
    db = fresh_db
    today = db.now_iso()[:10]
    await db.log_cost_event(user_id, "llm_call", model="grok-4-1-fast", prompt_tokens=1, completion_tokens=1)

    report = await admin_tasks._build_cost_report(today)

    assert "Голосовых" not in report
