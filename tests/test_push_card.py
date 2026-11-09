"""«Пуш как мем» (push_card.py): чем можно делиться, что уезжает в PNG,
кнопка под пушем в Telegram и её обработчик, `shareable` и картинка в
«Уведомлениях» (`GET /v1/inbox/{id}/card.png`)."""

from __future__ import annotations

import io
import re
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from PIL import Image

import api_v1
import finish_slides as fs
import i18n
import keyboards
import push_card
import push_texts
from handlers import push_share

CYRILLIC = re.compile(r"[А-Яа-яЁё]")
LANGS = ("ru", "en", "es", "pt", "pt-PT", "de", "fr", "tr")


def setup_function(_):
    i18n.reload()


def test_only_skip_pushes_are_shareable():
    for category in (push_texts.SKIP_3, push_texts.SKIP_7, push_texts.SKIP_14, push_texts.WIN_BACK):
        assert push_card.shareable(category)
    # Подколки — только про пропуски: рекорды, звание и итоги — своими карточками.
    for category in (push_texts.RANK_NEAR, push_texts.PLATEAU, push_texts.STREAK_MILESTONE,
                     push_texts.WEEKLY_DIGEST, push_texts.YEAR_REVIEW, None):
        assert not push_card.shareable(category)


def test_long_text_is_clipped_on_a_word():
    card = push_card.assemble("слово " * 200)
    assert len(card.body) <= push_card.MAX_BODY_CHARS + 1 and card.body.endswith("…")


@pytest.mark.parametrize("lang", LANGS)
def test_every_language_card_renders_with_glyphs(lang):
    body = i18n.t_in(lang, "push.skip_7.1")
    with i18n.use_lang(lang):
        card = push_card.assemble(body)
        signature = fs.current_signature()
    for text in push_card.card_texts(card, signature):
        assert not fs.missing_glyphs(text), (lang, text)
        if lang != "ru":
            assert not CYRILLIC.search(text), (lang, text)
    png = push_card.render(card, signature, lang)
    assert Image.open(io.BytesIO(png)).size == (fs.WIDTH, fs.HEIGHT)


def test_share_button_only_under_shareable_pushes():
    with i18n.use_lang("ru"):
        plain = keyboards.push_cta_keyboard("push.cta.default")
        shared = keyboards.push_cta_keyboard("push.cta.default", shareable=True)
    callbacks = lambda kb: [b.callback_data for row in kb.inline_keyboard for b in row]  # noqa: E731
    assert keyboards.PUSH_CARD_CALLBACK not in callbacks(plain)
    assert callbacks(shared)[-1] == keyboards.PUSH_CARD_CALLBACK


async def test_bot_handler_sends_card_and_share_link(fresh_db, monkeypatch):
    await fresh_db.get_or_create_user(telegram_id=111, username="tester")
    monkeypatch.setattr(push_share.sharing, "get_bot_username", AsyncMock(return_value="test_bot"))
    message = MagicMock()
    message.caption = "ПРИВЕТ АТЛЕТ! Третий день без зала."
    message.answer_photo = AsyncMock()
    message.answer = AsyncMock()
    callback = MagicMock()
    callback.message = message
    callback.from_user.id = 111
    callback.answer = AsyncMock()
    with i18n.use_lang("ru"):
        await push_share.cb_share_push(callback)
    message.answer_photo.assert_awaited_once()
    url = message.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0].url
    assert "ref_111" in url


async def test_bot_handler_without_text_says_so(fresh_db):
    message = MagicMock(caption=None, text=None)
    message.answer = AsyncMock()
    message.answer_photo = AsyncMock()
    callback = MagicMock(message=message)
    callback.answer = AsyncMock()
    with i18n.use_lang("en"):
        await push_share.cb_share_push(callback)
    message.answer_photo.assert_not_awaited()
    assert message.answer.await_args.args[0] == i18n.t_in("en", "push_card.gone")


# ---------- «Уведомления» ----------


async def _client(db, lang="en"):
    await db.get_or_create_user(telegram_id=111, username="tester")
    await db.set_user_lang(111, lang)
    code = await db.issue_oauth_link_code(111, ttl_seconds=600, digits=8)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    resp = await client.post("/auth/link", json={"code": code})
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def test_inbox_card_for_skip_push_only(fresh_db):
    client = await _client(fresh_db)
    skip_id = await fresh_db.add_inbox_item(111, push_texts.SKIP_7, "A week off", "HEY ATHLETE! A week.", None, "en")
    rank_id = await fresh_db.add_inbox_item(111, push_texts.RANK_NEAR, "Close", "HEY ATHLETE! Close.", None, "en")
    items = {i["id"]: i for i in (await client.get("/inbox")).json()["items"]}
    assert items[skip_id]["shareable"] is True and items[rank_id]["shareable"] is False
    card = await client.get(f"/inbox/{skip_id}/card.png")
    assert card.status_code == 200 and card.headers["content-type"] == "image/png"
    assert (await client.get(f"/inbox/{rank_id}/card.png")).status_code == 404
    assert (await client.get("/inbox/999999/card.png")).status_code == 404
