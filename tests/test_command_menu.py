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
    """Router names in the order setup_routers() feeds them to dp.include_router(...).

    Read from the source rather than from a live Dispatcher on purpose: the
    routers are module-level singletons that can only be attached once per
    process, and tests/test_routing.py already builds the real dispatcher.
    """
    tree = ast.parse(Path("main.py").read_text())
    (setup_fn,) = [
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "setup_routers"
    ]
    order = []
    for node in ast.walk(setup_fn):
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
async def test_default_scope_advertises_the_user_facing_sections(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 12345)
    bot = AsyncMock()

    await _setup_commands(bot)

    default_call = next(
        c for c in bot.set_my_commands.call_args_list if isinstance(c.kwargs["scope"], BotCommandScopeDefault)
    )
    commands = default_call.args[0]
    assert [c.command for c in commands] == [
        "start", "help", "ai_trainer", "food_diary", "feedback",
    ]


@pytest.mark.asyncio
async def test_mcp_available_adds_mcp_and_game_to_default_scope(monkeypatch):
    """/game раздаёт страница того же HTTP-сервера, что и MCP (см.
    handlers/game.game_url), так что обе команды в «/»-меню зависят от одного
    и того же условия."""
    monkeypatch.setattr(config, "ADMIN_ID", None)
    monkeypatch.setattr(config, "MCP_ENABLED", True)
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", "https://example.com")
    bot = AsyncMock()

    await _setup_commands(bot)

    default_call = next(
        c for c in bot.set_my_commands.call_args_list if isinstance(c.kwargs["scope"], BotCommandScopeDefault)
    )
    commands = default_call.args[0]
    assert [c.command for c in commands] == [
        "start", "help", "ai_trainer", "food_diary", "feedback", "mcp", "game",
    ]


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
    assert {c.command for c in commands} == {
        "start",
        "help",
        "ai_trainer",
        "feedback",
        "food_diary",
        "check_users",
        "ai_dialogs",
        "pushes",
        "activity",
        "growth",
        "broadcast",
        "announce",
        "admin_wipe",
    }

@pytest.mark.asyncio
async def test_no_admin_scope_registered_when_admin_id_unset(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", None)
    bot = AsyncMock()

    await _setup_commands(bot)

    # Без ADMIN_ID остаётся только дефолтный scope, но заливается он дважды —
    # русский набор (без language_code) и английский (language_code="en").
    assert bot.set_my_commands.call_count == 2
    for call in bot.set_my_commands.call_args_list:
        assert isinstance(call.kwargs["scope"], BotCommandScopeDefault)


def _slash_commands_in_handlers() -> set[str]:
    """Каждая команда, на которую в коде есть хендлер, — прямо из исходников.

    Разбор AST, а не импорт роутеров: роутеры — модульные синглтоны, их уже
    собирает tests/test_routing.py, а список команд нужен независимо от того,
    в каком порядке кто импортировался.
    """
    found: set[str] = set()
    for path in [*Path("handlers").glob("*.py"), Path("main.py")]:
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "Command"
            ):
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        found.add(arg.value)
    return found


@pytest.mark.asyncio
async def test_every_slash_command_is_in_the_quick_menu(monkeypatch):
    """Ни одной команды мимо «/»-меню: чего там нет, того для человека нет.

    /growth так и выпал — хендлер есть, в меню его не было, и вспомнить о нём
    можно было только по памяти. Тест держит список сам, поэтому следующая
    забытая команда краснеет здесь, а не обнаруживается через полгода.
    """
    monkeypatch.setattr(config, "ADMIN_ID", 12345)
    # Условные команды (/mcp, /game, /community) висят на адресах, которых в
    # тестовом окружении нет, — включаем, иначе проверять было бы нечего.
    monkeypatch.setattr(config, "mcp_available", lambda: True)
    monkeypatch.setattr(config, "community_available", lambda: True)
    bot = AsyncMock()

    await _setup_commands(bot)

    listed = {
        command.command
        for call in bot.set_my_commands.call_args_list
        for command in call.args[0]
    }
    assert _slash_commands_in_handlers() - listed == set()
