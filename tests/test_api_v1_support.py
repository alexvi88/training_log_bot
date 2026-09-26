"""Переписка с поддержкой (api_v1_support.py): ветка атлета, входящие админа,
счётчики непрочитанного в /me, старый /feedback, реплай админа в Telegram."""

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import api_v1
import api_v1_feedback
import apns
import config
from handlers import admin

pytestmark = pytest.mark.asyncio

ADMIN_ID = 999
USER_ID = 111
PNG = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d49444154789c6360000002000100e221bc330000000049454e44ae426082"
    )
).decode()


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    monkeypatch.setattr(api_v1_feedback, "_daily_counts", {})


class FakeTelegram:
    """Подмена отправки админу: запоминает, что ушло, и выдаёт message_id."""

    def __init__(self):
        self.sent: list[tuple[int, str, bytes | None]] = []
        self.next_id = 500

    async def __call__(self, user_id, text, photo):
        self.sent.append((user_id, text, photo))
        self.next_id += 2
        return self.next_id - 1, (self.next_id if photo is not None else None)


@pytest.fixture
def telegram(monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setattr(api_v1_feedback, "_send_feedback_to_admin", fake)
    return fake


@pytest.fixture
def pushes(monkeypatch):
    monkeypatch.setattr(apns, "is_configured", lambda: True)
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(apns, "send_alert", send)
    return send


async def _client(fresh_db, telegram_id: int, username: str | None = "tester", lang: str = "ru"):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username=username)
    await fresh_db.set_user_lang(telegram_id, lang)
    token = await fresh_db.issue_api_token(telegram_id)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    client.headers["Authorization"] = f"Bearer {token}"
    return client


# ---------- атлет ----------


