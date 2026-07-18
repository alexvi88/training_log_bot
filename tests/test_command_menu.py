"""Telegram only shows /check_users in the slash-command menu for the admin's own
chat; everyone else must see /start and /ai_trainer. These tests pin that scoping.
"""
import ast
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

import config
from main import _setup_commands


def _router_registration_order() -> list[str]:
    """Router names in the order main() feeds them to dp.include_router(...)."""
    tree = ast.parse(Path("main.py").read_text())
    (main_fn,) = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "main"]
    order = []
    for node in ast.walk(main_fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "include_router"
            and isinstance(node.args[0], ast.Attribute)
            and isinstance(node.args[0].value, ast.Name)
        ):
            order.append(node.args[0].value.id)
    return order


def test_admin_router_registered_before_fsm_flow_routers():
    """Admin-only commands (/check_users, /pushes) must win over any in-progress
    FSM flow's catch-all message handler (e.g. workout.py's logging_set handler
    accepts any text as a weight/reps entry), or the admin typing them mid-workout
    gets "Не понял ввод" instead of the admin screen. aiogram tries routers in
    registration order, so admin.router has to come before the flow routers.
    """
    order = _router_registration_order()
    flow_routers = {"workout", "backfill", "csv_import", "exercises", "history", "edit_workout", "ai_trainer"}
    admin_index = order.index("admin")
    for name in flow_routers & set(order):
        assert admin_index < order.index(name), f"admin.router must be registered before {name}.router"


def test_feedback_router_registered_before_fsm_flow_routers():
    """Same reasoning as above: /feedback must win over any in-progress FSM
    flow's catch-all message handler, so feedback.router has to come first.
    """
    order = _router_registration_order()
    flow_routers = {"workout", "backfill", "csv_import", "exercises", "history", "edit_workout", "ai_trainer"}
    feedback_index = order.index("feedback")
    for name in flow_routers & set(order):
        assert feedback_index < order.index(name), f"feedback.router must be registered before {name}.router"


@pytest.mark.asyncio
async def test_default_scope_only_has_start(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 12345)
    bot = AsyncMock()

    await _setup_commands(bot)

    default_call = next(
        c for c in bot.set_my_commands.call_args_list if isinstance(c.kwargs["scope"], BotCommandScopeDefault)
    )
    commands = default_call.args[0]
    assert [c.command for c in commands] == ["start", "ai_trainer", "feedback"]


@pytest.mark.asyncio
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
    assert {c.command for c in commands} == {"start", "ai_trainer", "feedback", "check_users", "pushes"}

@pytest.mark.asyncio
async def test_no_admin_scope_registered_when_admin_id_unset(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", None)
    bot = AsyncMock()

    await _setup_commands(bot)

    assert bot.set_my_commands.call_count == 1
    scope = bot.set_my_commands.call_args.kwargs["scope"]
    assert isinstance(scope, BotCommandScopeDefault)
