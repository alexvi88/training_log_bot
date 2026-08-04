"""OAuth к MCP: подключение бота как обычного коннектора.

Проверяется то, что ломается молча. Флоу гоняется целиком по проводу — через
ASGI-приложение, без реального сокета, но теми же запросами, что пришлют
claude.ai и ChatGPT: регистрация клиента, `/authorize`, страница согласия с
кодом из бота, `/token`, вызов инструмента выданным токеном.

Отдельно — границы, за которыми доступ обязан не работать: повторный обмен кода,
чужой `redirect_uri`, неверный PKCE-верификатор, перебор кода связывания,
отключённое приложение. И регресс на статический токен: он тут же, рядом, и
сломать его новым механизмом легче всего.

Лайфспан приложения и один POST на /mcp берём из tests/test_mcp_server.py —
там же, где они уже работают: две реализации транспорта разъезжаются, а
проверять этим тестам надо не транспорт.
"""

import base64
import hashlib
import json
import logging
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from test_mcp_server import _asgi_post, _headers, _rpc, _running

import config
import mcp_oauth
import mcp_server

pytestmark = pytest.mark.asyncio

REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"
PUBLIC_URL = "https://training-log.example.com"
RESOURCE = f"{PUBLIC_URL}/mcp"


@pytest.fixture(autouse=True)
def public_url(monkeypatch):
    """OAuth без HTTPS-адреса не собирается — issuer обязан быть публичным."""
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", PUBLIC_URL)


# ---------- запросы ----------


