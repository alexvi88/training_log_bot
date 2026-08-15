"""db.get_or_create_user — the check-then-insert race between two concurrent
first-touches (aiogram processes updates concurrently, so a doubled /start
could hit this)."""
import asyncio

import pytest

pytestmark = pytest.mark.asyncio


async def test_get_or_create_user_is_race_safe(fresh_db):
    """Two concurrent first-touches must not both try to INSERT — the loser
    used to die with an IntegrityError on the telegram_id unique constraint
    instead of getting back the row the winner created."""
    db = fresh_db
    telegram_id = 999999

    results = await asyncio.gather(
        db.get_or_create_user(telegram_id, "alice"),
        db.get_or_create_user(telegram_id, "alice"),
    )

    assert results[0]["telegram_id"] == telegram_id
    assert results[1]["telegram_id"] == telegram_id
    cur = await db.conn().execute(
        "SELECT COUNT(*) AS n FROM users WHERE telegram_id = ?", (telegram_id,)
    )
    assert (await cur.fetchone())["n"] == 1
