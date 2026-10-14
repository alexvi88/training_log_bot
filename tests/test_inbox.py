"""«Уведомления» в приложении (inbox.py, api_v1_inbox.py): каждый пуш человеку с
приложением — строкой, даже без баннера; Telegram-only — без строк; число на
иконке из сервера; /v1/inbox — список, курсор, отметка, язык; срок и снос."""

import datetime as dt
from unittest.mock import AsyncMock

import httpx
import pytest

import announcements
import api_v1
import apns
import coach_notify
import config
import db
import engagement
import inbox
import product_metrics
import push_ios
import push_texts
from tests.test_apns import FakeClient, FakeResponse, _configured  # noqa: F401 — autouse-фикстура конфига APNs

pytestmark = pytest.mark.asyncio

USER = 111


async def _app_user(fresh_db, telegram_id: int = USER, lang: str = "ru") -> str:
    """Атлет, входивший в приложение: токен /v1 ставит app_since."""
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    await fresh_db.set_user_lang(telegram_id, lang)
    return await fresh_db.issue_api_token(telegram_id)


def _client(token: str) -> httpx.AsyncClient:
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    client.headers["Authorization"] = f"Bearer {token}"
    return client


def _capture_apns(monkeypatch) -> FakeClient:
    client = FakeClient(FakeResponse(200))

    async def get_client():
        return client

    monkeypatch.setattr(apns, "_get_client", get_client)
    monkeypatch.setattr(apns, "_provider_token_jwt", lambda: "jwt")
    return client


async def _rows(user_id: int = USER) -> list:
    cur = await db.conn().execute("SELECT * FROM inbox WHERE user_id = ? ORDER BY id", (user_id,))
    return await cur.fetchall()


# ---------- кому пишем ----------


async def test_telegram_only_user_gets_no_inbox_rows(fresh_db, monkeypatch):
    await fresh_db.get_or_create_user(telegram_id=USER, username="tg")
    monkeypatch.setattr(apns, "send_alert", AsyncMock(return_value=True))

    delivery = await inbox.deliver(USER, "skip_7", "T", "B", lang="ru")

    assert delivery.inbox_id is None
    assert await _rows() == []
    assert not await fresh_db.is_app_user(USER)


async def test_app_user_gets_a_row_without_apns_or_token(fresh_db, monkeypatch):
    await _app_user(fresh_db)
    monkeypatch.setattr(apns, "is_configured", lambda: False)

    delivery = await inbox.deliver(USER, "skip_7", "Т", "Б", lang="ru", route={"screen": "workout"})

    assert delivery.inbox_id is not None and delivery.accepted == 0
    (row,) = await _rows()
    assert (row["category"], row["title"], row["body"], row["lang"]) == ("skip_7", "Т", "Б", "ru")
    assert row["read_at"] is None


async def test_app_since_survives_logout(fresh_db):
    await _app_user(fresh_db)
    await fresh_db.revoke_api_token(USER)
    assert await fresh_db.is_app_user(USER)


async def test_device_token_or_app_only_account_counts_as_app(fresh_db):
    await fresh_db.get_or_create_user(telegram_id=222, username="old")
    await fresh_db.register_push_token(222, "ios", "dev")
    assert await fresh_db.is_app_user(222)
    app = await fresh_db.create_app_only_user()
    assert await fresh_db.is_app_user(app["telegram_id"])


# ---------- баннер: inbox_id и число на иконке ----------


async def test_payload_carries_inbox_id_and_server_counted_badge(fresh_db, monkeypatch):
    await _app_user(fresh_db)
    await fresh_db.register_push_token(USER, "ios", "dev")
    client = _capture_apns(monkeypatch)

    first = await inbox.deliver(USER, "skip_3", "T", "B", lang="ru")
    second = await inbox.deliver(USER, "skip_5", "T", "B", lang="ru")

    payloads = [c["json"] for c in client.calls]
    assert [p["inbox_id"] for p in payloads] == [first.inbox_id, second.inbox_id]
    assert [p["aps"]["badge"] for p in payloads] == [1, 2]

    await fresh_db.mark_inbox_read(USER)
    await inbox.deliver(USER, "skip_7", "T", "B", lang="ru")
    assert client.calls[-1]["json"]["aps"]["badge"] == 1


async def test_send_alert_without_badge_leaves_icon_alone(monkeypatch):
    client = _capture_apns(monkeypatch)
    await apns.send_alert(1, "dev", "T", "B", category="admin_test")
    payload = client.calls[0]["json"]
    assert "badge" not in payload["aps"] and "inbox_id" not in payload