async def _asgi(
    app,
    method: str,
    path: str,
    *,
    query: str = "",
    form: dict | None = None,
    json_body: dict | None = None,
    token: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Один HTTP-запрос к приложению по настоящему ASGI-интерфейсу."""
    body = b""
    headers = {"host": "training-log.example.com"}
    if form is not None:
        body = urlencode(form).encode()
        headers["content-type"] = "application/x-www-form-urlencoded"
    elif json_body is not None:
        body = json.dumps(json_body).encode()
        headers["content-type"] = "application/json"
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()],
        "client": ("127.0.0.1", 12345),
        "server": ("training-log.example.com", 443),
    }
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    response_headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], response_headers, payload


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


async def _register(app, name: str = "Claude", redirect_uri: str = REDIRECT_URI) -> str:
    """Динамическая регистрация: клиент приходит без client_id и получает его."""
    status, _, body = await _asgi(
        app,
        "POST",
        "/register",
        json_body={
            "client_name": name,
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    assert status in (200, 201), body
    return json.loads(body)["client_id"]


async def _authorize(app, client_id: str, challenge: str, redirect_uri: str = REDIRECT_URI):
    """GET /authorize → редирект на страницу согласия."""
    return await _asgi(
        app,
        "GET",
        "/authorize",
        query=urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "state-42",
                "resource": RESOURCE,
            }
        ),
    )


def _request_id(location: str) -> str:
    return parse_qs(urlparse(location).query)["request"][0]


async def _consent(app, request_id: str, code: str, action: str = "allow"):
    return await _asgi(
        app,
        "POST",
        mcp_oauth.CONSENT_PATH,
        form={"request": request_id, "code": code, "action": action},
    )


async def _exchange_code(app, client_id: str, code: str, verifier: str, **overrides):
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
        "resource": RESOURCE,
    }
    form.update(overrides)
    return await _asgi(app, "POST", "/token", form=form)


async def _connect(app, user_id: int, name: str = "Claude") -> dict:
    """Пройти весь путь подключения и вернуть выданную пару токенов."""
    client_id = await _register(app, name=name)
    verifier, challenge = _pkce()
    status, headers, _ = await _authorize(app, client_id, challenge)
    assert status == 302
    request_id = _request_id(headers["location"])
    link_code = await mcp_oauth.issue_link_code(user_id)
    status, headers, _ = await _consent(app, request_id, link_code)
    assert status == 302, headers
    code = parse_qs(urlparse(headers["location"]).query)["code"][0]
    status, _, body = await _exchange_code(app, client_id, code, verifier)
    assert status == 200, body
    tokens = json.loads(body)
    tokens["client_id"] = client_id
    return tokens


async def _tool_call(app, token: str):
    return await _asgi_post(
        app,
        _rpc("tools/call", {"name": "get_training_overview", "arguments": {}}),
        _headers(token),
    )


# ---------- метаданные ----------


async def test_metadata_tells_the_client_where_to_authorize(fresh_db, user_id):
    """С этого начинается любое подключение: клиент читает метаданные и только по
    ним узнаёт адреса. Пустой или неполный документ — коннектор, который не
    добавляется, без каких-либо объяснений человеку."""
    app = mcp_server.build_app()
    async with _running(app):
        status, _, body = await _asgi(app, "GET", "/.well-known/oauth-authorization-server")

    assert status == 200
    metadata = json.loads(body)
    assert metadata["authorization_endpoint"] == f"{PUBLIC_URL}/authorize"
    assert metadata["token_endpoint"] == f"{PUBLIC_URL}/token"
    # Без динамической регистрации ни Claude, ни ChatGPT подключиться не смогут:
    # вписать client_id человеку негде.
    assert metadata["registration_endpoint"] == f"{PUBLIC_URL}/register"
    assert "S256" in metadata["code_challenge_methods_supported"]


async def test_the_resource_points_back_at_the_authorization_server(fresh_db, user_id):
    """RFC 9728: по 401 клиент идёт за метаданными ресурса и оттуда узнаёт, где
    авторизовываться. Если ресурс не назвал свой AS, флоу не начнётся."""
    app = mcp_server.build_app()
    async with _running(app):
        status, _, body = await _asgi(
            app, "GET", "/.well-known/oauth-protected-resource/mcp"
        )

    assert status == 200
    metadata = json.loads(body)
    assert metadata["resource"] == RESOURCE
    # Без слеша на конце: RFC 8414 сравнивает issuer посимвольно, и лишний слеш
    # здесь означал бы, что клиент не найдёт метаданные AS.
    assert metadata["authorization_servers"] == [PUBLIC_URL]


# ---------- флоу целиком ----------


async def test_the_whole_flow_ends_with_the_owners_data(fresh_db, user_id):
    """Главный тест: регистрация → согласие с кодом из бота → токен → данные.

    Каждый шаг здесь — то, что делает claude.ai сам, без участия человека, кроме
    шести цифр на странице согласия."""
    group_id = await fresh_db.create_muscle_group(user_id, "Грудь")
    await fresh_db.create_exercise(user_id, "Жим лёжа", group_id)
    app = mcp_server.build_app()

    async with _running(app):
        tokens = await _connect(app, user_id)
        status, _, body = await _tool_call(app, tokens["access_token"])

    assert tokens["token_type"] == "Bearer"
    assert tokens["refresh_token"]
    assert status == 200
    assert "Жим лёжа" in json.dumps(json.loads(body)["result"], ensure_ascii=False)


async def test_the_code_from_the_bot_decides_whose_data_it_is(fresh_db, user_id):
    """Личность приходит из кода связывания, а не из клиента: одно и то же
    приложение, подключённое чужим кодом, обязано открыть чужие данные."""
    group_id = await fresh_db.create_muscle_group(user_id, "Грудь")
    await fresh_db.create_exercise(user_id, "Жим лёжа", group_id)
    other = await fresh_db.get_or_create_user(telegram_id=222, username="other")
    app = mcp_server.build_app()

    async with _running(app):
        tokens = await _connect(app, other["telegram_id"])
        status, _, body = await _tool_call(app, tokens["access_token"])

    assert status == 200
    assert "Жим лёжа" not in json.dumps(json.loads(body)["result"], ensure_ascii=False)


async def test_consent_page_names_the_app_and_what_it_gets(fresh_db, user_id):
    """Страница согласия — единственное место, где человек видит, что именно
    отдаёт. Без имени приложения и перечня данных это кнопка «разрешить всё»."""
    app = mcp_server.build_app()
    async with _running(app):
        client_id = await _register(app, name="ChatGPT")
        _, headers, _ = await _authorize(app, client_id, _pkce()[1])
        status, _, body = await _asgi(
            app, "GET", mcp_oauth.CONSENT_PATH, query=f"request={_request_id(headers['location'])}"
        )

    page = body.decode()
    assert status == 200
    assert "ChatGPT" in page
    assert "только на чтение" in page
    assert "тренировки и подходы" in page
    # И страница не посылает никуда за кодом: он уже лежит в том сообщении бота,
    # из которого человек сюда пришёл. Разъехавшись с ботом, страница начинает
    # спорить с экраном, открытым у него на соседнем устройстве.
    assert "в том же сообщении" in page


async def test_a_stale_consent_link_is_a_dead_end(fresh_db, user_id):
    """Ссылку на согласие можно открыть через сутки из истории браузера — и она
    не должна вести к выдаче доступа."""
    app = mcp_server.build_app()
    async with _running(app):
        status, _, body = await _asgi(
            app, "GET", mcp_oauth.CONSENT_PATH, query="request=не-было-такой-заявки"
        )

    assert status == 400
    assert "устарел" in body.decode()


async def test_refusal_takes_the_client_back_with_an_error(fresh_db, user_id):
    """«Отмена» — это ответ, а не тупик: клиент обязан узнать про отказ по
    редиректу, иначе он будет ждать вечно."""
    app = mcp_server.build_app()
    async with _running(app):
        client_id = await _register(app)
        _, headers, _ = await _authorize(app, client_id, _pkce()[1])
        request_id = _request_id(headers["location"])
        status, headers, _ = await _consent(app, request_id, "000000", action="deny")

    assert status == 302
    query = parse_qs(urlparse(headers["location"]).query)
    assert query["error"] == ["access_denied"]
    assert query["state"] == ["state-42"]


# ---------- границы обмена ----------


async def test_an_authorization_code_cannot_be_exchanged_twice(fresh_db, user_id):
    """Перехваченный код не должен работать вторым обменом (RFC 6749 §10.5)."""
    app = mcp_server.build_app()
    async with _running(app):
        client_id = await _register(app)
        verifier, challenge = _pkce()
        _, headers, _ = await _authorize(app, client_id, challenge)
        link_code = await mcp_oauth.issue_link_code(user_id)
        _, headers, _ = await _consent(app, _request_id(headers["location"]), link_code)
        code = parse_qs(urlparse(headers["location"]).query)["code"][0]

        first_status, _, _ = await _exchange_code(app, client_id, code, verifier)
        second_status, _, second_body = await _exchange_code(app, client_id, code, verifier)

    assert first_status == 200
    assert second_status == 400
    assert json.loads(second_body)["error"] == "invalid_grant"


async def test_a_wrong_pkce_verifier_is_rejected(fresh_db, user_id):
    """PKCE — единственное, что отличает нашего клиента от того, кто перехватил
    редирект: у него есть код, но нет верификатора."""
    app = mcp_server.build_app()
    async with _running(app):
        client_id = await _register(app)
        _, challenge = _pkce()
        _, headers, _ = await _authorize(app, client_id, challenge)
        link_code = await mcp_oauth.issue_link_code(user_id)
        _, headers, _ = await _consent(app, _request_id(headers["location"]), link_code)
        code = parse_qs(urlparse(headers["location"]).query)["code"][0]

        status, _, body = await _exchange_code(app, client_id, code, secrets.token_urlsafe(32))

    assert status == 400
    assert json.loads(body)["error"] == "invalid_grant"


async def test_a_foreign_redirect_uri_never_reaches_consent(fresh_db, user_id):
    """Чужой redirect_uri — это попытка увести код себе. Отказ обязан случиться
    до страницы согласия: человеку нечего подтверждать."""
    app = mcp_server.build_app()
    async with _running(app):
        client_id = await _register(app)
        status, headers, _ = await _authorize(
            app, client_id, _pkce()[1], redirect_uri="https://attacker.example.com/catch"
        )

    assert status == 400
    assert "location" not in headers


async def test_the_redirect_uri_must_match_at_the_token_step_too(fresh_db, user_id):
    """Второй рубеж той же проверки (RFC 6749 §10.6): код, выданный на один
    адрес, не обменивается с другим."""
    app = mcp_server.build_app()
    async with _running(app):
        client_id = await _register(app)
        verifier, challenge = _pkce()
        _, headers, _ = await _authorize(app, client_id, challenge)
        link_code = await mcp_oauth.issue_link_code(user_id)
        _, headers, _ = await _consent(app, _request_id(headers["location"]), link_code)
        code = parse_qs(urlparse(headers["location"]).query)["code"][0]

        status, _, _ = await _exchange_code(
            app, client_id, code, verifier, redirect_uri="https://attacker.example.com/catch"
        )

    assert status == 400


# ---------- refresh ----------


async def test_refresh_rotates_both_tokens_and_kills_the_old_pair(fresh_db, user_id):
    """Ротация — то, ради чего refresh вообще существует: украденный refresh
    перестаёт работать после первого же обновления настоящим клиентом."""
    app = mcp_server.build_app()
    async with _running(app):
        first = await _connect(app, user_id)
        status, _, body = await _asgi(
            app,
            "POST",
            "/token",
            form={
                "grant_type": "refresh_token",
                "refresh_token": first["refresh_token"],
                "client_id": first["client_id"],
                "resource": RESOURCE,
            },
        )
        assert status == 200, body
        second = json.loads(body)

        replay_status, _, replay_body = await _asgi(
            app,
            "POST",
            "/token",
            form={
                "grant_type": "refresh_token",
                "refresh_token": first["refresh_token"],
                "client_id": first["client_id"],
            },
        )
        new_token_status, _, _ = await _tool_call(app, second["access_token"])
        old_token_status, _, _ = await _tool_call(app, first["access_token"])

    assert second["access_token"] != first["access_token"]
    assert second["refresh_token"] != first["refresh_token"]
    assert replay_status == 400
    assert json.loads(replay_body)["error"] == "invalid_grant"
    assert new_token_status == 200
    # Прежний access тоже умер: пара в базе одна, и ротация гасит её целиком.
    assert old_token_status == 401


# ---------- код связывания ----------


async def test_an_expired_link_code_opens_nothing(fresh_db, user_id):
    app = mcp_server.build_app()
    async with _running(app):
        client_id = await _register(app)
        _, headers, _ = await _authorize(app, client_id, _pkce()[1])
        request_id = _request_id(headers["location"])
        link_code = await mcp_oauth.issue_link_code(user_id)
        await fresh_db.conn().execute(
            "UPDATE oauth_link_codes SET expires_at = ? WHERE code = ?",
            (time.time() - 1, link_code),
        )
        await fresh_db.conn().commit()

        status, headers, body = await _consent(app, request_id, link_code)

    assert status == 400
    assert "location" not in headers
    assert "Код не подошёл" in body.decode()


async def test_a_link_code_works_exactly_once(fresh_db, user_id):
    """Иначе один код подключал бы сколько угодно приложений — в том числе
    чужих, если человек его переслал вместе со скриншотом."""
    app = mcp_server.build_app()
    async with _running(app):
        link_code = await mcp_oauth.issue_link_code(user_id)
        first_client = await _register(app, name="Claude")
        _, headers, _ = await _authorize(app, first_client, _pkce()[1])
        first_status, _, _ = await _consent(app, _request_id(headers["location"]), link_code)

        second_client = await _register(app, name="ChatGPT")
        _, headers, _ = await _authorize(app, second_client, _pkce()[1])
        second_status, _, _ = await _consent(app, _request_id(headers["location"]), link_code)

    assert first_status == 302
    assert second_status == 400


async def test_five_wrong_attempts_lock_the_request_even_for_the_right_code(fresh_db, user_id):
    """Шесть цифр перебираются за минуты, если пробовать можно бесконечно.
    Заперта именно заявка: правильный код после лимита тоже не проходит, и
    подключение приходится начинать заново."""
    app = mcp_server.build_app()
    async with _running(app):
        client_id = await _register(app)
        _, headers, _ = await _authorize(app, client_id, _pkce()[1])
        request_id = _request_id(headers["location"])
        link_code = await mcp_oauth.issue_link_code(user_id)

        for _ in range(mcp_oauth.LINK_CODE_MAX_ATTEMPTS):
            status, _, body = await _consent(app, request_id, "000000")
            assert status == 400
            assert "Код не подошёл" in body.decode()

        status, headers, body = await _consent(app, request_id, link_code)

    assert status == 400
    assert "location" not in headers
    assert "Слишком много попыток" in body.decode()


async def test_a_new_code_replaces_the_previous_one(fresh_db, user_id):
    """«Нажал кнопку дважды» не должно оставлять позади действующий код."""
    first = await mcp_oauth.issue_link_code(user_id)
    second = await mcp_oauth.issue_link_code(user_id)

    assert first != second
    assert await fresh_db.verify_oauth_link_code("нет-заявки", first, 5) == (
        "unknown_request",
        None,
    )
    cur = await fresh_db.conn().execute("SELECT code FROM oauth_link_codes")
    assert [row["code"] for row in await cur.fetchall()] == [second]


async def test_the_code_is_six_digits_and_lives_as_long_as_promised(fresh_db, user_id):
    """Шесть цифр — это то, что человек перенабирает с экрана телефона в браузер
    без ошибок. И столько минут, сколько ему обещал текст в боте: срок в базе и
    срок в тексте берутся из одной константы, иначе «код истёк» приходит раньше,
    чем человек этого ждёт."""
    before = time.time()
    code = await mcp_oauth.issue_link_code(user_id)

    assert len(code) == 6
    assert code.isdigit()
    cur = await fresh_db.conn().execute(
        "SELECT expires_at FROM oauth_link_codes WHERE code = ?", (code,)
    )
    assert (await cur.fetchone())["expires_at"] >= before + mcp_oauth.LINK_CODE_TTL


# ---------- отзыв ----------


async def test_disconnecting_an_app_kills_access_and_refresh(fresh_db, user_id):
    """«Отключить» в боте — единственная кнопка, которой человек закрывает доступ
    конкретному приложению. Она обязана гасить пару целиком: живой refresh
    означает, что через час приложение вернётся."""
    app = mcp_server.build_app()
    async with _running(app):
        tokens = await _connect(app, user_id)
        before, _, _ = await _tool_call(app, tokens["access_token"])

        revoked = await fresh_db.revoke_oauth_client_tokens(user_id, tokens["client_id"])

        after, _, _ = await _tool_call(app, tokens["access_token"])
        refresh_status, _, _ = await _asgi(
            app,
            "POST",
            "/token",
            form={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": tokens["client_id"],
            },
        )

    assert before == 200
    assert revoked == 1
    assert after == 401
    assert refresh_status == 400


async def test_disconnecting_one_app_leaves_the_other_connected(fresh_db, user_id):
    """У человека может быть подключён и браузерный Claude, и ChatGPT. Отзыв
    одного не должен ронять второй — иначе это не отзыв, а поломка."""
    app = mcp_server.build_app()
    async with _running(app):
        claude = await _connect(app, user_id, name="Claude")
        chatgpt = await _connect(app, user_id, name="ChatGPT")

        await fresh_db.revoke_oauth_client_tokens(user_id, claude["client_id"])

        claude_status, _, _ = await _tool_call(app, claude["access_token"])
        chatgpt_status, _, _ = await _tool_call(app, chatgpt["access_token"])

    assert claude_status == 401
    assert chatgpt_status == 200


async def test_the_connections_list_shows_one_row_per_app(fresh_db, user_id):
    """Список в боте строится по клиентам, а не по токенам: после обновления
    токена приложение не должно появляться в нём дважды."""
    app = mcp_server.build_app()
    async with _running(app):
        tokens = await _connect(app, user_id, name="Claude")
        await _asgi(
            app,
            "POST",
            "/token",
            form={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": tokens["client_id"],
            },
        )
        connections = await fresh_db.list_oauth_connections(user_id)

    assert len(connections) == 1
    assert mcp_oauth.client_display_name(
        connections[0]["metadata"], connections[0]["client_id"]
    ) == "Claude"


async def test_revocation_endpoint_kills_the_pair(fresh_db, user_id):
    """Клиент умеет отзывать сам (RFC 7009) — «удалил коннектор» должно закрывать
    доступ, не дожидаясь, что человек вспомнит про кнопку в боте."""
    app = mcp_server.build_app()
    async with _running(app):
        tokens = await _connect(app, user_id)
        status, _, _ = await _asgi(
            app,
            "POST",
            "/revoke",
            # client_secret обязателен в модели запроса SDK даже у публичного
            # клиента, у которого секрета нет: пустая строка — то, что он и
            # пришлёт.
            form={
                "token": tokens["access_token"],
                "client_id": tokens["client_id"],
                "client_secret": "",
            },
        )
        after, _, _ = await _tool_call(app, tokens["access_token"])
        refresh_status, _, _ = await _asgi(
            app,
            "POST",
            "/token",
            form={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": tokens["client_id"],
            },
        )

    assert status == 200
    assert after == 401
    # Парный refresh умер вместе с access — иначе отзыв ничего не закрывает.
    assert refresh_status == 400


# ---------- совместимость и гигиена ----------


async def test_the_static_token_still_works_next_to_oauth(fresh_db, user_id):
    """Регресс на то, что уже работало: Claude Code со статическим токеном
    обязан продолжать читать данные, пока рядом живёт OAuth."""
    group_id = await fresh_db.create_muscle_group(user_id, "Грудь")
    await fresh_db.create_exercise(user_id, "Жим лёжа", group_id)
    static_token = await fresh_db.issue_mcp_token(user_id)
    app = mcp_server.build_app()

    async with _running(app):
        oauth_tokens = await _connect(app, user_id)
        static_status, _, static_body = await _tool_call(app, static_token)
        oauth_status, _, _ = await _tool_call(app, oauth_tokens["access_token"])

        # Отзыв статического токена не трогает коннектор: механизмы разные, и
        # «перевыпустить токен» не должно ронять подключённые приложения.
        await fresh_db.revoke_mcp_token(user_id)
        static_after, _, _ = await _tool_call(app, static_token)
        oauth_after, _, _ = await _tool_call(app, oauth_tokens["access_token"])

    assert static_status == 200
    assert "Жим лёжа" in json.dumps(json.loads(static_body)["result"], ensure_ascii=False)
    assert oauth_status == 200
    assert static_after == 401
    assert oauth_after == 200


async def test_an_expired_access_token_stops_working(fresh_db, user_id):
    """Срок жизни access — то, что делает утечку токена конечной."""
    app = mcp_server.build_app()
    async with _running(app):
        tokens = await _connect(app, user_id)
        await fresh_db.conn().execute(
            "UPDATE oauth_tokens SET expires_at = ? WHERE access_token = ?",
            (time.time() - 1, tokens["access_token"]),
        )
        await fresh_db.conn().commit()
        status, _, _ = await _tool_call(app, tokens["access_token"])

    assert status == 401


async def test_secrets_do_not_reach_the_logs(fresh_db, user_id, caplog):
    """В базе токены лежат открытыми — отдельного хранилища секретов у бота нет.
    Единственное, что можно сделать сверх этого: не разносить их по логам, куда
    заглядывают чаще, чем в базу, и которые уезжают в бэкапы и трейсы.

    Уровень — INFO, на котором бот и работает (main.basicConfig). На DEBUG
    aiosqlite печатает каждый запрос вместе с параметрами, то есть и токены
    тоже: это свойство драйвера, и обещать тут нечего, кроме «не включать DEBUG
    в проде».
    """
    app = mcp_server.build_app()
    with caplog.at_level(logging.INFO):
        async with _running(app):
            link_code = await mcp_oauth.issue_link_code(user_id)
            client_id = await _register(app)
            verifier, challenge = _pkce()
            _, headers, _ = await _authorize(app, client_id, challenge)
            _, headers, _ = await _consent(app, _request_id(headers["location"]), link_code)
            code = parse_qs(urlparse(headers["location"]).query)["code"][0]
            _, _, body = await _exchange_code(app, client_id, code, verifier)
            tokens = json.loads(body)
            await _tool_call(app, tokens["access_token"])

    logs = caplog.text
    for secret in (
        link_code,
        code,
        verifier,
        tokens["access_token"],
        tokens["refresh_token"],
    ):
        assert secret not in logs
    # При этом факт события в логах есть — иначе отладить подключение нечем.
    assert "MCP OAuth" in logs


# ---------- прополка ----------


async def test_the_purge_removes_only_the_dead(fresh_db, user_id):
    """Брошенное на полпути подключение не гасит за собой ничего, и без прополки
    таблицы только растут. Живое при этом обязано остаться живым."""
    app = mcp_server.build_app()
    async with _running(app):
        alive = await _connect(app, user_id)

        # Мёртвое: заявка, код авторизации, код связывания и просроченная пара.
        stale = time.time() - 1
        await fresh_db.create_oauth_consent_request(
            request_id="stale",
            client_id=alive["client_id"],
            redirect_uri=REDIRECT_URI,
            redirect_uri_provided_explicitly=True,
            code_challenge="x",
            scopes="[]",
            resource=None,
            state=None,
            expires_at=stale,
        )
        await fresh_db.create_oauth_auth_code(
            code="stale",
            client_id=alive["client_id"],
            user_id=user_id,
            redirect_uri=REDIRECT_URI,
            redirect_uri_provided_explicitly=True,
            code_challenge="x",
            scopes="[]",
            resource=None,
            expires_at=stale,
        )
        await fresh_db.create_oauth_token(
            access_token="stale-access",
            refresh_token="stale-refresh",
            client_id=alive["client_id"],
            user_id=user_id,
            scopes="[]",
            resource=None,
            expires_at=stale,
            refresh_expires_at=stale,
        )
        code = await mcp_oauth.issue_link_code(user_id)
        await fresh_db.conn().execute(
            "UPDATE oauth_link_codes SET expires_at = ? WHERE code = ?", (stale, code)
        )
        await fresh_db.conn().commit()

        deleted = await fresh_db.purge_expired_oauth()
        status, _, _ = await _tool_call(app, alive["access_token"])

    assert deleted == 4
    assert status == 200
    assert await fresh_db.get_oauth_auth_code("stale") is None
    assert await fresh_db.get_oauth_consent_request("stale") is None
    assert await fresh_db.get_oauth_access_token("stale-access") is None
    assert len(await fresh_db.list_oauth_connections(user_id)) == 1
