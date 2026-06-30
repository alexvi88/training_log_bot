import sys
from pathlib import Path

import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402


@pytest_asyncio.fixture
async def fresh_db():
    """A throwaway in-memory DB, fully migrated/seeded like the real one."""
    await db.init_db(":memory:")
    try:
        yield db
    finally:
        await db.close_db()


@pytest_asyncio.fixture
async def user_id(fresh_db):
    row = await fresh_db.get_or_create_user(telegram_id=111, username="tester")
    return row["telegram_id"]