async def test_push_false_writes_row_without_banner(fresh_db, monkeypatch):
    await _app_user(fresh_db)
    await fresh_db.register_push_token(USER, "ios", "dev")
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(apns, "send_alert", send)

    await inbox.deliver(USER, "skip_7", "T", "B", lang="ru", push=False)

    send.assert_not_awaited()
    assert len(await _rows()) == 1


async def test_failed_delivery_still_leaves_the_row(fresh_db, monkeypatch):
    await _app_user(fresh_db)
    await fresh_db.register_push_token(USER, "ios", "dev")
    monkeypatch.setattr(apns, "send_alert", AsyncMock(return_value=False))

    delivery = await inbox.deliver(USER, "skip_7", "T", "B", lang="ru")

    assert delivery.accepted == 0 and len(await _rows()) == 1


# ---------- отправители ----------


async def test_engagement_push_is_one_row_for_telegram_plus_apns(fresh_db, monkeypatch):
    await _app_user(fresh_db)
    await fresh_db.register_push_token(USER, "ios", "dev")
    client = _capture_apns(monkeypatch)
    telegram = []

    async def fake_photo(bot, telegram_id, decision, kb):
        telegram.append(telegram_id)
        raise engagement.TelegramAPIError(method=None, message="boom")

    monkeypatch.setattr(engagement, "_send_push_photo", fake_photo)
    decision = engagement.PushDecision(push_texts.WIN_BACK, "текст")

    await engagement._deliver(None, USER, decision, dt.date(2026, 10, 14))

    assert telegram == [USER] and len(client.calls) == 1
    (row,) = await _rows()
    assert row["category"] == push_texts.WIN_BACK
    assert client.calls[0]["json"]["inbox_id"] == row["id"]


async def test_inbox_only_delivery_skips_telegram_and_banner(fresh_db, monkeypatch):
    await _app_user(fresh_db)
    await fresh_db.register_push_token(USER, "ios", "dev")
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(apns, "send_alert", send)

    async def no_photo(*_args):
        raise AssertionError("в Telegram выключившему пуши не пишем")

    monkeypatch.setattr(engagement, "_send_push_photo", no_photo)
    decision = engagement.PushDecision(push_texts.WIN_BACK, "текст")

    await engagement._deliver(None, USER, decision, dt.date(2026, 10, 14), inbox_only=True)

    send.assert_not_awaited()
    assert len(await _rows()) == 1
    assert await fresh_db.has_push_today(USER, "2026-10-14")


async def test_inbox_only_pool_is_app_users_with_pushes_off(fresh_db):
    await _app_user(fresh_db)
    await fresh_db.get_or_create_user(telegram_id=222, username="tg")
    for uid in (USER, 222):
        await fresh_db.update_user(uid, pushes_enabled=0)

    newbies = await fresh_db.list_newbie_user_ids(inbox_only=True)
    assert [r[0] for r in newbies] == [USER]
    assert await fresh_db.list_newbie_user_ids() == []


async def test_inbox_only_digest_does_not_call_the_model(fresh_db, monkeypatch):
    called = AsyncMock(return_value="AI")
    monkeypatch.setattr(engagement, "_ai_weekly_digest_text", called)
    await engagement.build_daily_push(USER, dt.date(2026, 10, 11), allow_ai=False)
    called.assert_not_awaited()


async def test_coach_comment_lands_in_inbox_without_apns(fresh_db, monkeypatch):
    await _app_user(fresh_db, lang="en")
    monkeypatch.setattr(apns, "is_configured", lambda: False)
    client = await fresh_db.get_user(USER)

    await coach_notify.push_ios_comment(client, "Masha", "Nice depth", 5)

    (row,) = await _rows()
    assert row["category"] == push_ios.COACH_COMMENT and row["lang"] == "en"
    assert "Masha" in row["body"]
    assert '"workout_id": 5' in row["route"]


async def test_announcement_after_telegram_is_a_row_without_banner(fresh_db, monkeypatch):
    await _app_user(fresh_db)
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(apns, "send_alert", send)

    class Bot:
        async def send_message(self, **kwargs):
            return type("M", (), {"message_id": 1})()

        async def send_photo(self, **kwargs):
            return type("M", (), {"message_id": 1, "photo": []})()

    ann = announcements.Announcement(key="test_inbox_ann", text="Новое", buttons=[])
    await announcements.send_announcement(Bot(), ann)

    send.assert_not_awaited()
    (row,) = await _rows()
    assert row["category"] == push_ios.ANNOUNCEMENT


