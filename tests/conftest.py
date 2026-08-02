import asyncio
import sys
from pathlib import Path

import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402


@pytest_asyncio.fixture
async def fresh_db():
    """A throwaway in-memory DB, fully migrated/seeded like the real one."""
    # db._write_lock is created at import time and binds itself to the loop of
    # the first *contended* acquire. The bot runs one loop for its lifetime, but
    # every test gets a fresh one — so a test that actually exercises
    # concurrency would bind the lock and make the next such test fail with
    # "bound to a different event loop". A new lock per test keeps that
    # module-level state from leaking across loops.
    db._write_lock = asyncio.Lock()
    await db.init_db(":memory:")
    try:
        yield db
    finally:
        await db.close_db()


@pytest_asyncio.fixture
async def user_id(fresh_db):
    row = await fresh_db.get_or_create_user(telegram_id=111, username="tester")
    return row["telegram_id"]
