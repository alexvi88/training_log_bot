"""Атлет сносит свой аккаунт сам — из настроек бота и из приложения.

Требование Apple 5.1.1(v): если в продукт можно зарегистрироваться, удалить
аккаунт должно быть можно изнутри него, а не письмом в поддержку. Раньше снос
умела только админская кнопка на TEST_USER_ID, и вся защита от случайного
нажатия жила в ней же — теперь вызывающих трое, поэтому тесты держат три вещи:

1. снос одинаково полный из любой точки входа (база + состояние диалога), —
   ровно то, что разъезжается, когда у операции появляется вторая копия;
2. случайный снос стоит дороже одного запроса: телу DELETE нужен явный
   `confirm`, экрану бота — подтверждение;
3. после сноса токен приложения мёртв: продолжать работать им — это доступ к
   аккаунту, которого нет.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

import account_deletion
import api_v1
from fsm_storage import JSONFileStorage
from handlers import settings as settings_handlers

pytestmark = pytest.mark.asyncio

USER_ID = 5150


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _linked_client(fresh_db, client_factory, telegram_id=USER_ID):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def _workout_with_a_set(fresh_db, user_id=USER_ID):
    """Немного истории, чтобы «снёс» и «нечего было сносить» не выглядели
    одинаково зелёными."""
    await fresh_db.get_or_create_user(telegram_id=user_id, username="tester")
    exercise_id = await fresh_db.create_exercise(user_id, "Жим лёжа", 1)
    workout_id = await fresh_db.create_finished_workout(
        user_id, "2026-08-01T10:00:00", "2026-08-01T10:30:00"
    )
    block_id = await fresh_db.create_block(workout_id, "single")
    await fresh_db.add_set(block_id, exercise_id, 1, 0, 100.0, 5)
    return workout_id


def _state(tmp_path, user_id=USER_ID):
    storage = JSONFileStorage(str(tmp_path / "fsm.json"))
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


def _callback(user_id=USER_ID):
    message = MagicMock()
    message.delete = AsyncMock()
    message.edit_text = AsyncMock(return_value=SimpleNamespace(message_id=1))
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=1))
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    callback.message = message
    callback.answer = AsyncMock()
    return callback


def _shown_text(callback) -> str:
    """Что человек увидел: ui.safe_edit по обстоятельствам правит сообщение
    или шлёт новое, и тесту важен текст, а не способ доставки."""
    for mock in (callback.message.edit_text, callback.message.answer):
        if mock.await_args is not None:
            return mock.await_args.args[0]
    raise AssertionError("экран не показан вообще")


async def test_delete_account_takes_the_database_and_the_draft(fresh_db, tmp_path):
    """База и файл состояния — два разных места, и снос обязан добить оба:
    черновик, переживший снос, возвращается первым тапом по висящей кнопке."""
    await _workout_with_a_set(fresh_db)
    state = _state(tmp_path)
    await state.update_data(draft={"program": "Масса 4×"})
    assert json.loads((tmp_path / "fsm.json").read_text())

    left = await account_deletion.delete_account(USER_ID, state.storage)

    assert left == {}
    assert await fresh_db.get_user(USER_ID) is None
    assert await fresh_db.list_workouts(USER_ID, status="finished") == []
    assert await state.get_data() == {}
    assert json.loads((tmp_path / "fsm.json").read_text()) == {}


async def test_delete_account_uses_registered_storage_when_none_passed(fresh_db, tmp_path):
    """REST-слой до диспетчера не достаёт и зовёт без storage — черновик всё
    равно обязан исчезнуть, иначе «удалил из приложения» сносит меньше, чем
    «удалил из бота» (main.py кладёт сюда хранилище диспетчера)."""
    state = _state(tmp_path)
    await state.update_data(draft={"program": "Масса 4×"})
    account_deletion.set_fsm_storage(state.storage)
    try:
        await account_deletion.delete_account(USER_ID)
    finally:
        account_deletion.set_fsm_storage(None)

    assert await state.get_data() == {}


async def test_api_delete_wipes_the_account(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    await _workout_with_a_set(fresh_db)

    resp = await client.delete("/account?confirm=delete")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": True}
    assert await fresh_db.get_user(USER_ID) is None


async def test_api_delete_needs_an_explicit_confirm(fresh_db, client_factory):
    """Повтор запроса из офлайн-очереди, чужой скрипт с утёкшим токеном,
    опечатка в пути — голый DELETE получить слишком легко, а вернуть историю
    нельзя ничем."""
    client = await _linked_client(fresh_db, client_factory)
    await _workout_with_a_set(fresh_db)

    bare = await client.delete("/account")
    assert bare.status_code == 400
    wrong_value = await client.delete("/account?confirm=yes")
    assert wrong_value.status_code == 400

    # Главное в этом тесте — не код ответа, а то, что аккаунт на месте.
    assert await fresh_db.get_user(USER_ID) is not None
    assert len(await fresh_db.list_workouts(USER_ID, status="finished")) == 1


async def test_api_delete_kills_the_token_it_came_with(fresh_db, client_factory):
    """Токен снесённого аккаунта, продолжающий работать, — это доступ к тому,
    чего уже нет."""
    client = await _linked_client(fresh_db, client_factory)

    await client.delete("/account?confirm=delete")

    resp = await client.get("/settings")
    assert resp.status_code == 401


async def test_api_delete_needs_a_token(fresh_db, client_factory):
    await fresh_db.get_or_create_user(telegram_id=USER_ID, username="tester")
    client = client_factory()

    resp = await client.delete("/account?confirm=delete")

    assert resp.status_code == 401
    assert await fresh_db.get_user(USER_ID) is not None


async def test_settings_delete_asks_before_it_wipes(fresh_db, tmp_path):
    """Первый тап только показывает экран подтверждения — на этом шаге не
    должно исчезнуть ничего."""
    await _workout_with_a_set(fresh_db)
    state = _state(tmp_path)
    callback = _callback()

    await settings_handlers.settings_delete_confirm(callback, state)

    assert await fresh_db.get_user(USER_ID) is not None
    text = _shown_text(callback)
    # Экран обязан сказать про обе поверхности: аккаунт один на бота и на
    # приложение, и человек, думающий, что отвязывает приложение, иначе
    # снесёт год истории.
    assert "приложение" in text.lower()


async def test_settings_delete_go_wipes_everything(fresh_db, tmp_path):
    await _workout_with_a_set(fresh_db)
    state = _state(tmp_path)
    await state.update_data(draft={"program": "Масса 4×"})

    await settings_handlers.settings_delete_go(_callback(), state)

    assert await fresh_db.get_user(USER_ID) is None
    assert await fresh_db.list_workouts(USER_ID, status="finished") == []
    assert await state.get_data() == {}


async def test_settings_delete_go_says_so_when_the_database_refuses(
    fresh_db, tmp_path, monkeypatch
):
    """Снос идёт одной транзакцией: упала — значит аккаунт цел, и сказать об
    этом надо прямо. «Удалил» и «не удалил» не имеют права выглядеть одинаково."""
    await _workout_with_a_set(fresh_db)

    async def _boom(*args, **kwargs):
        raise RuntimeError("database says no")

    monkeypatch.setattr(account_deletion, "delete_account", _boom)
    callback = _callback()

    await settings_handlers.settings_delete_go(callback, _state(tmp_path))

    assert await fresh_db.get_user(USER_ID) is not None
    text = _shown_text(callback)
    assert "не удалил" in text.lower()
