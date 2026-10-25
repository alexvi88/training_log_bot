"""Края входа и сноса аккаунта, которые не исполнялись.

- Заявка на согласие (OAuth коннектора) истекла, пока человек набирал код:
  отказ «заявка устарела», и при этом код связывания НЕ сгорает и попытка не
  засчитывается — иначе человек, пришедший с правильным кодом на старую
  вкладку, терял бы и код, и попытку;
- снос аккаунта с хранилищем диалога, которое не умеет `drop_user`
  (MemoryStorage, чужое): база всё равно стёрта целиком, а не «упало посреди».
"""

import time

import pytest
from aiogram.fsm.storage.memory import MemoryStorage

import account_deletion

pytestmark = pytest.mark.asyncio


async def _consent_request(db, request_id, expires_at):
    await db.create_oauth_consent_request(
        request_id, "client-1", "https://claude.ai/api/mcp/auth_callback", True,
        "challenge", "[]", None, None, expires_at,
    )


async def test_expired_consent_request_keeps_the_link_code_and_attempts(fresh_db, user_id):
    db = fresh_db
    code = await db.issue_oauth_link_code(user_id, ttl_seconds=600, digits=6)
    await _consent_request(db, "stale", time.time() - 1)
    await _consent_request(db, "fresh", time.time() + 600)

    assert await db.verify_oauth_link_code("stale", code, 5) == ("expired_request", None)

    cur = await db.conn().execute("SELECT attempts FROM oauth_consent_requests WHERE request_id = 'stale'")
    assert (await cur.fetchone())["attempts"] == 0
    # Тот же код на свежей заявке работает — он не сгорел на просроченной.
    assert await db.verify_oauth_link_code("fresh", code, 5) == ("ok", user_id)
    # И гасится после успешного входа: второй раз не пройдёт.
    await _consent_request(db, "again", time.time() + 600)
    assert (await db.verify_oauth_link_code("again", code, 5))[0] == "bad_code"


async def test_deletion_with_storage_without_drop_user_still_wipes_everything(fresh_db, user_id, caplog):
    db = fresh_db
    ex = await db.create_exercise(user_id, "Жим", await db.create_muscle_group(user_id, "Грудь"))
    workout = await db.create_workout(user_id)
    block = await db.create_block(workout, "single")
    await db.add_block_exercise(block, ex, 0)
    await db.add_set(block, ex, 0, 0, 80.0, 8, None)

    left = await account_deletion.delete_account(user_id, MemoryStorage())

    assert left == {}
    assert await db.get_user(user_id) is None
    # Несделанное не замалчивается: в логе причина, по которой диалог остался.
    assert any("drop_user" in r.getMessage() for r in caplog.records)
