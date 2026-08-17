"""Мини-игра «Кач-Отряд» (slug ``squad``): границы результата, рекорд по
очкам, реакция тренера, отдача страницы и миграция старой БД game_results."""

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_game import TOKEN, _post_game_result, _signed_init_data  # noqa: F401
from test_mcp_server import _running

import config
import game_server
import mcp_server
from handlers import game as game_handler

pytestmark = pytest.mark.asyncio


# ---------- границы результата отряда ----------


async def test_squad_result_within_bounds_is_parsed():
    parsed = game_server.parse_result(
        {"distance": 800, "score": 340, "squad": 4, "fighter": "power"}, game="squad"
    )
    assert parsed == {"distance": 800, "score": 340, "squad": 4, "fighter": "power"}


async def test_squad_out_of_bounds_is_rejected():
    assert game_server.parse_result({"distance": 0, "score": 0, "squad": -1}, game="squad") is None
    assert (
        game_server.parse_result(
            {"distance": 0, "score": 0, "squad": game_server.MAX_SQUAD + 1}, game="squad"
        )
        is None
    )
    assert (
        game_server.parse_result(
            {"distance": 0, "score": game_server.MAX_SCORE + 1, "squad": 1}, game="squad"
        )
        is None
    )


async def test_unknown_game_slug_is_rejected():
    assert game_server.parse_result({"distance": 1, "score": 1}, game="tetris") is None


# ---------- запись в БД и рекорд по score ----------


async def test_squad_best_score_is_max_of_all_runs(fresh_db, user_id):
    assert await fresh_db.get_squad_best_score(user_id) == 0
    await fresh_db.save_game_result(user_id, 800, 100, "power", game="squad", squad=3)
    await fresh_db.save_game_result(user_id, 900, 400, "build", game="squad", squad=5)
    await fresh_db.save_game_result(user_id, 700, 50, "cross", game="squad", squad=2)
    assert await fresh_db.get_squad_best_score(user_id) == 400
    # Раннер и отряд не путают рекорды друг друга.
    assert await fresh_db.get_game_best_distance(user_id) == 0


async def test_runner_and_squad_runs_do_not_mix_records(fresh_db, user_id):
    await fresh_db.save_game_result(user_id, 900, 5, "power")  # раннер, game по умолчанию
    await fresh_db.save_game_result(user_id, 10, 900, "build", game="squad", squad=5)
    assert await fresh_db.get_game_best_distance(user_id) == 900
    assert await fresh_db.get_squad_best_score(user_id) == 900


# ---------- реакция тренера ----------


async def test_first_squad_run_gets_a_trainer_message(fresh_db, user_id, monkeypatch):
    sent = []
    monkeypatch.setattr(
        game_server, "_send_trainer_message",
        AsyncMock(side_effect=lambda uid, text: sent.append((uid, text))),
    )

    ok = await game_server.process_game_result(
        user_id,
        {"distance": 400, "score": 150, "squad": 3, "fighter": "power", "gameTimestamp": 1},
        game="squad",
    )

    assert ok
    ((uid, text),) = sent
    assert uid == user_id
    assert text.startswith("ПРИВЕТ АТЛЕТ! ")
    assert "150" in text


async def test_ordinary_squad_run_stays_silent(fresh_db, user_id, monkeypatch):
    sent = AsyncMock()
    monkeypatch.setattr(game_server, "_send_trainer_message", sent)
    await fresh_db.save_game_result(user_id, 500, 500, "power", game="squad", squad=4)

    await game_server.process_game_result(
        user_id,
        {"distance": 300, "score": 400, "squad": 3, "fighter": "power", "gameTimestamp": 2},
        game="squad",
    )

    sent.assert_not_awaited()


async def test_squad_record_notifies_only_on_a_big_enough_gain(fresh_db, user_id, monkeypatch):
    sent = []
    monkeypatch.setattr(
        game_server, "_send_trainer_message",
        AsyncMock(side_effect=lambda uid, text: sent.append(text)),
    )
    await fresh_db.save_game_result(user_id, 500, 500, "power", game="squad", squad=4)

    # +10% — рекорд, но тихий.
    await game_server.process_game_result(
        user_id,
        {"distance": 500, "score": 550, "squad": 4, "fighter": "power", "gameTimestamp": 3},
        game="squad",
    )
    assert sent == []

    # +30% — уже событие.
    await game_server.process_game_result(
        user_id,
        {"distance": 500, "score": 715, "squad": 5, "fighter": "power", "gameTimestamp": 4},
        game="squad",
    )
    (text,) = sent
    assert "рекорд" in text and "715" in text and "550" in text


# ---------- команда /game теперь про две игры ----------


def _make_message(user_id: int):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, username="tester")
    message.answer = AsyncMock()
    return message