async def test_user_thread_roundtrip_and_unread(fresh_db, telegram, pushes):
    user = await _client(fresh_db, USER_ID)
    admin_client = await _client(fresh_db, ADMIN_ID, username="owner")

    resp = await user.post("/support/messages", json={"text": "  Не сохраняется подход  "})
    assert resp.status_code == 201, resp.text
    msg = resp.json()["message"]
    assert msg["from"] == "user" and msg["text"] == "Не сохраняется подход"
    assert msg["photo_url"] is None and msg["created_at"]
    assert telegram.sent[0][0] == USER_ID

    # Админу — банер с маршрутом в ветку этого атлета (токена у админа пока нет).
    pushes.assert_not_awaited()

    me = (await admin_client.get("/me")).json()
    assert me["is_support_admin"] is True and me["support_unread"] == 1
    me_user = (await user.get("/me")).json()
    assert me_user["is_support_admin"] is False and me_user["support_unread"] == 0

    resp = await admin_client.post(f"/support/threads/{USER_ID}/messages", json={"text": "Починил, обнови"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["message"]["from"] == "support"

    body = (await user.get("/support/messages")).json()
    assert [m["from"] for m in body["messages"]] == ["user", "support"]
    assert body["unread"] == 1
    assert (await user.get("/me")).json()["support_unread"] == 1

    resp = await user.post("/support/read")
    assert resp.json()["marked"] == 1
    assert (await user.get("/support/messages")).json()["unread"] == 0

    # Админ прочитал ветку — у него счётчик обнуляется.
    assert (await admin_client.post(f"/support/threads/{USER_ID}/read")).json()["marked"] == 1
    assert (await admin_client.get("/me")).json()["support_unread"] == 0


async def test_admin_gets_push_with_thread_route(fresh_db, telegram, pushes):
    user = await _client(fresh_db, USER_ID)
    await fresh_db.get_or_create_user(telegram_id=ADMIN_ID, username="owner")
    await fresh_db.register_push_token(ADMIN_ID, "ios", "admintok")

    await user.post("/support/messages", json={"text": "Привет"})

    args, kwargs = pushes.await_args
    assert args[:3] == (ADMIN_ID, "admintok", "Новое сообщение в поддержку")
    assert str(USER_ID) in args[3] and "Привет" in args[3]
    assert kwargs["route"] == {"screen": "support_thread", "user_id": USER_ID}


async def test_admin_reply_pushes_user_in_their_language(fresh_db, telegram, pushes):
    await _client(fresh_db, USER_ID, lang="en")
    await fresh_db.register_push_token(USER_ID, "ios", "usertok")
    admin_client = await _client(fresh_db, ADMIN_ID, username="owner")

    resp = await admin_client.post(f"/support/threads/{USER_ID}/messages", json={"text": "Fixed it " + "x" * 200})
    assert resp.status_code == 201

    args, kwargs = pushes.await_args
    assert args[:3] == (USER_ID, "usertok", "Support")
    assert args[3].startswith("Reply from support: Fixed it")
    assert len(args[3]) <= 110
    assert kwargs["route"] == {"screen": "support"}


async def test_photo_is_stored_and_served_to_owner_and_admin_only(fresh_db, telegram, pushes):
    user = await _client(fresh_db, USER_ID)
    stranger = await _client(fresh_db, 222, username="other")
    admin_client = await _client(fresh_db, ADMIN_ID, username="owner")

    resp = await user.post(
        "/support/messages", json={"text": "Скрин", "image_data_url": f"data:image/png;base64,{PNG}"}
    )
    assert resp.status_code == 201, resp.text
    photo_url = resp.json()["message"]["photo_url"]
    assert photo_url.startswith("/support/photos/")
    # Фото ушло админу и в Telegram.
    assert telegram.sent[0][2] == base64.b64decode(PNG)

    assert (await user.get(photo_url)).status_code == 200
    assert (await admin_client.get(photo_url)).content == base64.b64decode(PNG)
    assert (await stranger.get(photo_url)).status_code == 404


async def test_user_message_uses_feedback_validation(fresh_db, telegram):
    user = await _client(fresh_db, USER_ID)
    resp = await user.post("/support/messages", json={"text": "  "})
    assert resp.status_code == 400 and resp.json()["error"] == "bad_request"
    resp = await user.post("/support/messages", json={"text": "a" * (api_v1_feedback.MAX_FEEDBACK_LENGTH + 1)})
    assert resp.status_code == 400
    for _ in range(api_v1_feedback.FEEDBACK_DAILY_LIMIT):
        assert (await user.post("/support/messages", json={"text": "ещё"})).status_code == 201
    resp = await user.post("/support/messages", json={"text": "ещё"})
    assert resp.status_code == 429 and resp.json()["error"] == "feedback_limit_exceeded"


async def test_delivery_failure_rolls_back_the_message(fresh_db, monkeypatch):
    async def boom(user_id, text, photo):
        raise RuntimeError("telegram is down")

    monkeypatch.setattr(api_v1_feedback, "_send_feedback_to_admin", boom)
    user = await _client(fresh_db, USER_ID)
    resp = await user.post("/support/messages", json={"text": "Привет"})
    assert resp.status_code == 503 and resp.json()["error"] == "delivery_failed"
    assert (await user.get("/support/messages")).json()["messages"] == []


async def test_no_admin_configured_is_503(fresh_db, telegram, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", None)
    user = await _client(fresh_db, USER_ID)
    resp = await user.post("/support/messages", json={"text": "Привет"})
    assert resp.status_code == 503 and resp.json()["error"] == "not_configured"


# ---------- /feedback для старых сборок ----------


async def test_feedback_still_answers_delivered_and_lands_in_thread(fresh_db, telegram):
    user = await _client(fresh_db, USER_ID)
    resp = await user.post("/feedback", json={"text": "Старая сборка"})
    assert resp.status_code == 201
    assert resp.json() == {"delivered": True}

    messages = (await user.get("/support/messages")).json()["messages"]
    assert [(m["from"], m["text"]) for m in messages] == [("user", "Старая сборка")]
    assert await fresh_db.support_user_by_tg_message(501) == USER_ID


# ---------- доступ админа ----------


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/support/threads"),
        ("GET", f"/support/threads/{USER_ID}/messages"),
        ("POST", f"/support/threads/{USER_ID}/messages"),
        ("POST", f"/support/threads/{USER_ID}/read"),
    ],
)
async def test_admin_routes_are_forbidden_for_everyone_else(fresh_db, method, path):
    user = await _client(fresh_db, USER_ID)
    resp = await user.request(method, path, json={"text": "hi"} if method == "POST" else None)
    assert resp.status_code == 403
    assert resp.json()["error"] == "forbidden"
    assert resp.json()["message"] == "Сюда доступ только у поддержки."


async def test_admin_routes_require_auth(fresh_db):
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    assert (await client.get("/support/threads")).status_code == 401


async def test_threads_unread_first_then_latest(fresh_db, telegram, pushes):
    a = await _client(fresh_db, 201, username="alpha")
    b = await _client(fresh_db, 202, username=None)
    c = await _client(fresh_db, 203, username="gamma")
    admin_client = await _client(fresh_db, ADMIN_ID, username="owner")

    await a.post("/support/messages", json={"text": "a1"})
    await b.post("/support/messages", json={"text": "b1"})
    await c.post("/support/messages", json={"text": "c1"})
    # Ветку «b» админ прочитал и ответил — непрочитанного в ней нет, хоть она и свежее «a».
    await admin_client.post("/support/threads/202/read")
    await admin_client.post("/support/threads/202/messages", json={"text": "ответ b"})

    threads = (await admin_client.get("/support/threads")).json()["threads"]
    assert [t["user_id"] for t in threads] == [203, 201, 202]
    by_id = {t["user_id"]: t for t in threads}
    assert by_id[201] == {
        "user_id": 201, "name": "alpha", "last_text": "a1", "last_from": "user",
        "last_at": by_id[201]["last_at"], "unread": 1,
    }
    assert by_id[202]["name"] is None
    assert by_id[202]["last_from"] == "support" and by_id[202]["last_text"] == "ответ b"
    assert by_id[202]["unread"] == 0

    messages = (await admin_client.get("/support/threads/202/messages")).json()["messages"]
    assert [m["text"] for m in messages] == ["b1", "ответ b"]


async def test_app_only_negative_user_id_thread(fresh_db, telegram, pushes):
    app_user = await fresh_db.create_app_only_user(language_code="ru")
    uid = app_user["telegram_id"]
    assert uid < 0
    admin_client = await _client(fresh_db, ADMIN_ID, username="owner")
    resp = await admin_client.post(f"/support/threads/{uid}/messages", json={"text": "Привет"})
    assert resp.status_code == 201
    assert len((await admin_client.get(f"/support/threads/{uid}/messages")).json()["messages"]) == 1


async def test_reply_to_unknown_user_is_404(fresh_db):
    admin_client = await _client(fresh_db, ADMIN_ID, username="owner")
    resp = await admin_client.post("/support/threads/4242/messages", json={"text": "Привет"})
    assert resp.status_code == 404
    resp = await admin_client.get("/support/threads/abc/messages")
    assert resp.status_code == 404


# ---------- реплай админа в Telegram ----------


def _admin_reply(replied_id: int, text: str | None, replied_text: str | None = None, sender: int = ADMIN_ID):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=sender, username="owner", language_code=None)
    message.text = text
    message.caption = None
    message.reply_to_message = SimpleNamespace(message_id=replied_id, text=replied_text)
    message.reply = AsyncMock()
    return message


