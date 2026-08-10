"""MCP-сервер: что отдаётся наружу, кому и под каким токеном.

Запросы гоняются прямо через ASGI-приложение (без реального сокета), но по
настоящему проводу протокола: тот же JSON-RPC, те же заголовки, та же обёртка с
проверкой токена — то есть ровно то, что увидит Claude на другом конце.
"""

import asyncio
import json
from contextlib import asynccontextmanager, suppress

import pytest

import ai_trainer
import mcp_server
import timeutil


@asynccontextmanager
async def _running(app):
    """Поднять ASGI-приложение (lifespan) и отдать функцию «сделать POST /mcp».

    Lifespan обязателен: в нём стартует session manager транспорта, без него
    первый же запрос падает с «Task group is not initialized» — то есть это
    часть контракта приложения, а не церемония вокруг теста.
    """
    startup: asyncio.Queue = asyncio.Queue()
    shutdown = asyncio.Event()

    started_once = False

    async def receive():
        # Первый вызов — старт, дальше ждём, пока тест доработает: вернуть
        # «shutdown» сразу означало бы погасить session manager до запроса.
        nonlocal started_once
        if not started_once:
            started_once = True
            return {"type": "lifespan.startup"}
        await shutdown.wait()
        return {"type": "lifespan.shutdown"}

    async def send(message):
        if message["type"].startswith("lifespan.startup."):
            await startup.put(message)

    async def lifespan():
        await app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)

    task = asyncio.create_task(lifespan())
    started = await asyncio.wait_for(startup.get(), timeout=5)
    assert started["type"] == "lifespan.startup.complete", started
    try:
        yield lambda body, headers: _asgi_post(app, body, headers)
    finally:
        shutdown.set()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=5)


async def _asgi_post(app, body: dict, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
    """Один POST /mcp по настоящему ASGI-интерфейсу приложения."""
    payload = json.dumps(body).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": mcp_server.MCP_PATH,
        "raw_path": mcp_server.MCP_PATH.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 80),
    }
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    response_headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    payload_out = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], response_headers, payload_out


def _rpc(method: str, params: dict | None = None) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return body


def _headers(token: str | None, host: str = "training-log.example.com") -> dict[str, str]:
    headers = {
        "host": host,
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "mcp-protocol-version": "2025-06-18",
    }
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    return headers


# ---------- что вообще выставлено наружу ----------

def test_whitelist_contains_only_real_tools():
    """Белый список сверяется с инструментами AI-тренера: опечатка в имени —
    это молча не работающий инструмент, а лишнее имя — дыра наружу."""
    declared = {t["function"]["name"] for t in ai_trainer.TOOLS}
    assert declared >= mcp_server.EXPOSED_TOOLS
    assert declared >= mcp_server.WRITE_TOOLS
    # Пишущее наружу уходит только из ai_trainer._UNDOABLE_TOOLS (делает
    # сразу, откат кнопкой) — остальное, включая переписку с тренером, нет.
    assert set(ai_trainer._UNDOABLE_TOOLS) >= mcp_server.WRITE_TOOLS
    for forbidden in ("save_athlete_profile", "propose_program", "get_full_chat_history"):
        assert forbidden in declared, "инструмент переименовали — проверь белый список"
        assert forbidden not in mcp_server.EXPOSED_TOOLS


async def test_server_exposes_exactly_the_whitelist():
    tools = await mcp_server.build_server().list_tools()
    assert {t.name for t in tools} == set(mcp_server.EXPOSED_TOOLS)
    # Пустых описаний быть не должно: по ним клиент выбирает инструмент.
    assert all((t.description or "").strip() for t in tools)


# ---------- по проводу ----------

async def test_tool_call_returns_the_owners_data(fresh_db, user_id):
    group_id = await fresh_db.create_muscle_group(user_id, "Грудь")
    await fresh_db.create_exercise(user_id, "Жим лёжа", group_id)
    token = await fresh_db.issue_mcp_token(user_id)

    async with _running(mcp_server.build_app()) as post:
        status, _, body = await post(
            _rpc("tools/call", {"name": "get_training_overview", "arguments": {}}),
            _headers(token),
        )

    assert status == 200
    result = json.loads(body)["result"]
    assert "Жим лёжа" in json.dumps(result, ensure_ascii=False)


async def test_log_bodyweight_writes_and_hides_the_undo_button_note(fresh_db, user_id):
    """Единственный пишущий путь наружу — и его нельзя перепутать с чтением:
    вес обязан лечь в базу, а payload не должен обещать кнопку, которой у
    MCP-клиента нет (_UNDO_NOTE рассчитан на модель тренера в Telegram)."""
    token = await fresh_db.issue_mcp_token(user_id)

    async with _running(mcp_server.build_app()) as post:
        status, _, body = await post(
            _rpc("tools/call", {"name": "log_bodyweight", "arguments": {"weight": 78.4}}),
            _headers(token),
        )

    assert status == 200
    text = json.loads(body)["result"]["content"][0]["text"]
    assert "78.4" in text
    assert "кнопка отката" not in text
    # Не «Дневник веса»/«Дневник еды» конкретно: этот текст общий на все шесть
    # WRITE_TOOLS, включая create_exercise и copy_program, у которых таких
    # экранов нет вовсе.
    assert "в самом боте" in text
    logs = await fresh_db.list_bodyweight_logs(user_id)
    assert [log["weight"] for log in logs] == [78.4]