async def test_cmd_game_squad_button_opens_squad_url(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "MCP_ENABLED", True)
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", "https://bot.example")
    message = _make_message(user_id)

    await game_handler.cmd_game(message)

    kwargs = message.answer.await_args.kwargs
    (_runner_row, squad_row) = kwargs["reply_markup"].inline_keyboard
    # ?lang= — выбор пользователя, см. handlers/game._lang_query.
    assert squad_row[0].web_app.url == "https://bot.example/game/squad?lang=ru"


# ---------- отдача страницы отряда и приём результата по HTTP ----------


async def test_squad_page_route_serves_the_squad_html():
    """Роут /game/squad должен указывать на game_squad.html, а не на game.html
    (регрессия на копипаст раннера) — сам файл может ещё не существовать
    (его пишет параллельный агент), поэтому здесь без реального ASGI-вызова."""
    assert game_server.GAMES["squad"].page_path == game_server.SQUAD_PATH
    assert game_server.GAMES["squad"].page_file.name == "game_squad.html"
    assert game_server.SQUAD_PATH != game_server.GAME_PATH


async def _get(app, path: str) -> int:
    """Один GET по настоящему ASGI-проводу — статус ответа без чтения тела."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("training-log.example.com", 443),
    }
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"]


async def test_squad_page_route_is_registered_and_not_a_404():
    """Роутинг реально навешан на приложение: /game/squad не падает на 404
    ("маршрут не существует") — не проверяет содержимое game_squad.html,
    которое пишет параллельный агент и которого на момент прогона теста может
    ещё не быть на диске (тогда FileResponse сама вернёт свою ошибку, а не
    starlette 404 "no matching route")."""
    app = mcp_server.build_app()
    async with _running(app):
        status = await _get(app, game_server.SQUAD_PATH)
    assert status != 404

    # /game (раннер) — старый маршрут остаётся рабочим без изменений. Новый
    # app: StreamableHTTPSessionManager запускается только один раз за жизнь
    # инстанса, второй _running на том же app свалился бы сам.
    app2 = mcp_server.build_app()
    async with _running(app2):
        status = await _get(app2, game_server.GAME_PATH)
    assert status == 200


async def test_game_result_endpoint_accepts_a_squad_result(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "BOT_TOKEN", TOKEN)
    app = mcp_server.build_app()
    async with _running(app):
        status, body = await _post_game_result(
            app,
            {
                "initData": _signed_init_data(user_id=43),
                "game": "squad",
                "result": {
                    "distance": 600,
                    "score": 250,
                    "squad": 4,
                    "fighter": "power",
                    "gameTimestamp": 1,
                },
            },
        )
    assert status == 200, body
    assert await fresh_db.get_squad_best_score(43) == 250
    assert await fresh_db.get_game_best_distance(43) == 0


async def test_game_result_endpoint_rejects_an_unknown_game_slug(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "BOT_TOKEN", TOKEN)
    app = mcp_server.build_app()
    async with _running(app):
        status, body = await _post_game_result(
            app,
            {
                "initData": _signed_init_data(user_id=44),
                "game": "tetris",
                "result": {"distance": 1, "score": 1},
            },
        )
    assert status == 400, body


async def test_game_result_endpoint_rejects_squad_over_the_cap(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "BOT_TOKEN", TOKEN)
    app = mcp_server.build_app()
    async with _running(app):
        status, body = await _post_game_result(
            app,
            {
                "initData": _signed_init_data(user_id=45),
                "game": "squad",
                "result": {
                    "distance": 100,
                    "score": 100,
                    "squad": game_server.MAX_SQUAD + 1,
                    "fighter": "power",
                },
            },
        )
    assert status == 400, body


# ---------- миграция старой БД без новых колонок ----------


async def test_old_db_without_game_squad_columns_migrates_rows_to_runner(tmp_path):
    """Строка, записанная до появления колонок game/squad, обязана стать
    раннером после init_db — старые ссылки/кнопки в чатах его и звали."""
    path = str(tmp_path / "legacy_game.db")
    raw = sqlite3.connect(path)
    try:
        raw.execute(
            "CREATE TABLE game_results ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "telegram_id INTEGER NOT NULL, "
            "distance INTEGER NOT NULL, "
            "score INTEGER NOT NULL, "
            "fighter TEXT, "
            "created_at TEXT NOT NULL)"
        )
        raw.execute(
            "INSERT INTO game_results (telegram_id, distance, score, fighter, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (999, 321, 12, "power", "2024-01-01T00:00:00"),
        )
        raw.commit()
    finally:
        raw.close()

    import db

    db._write_lock = asyncio.Lock()
    await db.init_db(path)
    try:
        cur = await db.conn().execute(
            "SELECT game, squad FROM game_results WHERE telegram_id = 999"
        )
        row = await cur.fetchone()
        assert row["game"] == "runner"
        assert row["squad"] == 0
        assert await db.get_game_best_distance(999) == 321
        assert await db.get_squad_best_score(999) == 0
    finally:
        await db.close_db()