async def test_telegram_reply_maps_to_user_thread(fresh_db, telegram, pushes):
    user = await _client(fresh_db, USER_ID)
    await fresh_db.register_push_token(USER_ID, "ios", "usertok")
    await user.post("/support/messages", json={"text": "Помоги"})
    tg_id = telegram.next_id - 1

    message = _admin_reply(tg_id, "Уже смотрю")
    target = await admin._support_reply_target(message)
    assert target == {"support_user_id": USER_ID}
    await admin.support_reply(message, **target)

    body = (await user.get("/support/messages")).json()
    assert [(m["from"], m["text"]) for m in body["messages"]] == [("user", "Помоги"), ("support", "Уже смотрю")]
    assert body["unread"] == 1
    assert pushes.await_args.args[:2] == (USER_ID, "usertok")
    assert message.reply.await_args.args[0].startswith("✅")


async def test_telegram_reply_to_old_feedback_uses_header(fresh_db, pushes):
    await fresh_db.get_or_create_user(telegram_id=USER_ID, username="tester")
    old = f"📱 Фидбек из приложения от id {USER_ID}:\n\nстарый отзыв"
    message = _admin_reply(77, "Ответ на старое", replied_text=old)
    assert await admin._support_reply_target(message) == {"support_user_id": USER_ID}


async def test_telegram_reply_to_other_messages_passes_through(fresh_db):
    # Отзыв из бота (copy_to) и любые другие сообщения — не наша ветка.
    bot_feedback = _admin_reply(55, "ок", replied_text="📬 Фидбек от @x (id 5):")
    assert await admin._support_reply_target(bot_feedback) is False
    # Не реплай вовсе.
    plain = _admin_reply(55, "ок")
    plain.reply_to_message = None
    assert await admin._support_reply_target(plain) is False
    # Реплай не от админа.
    stranger = _admin_reply(55, "ок", replied_text=f"📱 Фидбек из приложения от id {USER_ID}:", sender=5)
    assert await admin._support_reply_target(stranger) is False
    # Команда реплаем — это команда, а не ответ.
    command = _admin_reply(55, "/testpush", replied_text=f"📱 Фидбек из приложения от id {USER_ID}:")
    assert await admin._support_reply_target(command) is False


async def test_admin_text_to_telegram_fits_message_limit():
    text = api_v1_feedback._admin_text(USER_ID, "я" * api_v1_feedback.MAX_FEEDBACK_LENGTH)
    assert len(text) <= 4096
    assert api_v1_feedback.ADMIN_HEADER_RE.match(text).group(1) == str(USER_ID)


async def test_account_deletion_removes_support_rows_and_photos(fresh_db, telegram, pushes, tmp_path):
    user = await _client(fresh_db, USER_ID)
    await user.post("/support/messages", json={"text": "Скрин", "image_data_url": f"data:image/png;base64,{PNG}"})
    folder = tmp_path / "support_media"
    assert any(folder.iterdir())
    await fresh_db.wipe_user_account(USER_ID)
    assert not any(folder.iterdir())
    assert await fresh_db.list_support_messages(USER_ID) == []
