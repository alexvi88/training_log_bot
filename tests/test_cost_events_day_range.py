"""cost_events day queries: date(created_at) = ? was replaced with a
created_at >= start AND created_at < end range so idx_cost_events_created
actually gets used. Same rows must count either way, including at the
midnight boundary."""
import pytest

pytestmark = pytest.mark.asyncio


async def test_get_llm_cost_breakdown_range_matches_exact_day_boundaries(fresh_db, user_id):
    """date(created_at) = ? and the created_at >= start AND < end range it was
    replaced with must agree on which rows count as "that day", including
    right at midnight."""
    db = fresh_db
    await db.conn().execute(
        "INSERT INTO cost_events (user_id, event_type, model, prompt_tokens, completion_tokens, created_at) "
        "VALUES (?, 'llm_call', 'grok-4-1-fast', 1, 1, '2026-03-01T00:00:00')",
        (user_id,),
    )
    await db.conn().execute(
        "INSERT INTO cost_events (user_id, event_type, model, prompt_tokens, completion_tokens, created_at) "
        "VALUES (?, 'llm_call', 'grok-4-1-fast', 1, 1, '2026-03-01T23:59:59')",
        (user_id,),
    )
    await db.conn().execute(
        "INSERT INTO cost_events (user_id, event_type, model, prompt_tokens, completion_tokens, created_at) "
        "VALUES (?, 'llm_call', 'grok-4-1-fast', 1, 1, '2026-03-02T00:00:00')",
        (user_id,),
    )
    await db.conn().commit()

    breakdown = await db.get_llm_cost_breakdown("2026-03-01")

    assert breakdown["grok-4-1-fast"]["calls"] == 2


async def test_get_transcription_count_range_excludes_next_day(fresh_db, user_id):
    db = fresh_db
    await db.conn().execute(
        "INSERT INTO cost_events (user_id, event_type, model, prompt_tokens, completion_tokens, created_at) "
        "VALUES (?, 'transcription', 'whisper', 0, 0, '2026-03-01T12:00:00')",
        (user_id,),
    )
    await db.conn().execute(
        "INSERT INTO cost_events (user_id, event_type, model, prompt_tokens, completion_tokens, created_at) "
        "VALUES (?, 'transcription', 'whisper', 0, 0, '2026-03-02T00:00:01')",
        (user_id,),
    )
    await db.conn().commit()

    assert await db.get_transcription_count("2026-03-01") == 1
    assert await db.get_transcription_count("2026-03-02") == 1


async def test_get_cost_total_usd_range_matches_a_specific_day(fresh_db, user_id):
    db = fresh_db
    await db.conn().execute(
        "INSERT INTO cost_events (user_id, event_type, model, prompt_tokens, completion_tokens, created_at) "
        "VALUES (?, 'transcription', 'whisper', 0, 0, '2026-03-01T12:00:00')",
        (user_id,),
    )
    await db.conn().execute(
        "INSERT INTO cost_events (user_id, event_type, model, prompt_tokens, completion_tokens, created_at) "
        "VALUES (?, 'transcription', 'whisper', 0, 0, '2026-03-02T12:00:00')",
        (user_id,),
    )
    await db.conn().commit()

    import config

    total = await db.get_cost_total_usd("2026-03-01")
    assert total == pytest.approx(config.TRANSCRIPTION_PRICE_USD_PER_CALL)