# ---------- /v1 ----------


async def test_v1_inbox_list_page_read_and_language(fresh_db, monkeypatch):
    token = await _app_user(fresh_db)
    monkeypatch.setattr(apns, "is_configured", lambda: False)
    ids = [
        (await inbox.deliver(USER, "skip_7", f"T{i}", "B", lang="ru", route={"screen": "workout"})).inbox_id
        for i in range(3)
    ]
    # Строка на другом языке (атлет сменил язык после пуша) — не отдаётся.
    await fresh_db.add_inbox_item(USER, "skip_7", "Old", "English", None, "en")
    client = _client(token)

    body = (await client.get("/inbox")).json()
    assert [i["id"] for i in body["items"]] == ids[::-1]
    assert body["unread"] == 3 and body["next_before"] is None
    assert body["items"][0]["route"] == {"screen": "workout"}
    assert body["items"][0]["read"] is False

    page = (await client.get("/inbox?limit=2")).json()
    assert [i["id"] for i in page["items"]] == [ids[2], ids[1]] and page["next_before"] == ids[1]
    rest = (await client.get(f"/inbox?limit=2&before={ids[1]}")).json()
    assert [i["id"] for i in rest["items"]] == [ids[0]]

    resp = await client.post("/inbox/read", json={"id": ids[0]})
    assert resp.json() == {"marked": 1, "unread": 2}
    assert (await client.get("/me")).json()["inbox_unread"] == 2
    assert (await client.post("/inbox/read", json={"id": 999999})).json()["marked"] == 0
    # «Прочитать всё» гасит и спрятанную строку другого языка.
    assert (await client.post("/inbox/read", json={"all": True})).json() == {"marked": 3, "unread": 0}

    bad = await client.post("/inbox/read", json={})
    assert bad.status_code == 400
    assert "прочитанным" in bad.json()["message"]


async def test_someone_elses_row_is_not_marked(fresh_db, monkeypatch):
    await _app_user(fresh_db)
    other_token = await _app_user(fresh_db, telegram_id=222)
    monkeypatch.setattr(apns, "is_configured", lambda: False)
    mine = (await inbox.deliver(USER, "skip_7", "T", "B", lang="ru")).inbox_id

    resp = await _client(other_token).post("/inbox/read", json={"id": mine})

    assert resp.json()["marked"] == 0
    assert (await _rows())[0]["read_at"] is None


async def test_opening_support_thread_reads_support_rows(fresh_db, monkeypatch):
    token = await _app_user(fresh_db)
    monkeypatch.setattr(apns, "is_configured", lambda: False)
    import api_v1_support

    await api_v1_support.send_admin_reply(USER, "Починил")
    await inbox.deliver(USER, "skip_7", "T", "B", lang="ru")

    await _client(token).post("/support/read")

    rows = {r["category"]: r["read_at"] for r in await _rows()}
    assert rows["support"] is not None and rows["skip_7"] is None


# ---------- срок, снос, аналитика ----------


async def test_retention_prunes_old_rows(fresh_db, monkeypatch):
    await _app_user(fresh_db)
    monkeypatch.setattr(apns, "is_configured", lambda: False)
    await inbox.deliver(USER, "skip_7", "T", "B", lang="ru")
    old = (db.utc_now_naive() - dt.timedelta(days=config.INBOX_RETENTION_DAYS + 1)).isoformat(timespec="seconds")
    await db.conn().execute(
        "INSERT INTO inbox (user_id, category, title, body, lang, created_at) VALUES (?, 'skip_3', 'T', 'B', 'ru', ?)",
        (USER, old),
    )
    await db.conn().commit()

    assert await fresh_db.prune_old_inbox(config.INBOX_RETENTION_DAYS) == 1
    assert [r["category"] for r in await _rows()] == ["skip_7"]


async def test_account_deletion_wipes_inbox(fresh_db, monkeypatch):
    await _app_user(fresh_db)
    monkeypatch.setattr(apns, "is_configured", lambda: False)
    await inbox.deliver(USER, "skip_7", "T", "B", lang="ru")

    await fresh_db.wipe_user_account(USER)

    assert await _rows() == []
    assert "inbox" not in await fresh_db.user_data_left(USER)


def test_inbox_events_carry_category_and_never_text():
    assert product_metrics.clean_props("inbox_open", {"text": "x"}) == {}
    assert product_metrics.clean_props(
        "inbox_item_open", {"category": "skip_7", "title": "ПРИВЕТ"}
    ) == {"category": "skip_7"}
