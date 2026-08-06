"""Мини-игра «Кач-Раннер»: подпись initData, границы результата, рекорд в БД
и команда /game."""

import hashlib
import hmac
import json
import time
import urllib.parse
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
import game_server
from handlers import game as game_handler

pytestmark = pytest.mark.asyncio

TOKEN = "12345:TEST_TOKEN"


def _signed_init_data(user_id: int = 111, auth_date: int | None = None, token: str = TOKEN) -> str:
    params = {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "AAE1",
        "user": json.dumps({"id": user_id, "first_name": "Тест"}),
    }
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret = hmac.HMAC(b"WebAppData", token.encode(), hashlib.sha256).digest()
    params["hash"] = hmac.HMAC(secret, data_check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(params)


# ---------- подпись initData ----------


async def test_valid_init_data_returns_user_id(monkeypatch):
    monkeypatch.setattr(config, "BOT_TOKEN", TOKEN)
    assert game_server.validate_init_data(_signed_init_data(user_id=42)) == 42


async def test_wrong_signature_is_rejected(monkeypatch):
    monkeypatch.setattr(config, "BOT_TOKEN", TOKEN)
    forged = _signed_init_data(user_id=42, token="999:OTHER_TOKEN")
    assert game_server.validate_init_data(forged) is None


async def test_stale_and_future_init_data_are_rejected(monkeypatch):
    monkeypatch.setattr(config, "BOT_TOKEN", TOKEN)
    stale = _signed_init_data(auth_date=int(time.time()) - game_server.INIT_DATA_TTL - 10)
    assert game_server.validate_init_data(stale) is None
    # Часы «из будущего» дали бы initData, который не протухает никогда.
    future = _signed_init_data(auth_date=int(time.time()) + game_server.CLOCK_SKEW + 60)
    assert game_server.validate_init_data(future) is None


async def test_empty_init_data_is_rejected():
    assert game_server.validate_init_data("") is None


# ---------- границы результата ----------


async def test_result_within_bounds_is_parsed():
    parsed = game_server.parse_result({"distance": 512, "score": 120, "fighter": "power"})
    assert parsed == {"distance": 512, "score": 120, "fighter": "power"}


async def test_result_out_of_bounds_or_malformed_is_rejected():
    assert game_server.parse_result({"distance": -1, "score": 0}) is None
    assert game_server.parse_result({"distance": game_server.MAX_DISTANCE + 1, "score": 0}) is None
    assert game_server.parse_result({"distance": "далеко", "score": 0}) is None
    assert game_server.parse_result("not a dict") is None


async def test_fighter_name_is_truncated():
    parsed = game_server.parse_result({"distance": 1, "score": 0, "fighter": "x" * 100})
    assert len(parsed["fighter"]) == 16


# ---------- рекорд в БД ----------


async def test_best_distance_is_max_of_all_runs(fresh_db, user_id):
    assert await fresh_db.get_game_best_distance(user_id) == 0
    await fresh_db.save_game_result(user_id, 120, 30, "power")
    await fresh_db.save_game_result(user_id, 512, 10, "build")
    await fresh_db.save_game_result(user_id, 90, 999, "cross")
    assert await fresh_db.get_game_best_distance(user_id) == 512


# ---------- реакция тренера на забег ----------


async def test_first_run_gets_a_trainer_message(fresh_db, user_id, monkeypatch):
    sent = []
    monkeypatch.setattr(game_server, "_send_trainer_message",
                        AsyncMock(side_effect=lambda uid, text: sent.append((uid, text))))

    ok = await game_server.process_game_result(
        user_id, {"distance": 150, "score": 40, "fighter": "power", "gameTimestamp": 1}
    )

    assert ok
    ((uid, text),) = sent
    assert uid == user_id
    assert text.startswith("ПРИВЕТ АТЛЕТ, ")
    assert "150" in text


async def test_ordinary_run_stays_silent(fresh_db, user_id, monkeypatch):
    """Не рекорд и не первый забег — тренер молчит, иначе каждый заход = спам."""
    sent = AsyncMock()
    monkeypatch.setattr(game_server, "_send_trainer_message", sent)
    await fresh_db.save_game_result(user_id, 500, 10, "power")

    await game_server.process_game_result(
        user_id, {"distance": 400, "score": 5, "fighter": "power", "gameTimestamp": 2}
    )

    sent.assert_not_awaited()


async def test_small_record_gains_stay_silent_but_big_ones_notify(fresh_db, user_id, monkeypatch):
    """Ранние забеги бьют рекорд каждый раз — сообщение только за заметный прирост."""
    sent = []
    monkeypatch.setattr(game_server, "_send_trainer_message",
                        AsyncMock(side_effect=lambda uid, text: sent.append(text)))
    await fresh_db.save_game_result(user_id, 500, 10, "power")

    # +10% — рекорд, но тихий
    await game_server.process_game_result(
        user_id, {"distance": 550, "score": 5, "fighter": "power", "gameTimestamp": 3}
    )
    assert sent == []

    # +30% — уже событие
    await game_server.process_game_result(
        user_id, {"distance": 715, "score": 5, "fighter": "power", "gameTimestamp": 4}
    )
    (text,) = sent
    assert "рекорд" in text and "715" in text and "550" in text


async def test_notify_failure_does_not_break_result_saving(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(
        game_server, "_send_trainer_message", AsyncMock(side_effect=RuntimeError("tg down"))
    )

    ok = await game_server.process_game_result(
        user_id, {"distance": 200, "score": 1, "fighter": "cross", "gameTimestamp": 5}
    )

    assert ok
    assert await fresh_db.get_game_best_distance(user_id) == 200


async def test_duplicate_submission_is_saved_once(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(game_server, "_send_trainer_message", AsyncMock())
    raw = {"distance": 300, "score": 10, "fighter": "power", "gameTimestamp": 777}

    assert await game_server.process_game_result(user_id, raw)
    assert await game_server.process_game_result(user_id, raw)

    cur = await fresh_db.conn().execute(
        "SELECT COUNT(*) FROM game_results WHERE telegram_id = ?", (user_id,)
    )
    (count,) = await cur.fetchone()
    assert count == 1


# ---------- команда /game ----------


def _make_message(user_id: int):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, username="tester")
    message.answer = AsyncMock()
    return message


async def test_cmd_game_replies_with_webapp_button(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "MCP_ENABLED", True)
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", "https://bot.example")
    message = _make_message(user_id)

    await game_handler.cmd_game(message)

    kwargs = message.answer.await_args.kwargs
    (button,) = kwargs["reply_markup"].inline_keyboard[0]
    assert button.web_app.url == "https://bot.example/game"
    text = message.answer.await_args.args[0]
    assert "КАЧ-РАННЕР" in text


async def test_cmd_game_mentions_best_distance_when_it_exists(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "MCP_ENABLED", True)
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", "https://bot.example")
    await fresh_db.save_game_result(user_id, 512, 10, "power")
    message = _make_message(user_id)

    await game_handler.cmd_game(message)

    assert "512 м" in message.answer.await_args.args[0]


async def test_cmd_game_without_server_says_so(fresh_db, user_id, monkeypatch):
    """Без публичного адреса страницу игры никто не отдаёт — кнопка вела бы в никуда."""
    monkeypatch.setattr(config, "MCP_ENABLED", False)
    message = _make_message(user_id)

    await game_handler.cmd_game(message)

    text = message.answer.await_args.args[0]
    assert "не подключена" in text
    assert message.answer.await_args.kwargs.get("reply_markup") is None
