"""Telegram only shows /check_users in the slash-command menu for the admin's own
chat; everyone else must see just /start. These tests pin that scoping.
"""
from unittest.mock import AsyncMock

import pytest
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

import config
from main import _setup_commands

pytestmark = pytest.mark.asyncio


async def test_default_scope_only_has_start(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 12345)
    bot = AsyncMock()

    await _setup_commands(bot)

    default_call = next(
        c for c in bot.set_my_commands.call_args_list if isinstance(c.kwargs["scope"], BotCommandScopeDefault)
    )
    commands = default_call.args[0]
    assert [c.command for c in commands] == ["start"]


async def test_admin_scope_targets_only_admin_chat_and_includes_admin_command(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 12345)
    bot = AsyncMock()

    await _setup_commands(bot)

    admin_call = next(
        c for c in bot.set_my_commands.call_args_list if isinstance(c.kwargs["scope"], BotCommandScopeChat)
    )
    scope: BotCommandScopeChat = admin_call.kwargs["scope"]
    commands: list[BotCommand] = admin_call.args[0]
    assert scope.chat_id == 12345
    assert {c.command for c in commands} == {"start", "check_users"}


async def test_no_admin_scope_registered_when_admin_id_unset(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", None)
    bot = AsyncMock()

    await _setup_commands(bot)

    assert bot.set_my_commands.call_count == 1
    scope = bot.set_my_commands.call_args.kwargs["scope"]
    assert isinstance(scope, BotCommandScopeDefault)
