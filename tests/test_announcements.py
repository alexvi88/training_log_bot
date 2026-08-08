"""Разовая релизная рассылка (announcements.py): один раз на человека, и без человека у клавиатуры."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError

import announcements
import config
import db as db_module

pytestmark = pytest.mark.asyncio


class FakeBot:
    """Считает отправки и умеет падать на конкретном получателе."""

    def __init__(self, fail_for: dict[int, Exception] | None = None):
        self.sent: list[tuple[int, str]] = []
        self.fail_for = fail_for or {}

    async def send_message(self, chat_id: int, text: str, **kwargs):
        return await self._record(chat_id, text)

    async def send_photo(self, chat_id: int, caption: str, **kwargs):
        return await self._record(chat_id, caption)

    async def _record(self, chat_id: int, text: str):
        exc = self.fail_for.get(chat_id)
        if exc is not None:
            raise exc
        self.sent.append((chat_id, text))
        return SimpleNamespace(photo=None)


def _announcement() -> announcements.Announcement:
    return announcements.Announcement(
        key="test_release",
        text="ПРИВЕТ АТЛЕТ, новая штука.",
        buttons=[("🤖 Собрать программу", "ai:buildprog")],
        image=None,
    )


async def _users(db, *ids: int) -> None:
    for telegram_id in ids:
        await db.get_or_create_user(telegram_id=telegram_id, username=f"u{telegram_id}")


async def test_reaches_everyone_once(fresh_db):
    await _users(fresh_db, 1, 2, 3)
    bot, ann = FakeBot(), _announcement()

    assert await announcements.send_announcement(bot, ann) == (3, 0, 0)
    assert [chat_id for chat_id, _ in bot.sent] == [1, 2, 3]


async def test_second_run_sends_nothing(fresh_db):
    """Перезапуск контейнера и повторный деплой не рассылают релиз заново."""
    await _users(fresh_db, 1, 2)
    ann = _announcement()
    await announcements.send_announcement(FakeBot(), ann)

    bot = FakeBot()
    assert await announcements.send_announcement(bot, ann) == (0, 0, 0)
    assert bot.sent == []


async def test_new_user_after_the_release_still_gets_it(fresh_db):
    await _users(fresh_db, 1)
    ann = _announcement()
    await announcements.send_announcement(FakeBot(), ann)

    await _users(fresh_db, 2)
    bot = FakeBot()
    assert await announcements.send_announcement(bot, ann) == (1, 0, 0)
    assert [chat_id for chat_id, _ in bot.sent] == [2]


async def test_pushes_off_means_no_announcement(fresh_db):
    await _users(fresh_db, 1, 2)
    await fresh_db.update_user(2, pushes_enabled=0)

    bot = FakeBot()
    await announcements.send_announcement(bot, _announcement())

    assert [chat_id for chat_id, _ in bot.sent] == [1]


async def test_blocked_user_drops_out_of_future_pushes(fresh_db):
    await _users(fresh_db, 1, 2)
    bot = FakeBot(fail_for={1: TelegramForbiddenError(method=None, message="blocked")})

    assert await announcements.send_announcement(bot, _announcement()) == (1, 1, 0)
    row = await fresh_db.get_user(1)
    assert row["pushes_enabled"] == 0
    # Остальные не остаются без релиза из-за одного удалённого чата.
    assert [chat_id for chat_id, _ in bot.sent] == [2]


async def test_failed_send_is_retried_on_the_next_start(fresh_db):
    """Не дошло — не отмечаем доставленным: следующий старт попробует ещё раз."""
    await _users(fresh_db, 1, 2)
    ann = _announcement()
    first = FakeBot(fail_for={1: TelegramNetworkError(method=None, message="down")})

    assert await announcements.send_announcement(first, ann) == (1, 0, 1)

    second = FakeBot()
    assert await announcements.send_announcement(second, ann) == (1, 0, 0)
    assert [chat_id for chat_id, _ in second.sent] == [1]


async def test_startup_job_waits_while_the_feature_is_off(fresh_db, monkeypatch):
    await _users(fresh_db, 1)
    monkeypatch.setattr(announcements.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(config, "ANNOUNCEMENTS_ENABLED", True)
    monkeypatch.setattr(config, "ADMIN_ID", None)
    ann = _announcement()
    ann.available = lambda: False
    monkeypatch.setattr(announcements, "ANNOUNCEMENTS", [ann])

    bot = FakeBot()
    await announcements.run_pending_announcements(bot)

    assert bot.sent == []
    # Ничего не отмечено доставленным — рассылка уйдёт, когда фичу включат.
    assert await db_module.list_announcement_recipients(ann.key) == [1]


async def test_startup_job_off_by_env(fresh_db, monkeypatch):
    await _users(fresh_db, 1)
    monkeypatch.setattr(announcements.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(config, "ANNOUNCEMENTS_ENABLED", False)
    monkeypatch.setattr(announcements, "ANNOUNCEMENTS", [_announcement()])

    bot = FakeBot()
    await announcements.run_pending_announcements(bot)

    assert bot.sent == []


async def test_release_text_speaks_in_the_coach_voice():
    ann = announcements.RELEASE_AI_PROGRAMS_AND_VIDEO
    assert ann.text.startswith("ПРИВЕТ АТЛЕТ, ")
    assert len(ann.text) <= announcements.CAPTION_LIMIT


async def test_release_buttons_lead_into_the_two_features():
    """Кнопки анонса должны попадать в живые хендлеры, а не в никуда.

    Сверяем с исходником хендлеров: переименуют callback у входа в сборку
    программы — тест упадёт здесь, а не в проде через кнопку под релизом,
    которая молча ничего не делает.
    """
    callbacks = [data for _, data in announcements.RELEASE_AI_PROGRAMS_AND_VIDEO.buttons]
    assert callbacks == ["ai:buildprog", "ai:videohint"]

    source = (Path(__file__).resolve().parent.parent / "handlers" / "ai_trainer.py").read_text()
    for data in callbacks:
        assert f'F.data == "{data}"' in source