async def test_log_food_writes_a_diary_entry(fresh_db, user_id):
    token = await fresh_db.issue_mcp_token(user_id)

    async with _running(mcp_server.build_app()) as post:
        status, _, body = await post(
            _rpc(
                "tools/call",
                {"name": "log_food", "arguments": {"description": "овсянка с бананом"}},
            ),
            _headers(token),
        )

    assert status == 200
    text = json.loads(body)["result"]["content"][0]["text"]
    assert "кнопка отката" not in text
    assert "в самом боте" in text
    today = timeutil.user_today(await fresh_db.get_user(user_id)).isoformat()
    entries = await fresh_db.list_food_entries(user_id, today)
    assert [e["description"] for e in entries] == ["овсянка с бананом"]


async def test_a_tool_argument_called_name_does_not_collide_with_the_tool_name(
    fresh_db, user_id
):
    """create_exercise и copy_program сами принимают аргумент `name` (название
    упражнения/программы) — с `_call(name, **arguments)` вызов `_call("create_
    exercise", name=name)` падал с «got multiple values for argument 'name'».
    Прямой вызов execute_tool в тестах эту коллизию не ловит вообще — нужен
    именно проход через провод, JSON-RPC tools/call."""
    await fresh_db.create_muscle_group(user_id, "Ноги")
    token = await fresh_db.issue_mcp_token(user_id)

    async with _running(mcp_server.build_app()) as post:
        status, _, body = await post(
            _rpc(
                "tools/call",
                {"name": "create_exercise", "arguments": {"name": "Присед", "group": "Ноги"}},
            ),
            _headers(token),
        )

    assert status == 200
    result = json.loads(body)["result"]
    assert result.get("isError") is not True, result


async def test_create_exercise_writes_it(fresh_db, user_id):
    group = await fresh_db.create_muscle_group(user_id, "Ноги")
    token = await fresh_db.issue_mcp_token(user_id)

    async with _running(mcp_server.build_app()) as post:
        status, _, body = await post(
            _rpc(
                "tools/call",
                {"name": "create_exercise", "arguments": {"name": "Гак-присед", "group": "Ноги"}},
            ),
            _headers(token),
        )

    assert status == 200
    text = json.loads(body)["result"]["content"][0]["text"]
    assert "кнопка отката" not in text
    # Живой прогон поймал именно тут: первая версия _MCP_WRITE_NOTE была
    # скопирована с log_bodyweight/log_food и говорила «поправить можно в
    # 🏋️ Дневник веса или 🍽 Дневник еды» — неправда для упражнения, у него
    # такого экрана нет вовсе.
    assert "Дневник веса" not in text
    assert "Дневник еды" not in text
    assert await fresh_db.find_exercise_by_name(user_id, "Гак-присед") is not None
    assert group is not None


async def test_copy_program_writes_a_duplicate(fresh_db, user_id):
    program_id = await fresh_db.create_program(user_id, "PPL")
    await fresh_db.create_routine(user_id, "Жим", program_id=program_id)
    token = await fresh_db.issue_mcp_token(user_id)

    async with _running(mcp_server.build_app()) as post:
        status, _, body = await post(
            _rpc("tools/call", {"name": "copy_program", "arguments": {"name": "PPL"}}),
            _headers(token),
        )

    assert status == 200
    text = json.loads(body)["result"]["content"][0]["text"]
    assert "кнопка отката" not in text
    names = sorted(p["program_name"] for p in await fresh_db.list_programs(user_id))
    assert names == ["PPL", "PPL (2)"]


async def test_delete_food_entry_removes_it(fresh_db, user_id):
    """Живой запрос, который завёл этот раунд правок: «удали эту запись» из
    Claude Desktop упиралось в «у меня нет инструмента на удаление»."""
    entry_id = await fresh_db.add_food_entry(
        user_id, eaten_on="2026-08-08", description="Ritter Sport, 5 литров"
    )
    token = await fresh_db.issue_mcp_token(user_id)

    async with _running(mcp_server.build_app()) as post:
        status, _, body = await post(
            _rpc("tools/call", {"name": "delete_food_entry", "arguments": {"entry_id": entry_id}}),
            _headers(token),
        )

    assert status == 200
    text = json.loads(body)["result"]["content"][0]["text"]
    assert "кнопка отката" not in text
    assert await fresh_db.get_food_entry(entry_id) is None


