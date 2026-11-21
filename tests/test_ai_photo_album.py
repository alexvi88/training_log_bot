"""Альбом фото в чате тренера бота — один вопрос со всеми фото.

Telegram присылает альбом отдельными сообщениями с общим media_group_id.
Раньше первое уходило тренеру, а на второе бот отвечал «ещё думаю» — фото
терялось (живой случай владельца, 21.11.2026)."""

import asyncio
from types import SimpleNamespace

import pytest

import i18n
from handlers import ai_trainer as h


class FakeMessage:
    def __init__(self, user_id=1, group="g1", caption=None, n=0):
        self.from_user = SimpleNamespace(id=user_id)
        self.media_group_id = group
        self.caption = caption
        self.photo = [SimpleNamespace(file_size=10, n=n)]
        self.replies = []

    async def reply(self, text, **kwargs):
        self.replies.append(text)


class FakeState:
    async def get_data(self):
        return {}


@pytest.fixture
def captured(monkeypatch):
    calls = []

    async def fake_download(message):
        return f"data:image/jpeg;base64,{message.photo[-1].n}"

    async def fake_handle(message, state, question, history_question, image_data_url=None, **kwargs):
        calls.append({"question": question, "images": image_data_url})

    monkeypatch.setattr(h, "_download_photo_as_data_url", fake_download)
    monkeypatch.setattr(h, "_handle_question", fake_handle)
    monkeypatch.setattr(h, "ALBUM_COLLECT_SECONDS", 0.05)
    h._busy.clear()
    h._albums.clear()
    return calls


async def _send_album(messages):
    await asyncio.gather(*(h.ai_photo_question(m, FakeState()) for m in messages))


@pytest.mark.asyncio
async def test_album_of_two_is_one_question_with_both_photos(captured):
    first = FakeMessage(caption="что думаем", n=0)
    second = FakeMessage(n=1)
    with i18n.use_lang("ru"):
        await _send_album([first, second])

    assert captured == [{"question": "что думаем", "images": [
        "data:image/jpeg;base64,0", "data:image/jpeg;base64,1",
    ]}]
    assert first.replies == [] and second.replies == []
    assert 1 not in h._busy and h._albums == {}


@pytest.mark.asyncio
async def test_caption_on_a_later_photo_is_used(captured):
    with i18n.use_lang("ru"):
        await _send_album([FakeMessage(n=0), FakeMessage(caption="  спина?  ", n=1)])
    assert captured[0]["question"] == "спина?"


@pytest.mark.asyncio
async def test_album_over_the_limit_takes_the_first_three_and_says_so(captured):
    messages = [FakeMessage(n=i) for i in range(5)]
    with i18n.use_lang("ru"):
        await _send_album(messages)

    assert len(captured) == 1
    assert captured[0]["images"] == [f"data:image/jpeg;base64,{i}" for i in range(3)]
    assert messages[0].replies == [i18n.t_in("ru", "ai.screen.album_trimmed", max=3)]


@pytest.mark.asyncio
async def test_single_photo_keeps_the_old_shape(captured):
    with i18n.use_lang("ru"):
        await h.ai_photo_question(FakeMessage(group=None, n=7), FakeState())
    assert captured[0]["images"] == "data:image/jpeg;base64,7"


@pytest.mark.asyncio
async def test_separate_photo_while_busy_still_gets_busy_reply(captured):
    h._busy.add(1)
    message = FakeMessage(group=None)
    with i18n.use_lang("ru"):
        await h.ai_photo_question(message, FakeState())
    assert captured == []
    assert message.replies == [i18n.t_in("ru", "ai.screen.busy")]
