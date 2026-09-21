"""db.push_tokens: one live device token maps to at most one account.

A physical device keeps the same APNs token across a logout/login with a
different account (reinstall gets a new token, but a plain account switch on
the same install does not). register_push_token keys its upsert on
(user_id, platform), so nothing here stopped the SAME device_token from
sitting under two different user_id rows at once — and engagement.py/
announcements.py resolve "who gets this token's banner" per user_id, so both
accounts would get pushed to the one physical device, including onto the
account that logged out.
"""

import pytest

pytestmark = pytest.mark.asyncio


async def test_registering_a_token_for_a_new_account_detaches_the_old_owner(fresh_db):
    user_a = (await fresh_db.get_or_create_user(telegram_id=201, username="a"))["telegram_id"]
    user_b = (await fresh_db.get_or_create_user(telegram_id=202, username="b"))["telegram_id"]

    await fresh_db.register_push_token(user_a, "ios", "shared-device-token")
    # Same phone, different account logs in — same APNs token re-registers.
    await fresh_db.register_push_token(user_b, "ios", "shared-device-token")

    cur = await fresh_db.conn().execute(
        "SELECT user_id FROM push_tokens WHERE device_token = ? AND platform = 'ios'",
        ("shared-device-token",),
    )
    rows = await cur.fetchall()
    owners = {r["user_id"] for r in rows}
    assert owners == {user_b}, (
        f"token must belong to exactly the new owner, got {owners} — "
        "the old account must not keep receiving this device's pushes"
    )