async def test_delete_bodyweight_log_removes_it(fresh_db, user_id):
    log_id = await fresh_db.add_bodyweight_log(user_id, 78.4)
    token = await fresh_db.issue_mcp_token(user_id)

    async with _running(mcp_server.build_app()) as post:
        status, _, body = await post(
            _rpc(
                "tools/call",
                {"name": "delete_bodyweight_log", "arguments": {"log_id": log_id}},
            ),
            _headers(token),
        )

    assert status == 200
    text = json.loads(body)["result"]["content"][0]["text"]
    assert "кнопка отката" not in text
    assert await fresh_db.list_bodyweight_logs(user_id) == []


async def test_token_only_opens_its_own_data(fresh_db, user_id):
    """Токен — это и есть личность: чужой токен обязан приводить к чужим данным,
    а не к данным того, кто первым пришёл."""
    group_id = await fresh_db.create_muscle_group(user_id, "Грудь")
    await fresh_db.create_exercise(user_id, "Жим лёжа", group_id)
    other = await fresh_db.get_or_create_user(telegram_id=222, username="other")
    other_token = await fresh_db.issue_mcp_token(other["telegram_id"])

    async with _running(mcp_server.build_app()) as post:
        status, _, body = await post(
            _rpc("tools/call", {"name": "get_training_overview", "arguments": {}}),
            _headers(other_token),
        )

    assert status == 200
    assert "Жим лёжа" not in json.dumps(json.loads(body)["result"], ensure_ascii=False)


@pytest.mark.parametrize("token", [None, "", "somebody-elses-token"])
async def test_requests_without_a_valid_token_are_rejected(fresh_db, user_id, token):
    await fresh_db.issue_mcp_token(user_id)
    async with _running(mcp_server.build_app()) as post:
        status, headers, _ = await post(_rpc("tools/list"), _headers(token))
    assert status == 401
    # Без WWW-Authenticate клиент показывает голый 401 и не подсказывает, чего от
    # него хотят (RFC 6750).
    assert "bearer" in headers.get("www-authenticate", "").lower()


async def test_revoked_token_stops_working(fresh_db, user_id):
    token = await fresh_db.issue_mcp_token(user_id)
    await fresh_db.revoke_mcp_token(user_id)
    async with _running(mcp_server.build_app()) as post:
        status, _, _ = await post(_rpc("tools/list"), _headers(token))
    assert status == 401


async def test_tools_list_over_the_wire(fresh_db, user_id):
    token = await fresh_db.issue_mcp_token(user_id)
    async with _running(mcp_server.build_app()) as post:
        status, _, body = await post(_rpc("tools/list"), _headers(token))
    assert status == 200
    names = {t["name"] for t in json.loads(body)["result"]["tools"]}
    assert names == set(mcp_server.EXPOSED_TOOLS)


async def test_public_host_is_not_rejected(fresh_db, user_id):
    """Регрессия на деплой: без явной настройки SDK включает DNS-rebinding-защиту
    со списком из одного localhost, и за прокси хостинга каждый запрос получает
    421 — фича мертва ровно там, где она и нужна."""
    token = await fresh_db.issue_mcp_token(user_id)
    async with _running(mcp_server.build_app()) as post:
        status, _, _ = await post(
            _rpc("tools/list"), _headers(token, host="training-log.amvera.io")
        )
    assert status == 200


async def test_call_refuses_tools_outside_the_whitelist(fresh_db, user_id):
    """Страховка на случай, если инструмент добавят мимо белого списка.

    Белый список проверяется до личности — потому эта страховка и работает даже
    там, где авторизацию смонтировали неправильно."""
    with pytest.raises(ValueError, match="not exposed"):
        await mcp_server._call("propose_program")


async def test_call_without_an_authenticated_user_raises_unauthorized(fresh_db, user_id):
    """Личность приходит из контекста, который выставляет middleware SDK. Вне
    запроса её нет — и инструмент обязан отказать, а не отдать чьи-то данные."""
    with pytest.raises(mcp_server.Unauthorized):
        await mcp_server._call("get_training_overview")


async def test_serve_restarts_after_a_crash_instead_of_dying_silently(monkeypatch):
    """Регрессия: server.serve() падал без обёртки retry — /mcp тихо умирал до
    следующего редеплоя, а бот в Telegram продолжал отвечать как ни в чём не
    бывало (см. admin_tasks.run_oauth_purge_job — тот же паттерн retry-loop)."""
    monkeypatch.setattr(mcp_server, "build_app", lambda: object())
    monkeypatch.setattr(mcp_server, "MCP_RESTART_DELAY_SECONDS", 0)
    attempts = []

    class FlakyServer:
        def __init__(self, config):
            pass

        async def serve(self):
            attempts.append(1)
            if len(attempts) < 2:
                raise RuntimeError("port taken")
            return  # чистая остановка на второй попытке

    monkeypatch.setattr(mcp_server.uvicorn, "Server", FlakyServer)
    monkeypatch.setattr(mcp_server.uvicorn, "Config", lambda *a, **kw: None)

    await mcp_server.serve()

    assert len(attempts) == 2
