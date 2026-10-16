"""MCP-сервер в публичных каталогах коннекторов (docs/MCP_LISTING.md).

Каталоги Claude и ChatGPT проверяют сервер автоматически, и то, что они
смотрят, здесь закреплено тестом: аннотации у каждого инструмента, имя и
иконка сервера, метаданные OAuth на каноническом адресе, файлы подтверждения
домена, публичная инструкция и `server.json` для MCP Registry. Сломать
любое из этого можно молча — коннектор продолжит работать у тех, кто уже
подключён, а новый листинг завернут.
"""

import html
import json
import re
import tomllib
from pathlib import Path

import pytest
from test_mcp_oauth import _asgi
from test_mcp_server import _headers, _rpc, _running

import ai_trainer
import config
import i18n
import legal_pages
import mcp_server
import mcp_wellknown

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_URL = "https://athletediary.app"


@pytest.fixture(autouse=True)
def canonical_urls(monkeypatch):
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", PUBLIC_URL)
    monkeypatch.setattr(config, "SITE_BASE_URL", PUBLIC_URL)
    mcp_server._tool_calls.clear()
    yield
    mcp_server._tool_calls.clear()


# ---------- аннотации ----------


def test_every_exposed_tool_has_explicit_annotations():
    """Claude: title + readOnlyHint/destructiveHint; ChatGPT: явные
    readOnlyHint, destructiveHint и openWorldHint у всех — иначе отказ."""
    assert set(mcp_server.TOOL_ANNOTATIONS) == set(mcp_server.EXPOSED_TOOLS)
    for name, ann in mcp_server.TOOL_ANNOTATIONS.items():
        assert (ann.title or "").strip(), name
        for hint in (ann.read_only_hint, ann.destructive_hint, ann.idempotent_hint, ann.open_world_hint):
            assert hint is not None, name
        assert ann.read_only_hint is (name in mcp_server.READ_ONLY_TOOLS), name
        assert ann.open_world_hint is False, name
        if ann.read_only_hint:
            assert ann.destructive_hint is False, name
    # Удаляет данные ровно один инструмент — и он обязан спрашивать человека.
    destructive = {n for n, a in mcp_server.TOOL_ANNOTATIONS.items() if a.destructive_hint}
    assert destructive == {"delete_bodyweight_log"}


async def test_tools_list_over_the_wire_carries_annotations(fresh_db, user_id):
    token = await fresh_db.issue_mcp_token(user_id)
    async with _running(mcp_server.build_app()) as post:
        status, _, body = await post(_rpc("tools/list"), _headers(token))
    assert status == 200
    tools = json.loads(body)["result"]["tools"]
    assert {t["name"] for t in tools} == set(mcp_server.EXPOSED_TOOLS)
    for tool in tools:
        ann = tool["annotations"]
        assert ann["title"] and tool["title"] == ann["title"]
        assert set(ann) >= {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
        # Описание — английский текст для модели, без кириллицы.
        assert not re.search("[а-яё]", tool["description"], re.I), tool["name"]
        assert len(tool["name"]) <= 64


def test_tool_descriptions_do_not_talk_about_telegram_only():
    """Коннектор открыт и тем, у кого нет бота (вход через Apple): описание
    «данные пользователя Telegram-бота» было бы неправдой про сервер."""
    server = mcp_server.build_server()
    assert "telegram" not in (server.instructions or "").lower()


async def test_initialize_reports_name_version_and_icon(fresh_db, user_id):
    token = await fresh_db.issue_mcp_token(user_id)
    params = {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    }
    async with _running(mcp_server.build_app()) as post:
        status, _, body = await post(_rpc("initialize", params), _headers(token))
    assert status == 200
    info = json.loads(body)["result"]["serverInfo"]
    assert info["name"] == mcp_server.SERVER_NAME
    assert info["title"] == "Athlete Diary"
    assert info["version"] == mcp_server.SERVER_VERSION
    assert info["websiteUrl"] == PUBLIC_URL
    assert info["icons"][0]["src"] == f"{PUBLIC_URL}/site/icons/icon-512.png"
    assert (ROOT / "media/site/icons/icon-512.png").exists()


# ---------- поведение вызовов ----------


async def test_tool_calls_have_a_per_athlete_ceiling(fresh_db, user_id, monkeypatch):
    """Зациклившийся клиент получает человеческую ошибку на языке атлета,
    а не грузит процесс бота сотней вызовов."""
    monkeypatch.setattr(mcp_server, "TOOL_CALLS_PER_MINUTE", 2)
    await fresh_db.set_user_lang(user_id, "en")
    token = await fresh_db.issue_mcp_token(user_id)
    call = _rpc("tools/call", {"name": "get_bodyweight_history", "arguments": {}})
    async with _running(mcp_server.build_app()) as post:
        for _ in range(2):
            status, _, body = await post(call, _headers(token))
            assert not json.loads(body)["result"].get("isError")
        status, _, body = await post(call, _headers(token))
    result = json.loads(body)["result"]
    assert result.get("isError")
    text = result["content"][0]["text"]
    assert i18n.t_in("en", "mcp.error.rate_limited", n=2) in text


def test_ceiling_window_slides():
    uid = 424242
    limit = mcp_server.TOOL_CALLS_PER_MINUTE
    for i in range(limit):
        assert mcp_server._allow_call(uid, now=1000.0 + i * 0.01)
    assert not mcp_server._allow_call(uid, now=1001.0)
    # Через минуту окно освобождается.
    assert mcp_server._allow_call(uid, now=1000.0 + 61)


async def test_english_athlete_gets_english_catalog(fresh_db, user_id):
    """Язык MCP-вызова — из базы: без этого каталог ехал языком по
    умолчанию (русским) любому атлету."""
    await fresh_db.set_user_lang(user_id, "en")
    token = await fresh_db.issue_mcp_token(user_id)
    async with _running(mcp_server.build_app()) as post:
        _, _, body = await post(
            _rpc("tools/call", {"name": "list_exercise_catalog", "arguments": {}}),
            _headers(token),
        )
    text = json.loads(body)["result"]["content"][0]["text"]
    assert json.loads(text)["catalog"] == json.loads(
        json.dumps(ai_trainer._localized_catalog("en"), ensure_ascii=False)
    )


def test_unauthorized_message_names_the_app_path_too():
    """Аккаунт без бота (вход через Apple) берёт код в приложении — подсказка
    «набери /mcp в боте» была бы для него тупиком."""
    with pytest.raises(mcp_server.Unauthorized) as exc:
        mcp_server._user_id()
    message = str(exc.value)
    assert "Connect to Claude and ChatGPT" in message
    assert "«Подключить к Claude и ChatGPT»" in message
    assert "/mcp" in message


# ---------- OAuth на каноническом адресе ----------


async def test_oauth_metadata_points_at_the_canonical_host():
    """Каноника — athletediary.app. Приложение от хоста не зависит: fly.dev
    отвечает тем же процессом и теми же метаданными, а выданные там токены
    продолжают работать (ресурс токена SDK не сверяет)."""
    app = mcp_server.build_app()
    status, _, body = await _asgi(app, "GET", "/.well-known/oauth-authorization-server")
    assert status == 200
    meta = json.loads(body)
    assert meta["issuer"].rstrip("/") == PUBLIC_URL
    assert meta["authorization_endpoint"] == f"{PUBLIC_URL}/authorize"
    assert meta["registration_endpoint"] == f"{PUBLIC_URL}/register"
    assert "S256" in meta["code_challenge_methods_supported"]
    assert meta["service_documentation"] == f"{PUBLIC_URL}/connect"

    status, _, body = await _asgi(app, "GET", "/.well-known/oauth-protected-resource/mcp")
    assert status == 200
    prm = json.loads(body)
    assert prm["resource"] == f"{PUBLIC_URL}/mcp"
    assert prm["authorization_servers"][0].rstrip("/") == PUBLIC_URL

    # Корневой адрес без пути — тот же документ.
    status, _, body = await _asgi(app, "GET", "/.well-known/oauth-protected-resource")
    assert status == 200
    root = json.loads(body)
    assert root["resource"] == prm["resource"]
    assert [a.rstrip("/") for a in root["authorization_servers"]] == [
        a.rstrip("/") for a in prm["authorization_servers"]
    ]


async def test_unauthenticated_call_gets_401_with_resource_metadata_pointer():
    app = mcp_server.build_app()
    async with _running(app) as post:
        status, headers, _ = await post(_rpc("tools/list"), _headers(None))
    assert status == 401
    assert (
        f'resource_metadata="{PUBLIC_URL}/.well-known/oauth-protected-resource/mcp"'
        in headers["www-authenticate"]
    )


# ---------- подтверждение домена ----------


@pytest.mark.parametrize(
    "path",
    [
        mcp_wellknown.REGISTRY_AUTH_PATH,
        mcp_wellknown.OPENAI_CHALLENGE_PATH,
        mcp_wellknown.GLAMA_PATH,
    ],
)
async def test_verification_files_are_404_until_configured(path, monkeypatch):
    monkeypatch.setattr(config, "MCP_REGISTRY_AUTH", "")
    monkeypatch.setattr(config, "OPENAI_APPS_CHALLENGE", "")
    monkeypatch.setattr(config, "GLAMA_MAINTAINER_EMAIL", "")
    status, _, _ = await _asgi(mcp_server.build_app(), "GET", path)
    assert status == 404


async def test_verification_files_serve_exactly_the_configured_values(monkeypatch):
    proof = "v=MCPv1; k=ed25519; p=MCowBQYDK2VwAyEAexamplepublickeyonly="
    monkeypatch.setattr(config, "MCP_REGISTRY_AUTH", proof)
    monkeypatch.setattr(config, "OPENAI_APPS_CHALLENGE", "tok_123")
    monkeypatch.setattr(config, "GLAMA_MAINTAINER_EMAIL", "owner@example.com")
    app = mcp_server.build_app()

    status, headers, body = await _asgi(app, "GET", mcp_wellknown.REGISTRY_AUTH_PATH)
    assert status == 200 and body.decode().strip() == proof
    assert headers["content-type"].startswith("text/plain")

    status, _, body = await _asgi(app, "GET", mcp_wellknown.OPENAI_CHALLENGE_PATH)
    assert status == 200 and body == b"tok_123"

    status, _, body = await _asgi(app, "GET", mcp_wellknown.GLAMA_PATH)
    assert status == 200
    assert json.loads(body) == {
        "$schema": mcp_wellknown.GLAMA_SCHEMA,
        "maintainers": [{"email": "owner@example.com"}],
    }


async def test_registry_proof_with_a_typo_is_not_served(monkeypatch):
    monkeypatch.setattr(config, "MCP_REGISTRY_AUTH", "k=ed25519; p=abc")
    status, _, _ = await _asgi(mcp_server.build_app(), "GET", mcp_wellknown.REGISTRY_AUTH_PATH)
    assert status == 404


async def test_server_card_lists_the_live_tools():
    status, _, body = await _asgi(mcp_server.build_app(), "GET", mcp_wellknown.SERVER_CARD_PATH)
    assert status == 200
    card = json.loads(body)
    assert card["serverInfo"]["name"] == mcp_server.SERVER_NAME
    assert card["transport"]["url"] == f"{PUBLIC_URL}/mcp"
    assert card["authentication"]["required"] is True
    assert {t["name"] for t in card["tools"]} == set(mcp_server.EXPOSED_TOOLS)
    assert all("annotations" in t for t in card["tools"])
    assert card["privacyPolicyUrl"] == f"{PUBLIC_URL}/privacy"


# ---------- публичная инструкция ----------


@pytest.mark.parametrize("lang", legal_pages.LANGS)
async def test_connect_page_is_public_and_names_the_canonical_address(lang):
    app = mcp_server.build_app()
    status, _, body = await _asgi(app, "GET", "/connect", query=f"lang={lang}")
    assert status == 200
    page = body.decode()
    assert f"<html lang={lang}>" in page
    assert f"{PUBLIC_URL}/mcp" in page
    assert "mailto:" in page
    for tool in mcp_server.EXPOSED_TOOLS:
        assert f"<code>{tool}</code>" in page, tool
    if lang == "en":
        assert not re.search("[а-яё]", page.replace("Русский", ""), re.I)


def test_connect_page_address_is_the_production_one():
    """Адрес в тексте страницы — тот же, что в fly.toml: страница — это
    инструкция для людей, и адрес в ней обязан работать."""
    fly = tomllib.loads((ROOT / "fly.toml").read_text())
    url = fly["env"]["MCP_PUBLIC_URL"] + mcp_server.MCP_PATH
    for lang in legal_pages.LANGS:
        assert url in legal_pages.render(legal_pages.CONNECT_PAGE, lang)


# ---------- server.json для MCP Registry ----------


def test_server_json_matches_the_server():
    data = json.loads((ROOT / "server.json").read_text())
    fly = tomllib.loads((ROOT / "fly.toml").read_text())
    assert data["$schema"].startswith("https://static.modelcontextprotocol.io/schemas/")
    assert re.fullmatch(r"[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+", data["name"])
    # Пространство имён — наш домен задом наперёд (вход через HTTP-файл).
    assert data["name"].split("/")[0] == "app.athletediary"
    assert 1 <= len(data["description"]) <= 100
    assert data["title"] == "Athlete Diary"
    assert data["version"] == mcp_server.SERVER_VERSION
    remotes = data["remotes"]
    assert remotes == [{"type": "streamable-http", "url": fly["env"]["MCP_PUBLIC_URL"] + "/mcp"}]
    for icon in data.get("icons", []):
        assert icon["src"].startswith("https://") and len(icon["src"]) <= 255


# ---------- код для ревьюеров каталогов ----------


@pytest.fixture
def demo_secrets(monkeypatch):
    import review_demo

    monkeypatch.setattr(config, "REVIEW_DEMO_USERNAME", "reviewer")
    monkeypatch.setattr(config, "REVIEW_DEMO_PASSWORD", "s3cret-pass")
    monkeypatch.setattr(config, "MCP_DEMO_CODE_ENABLED", True)
    monkeypatch.setattr(config, "WALK_DEMO_USERNAME", "")
    monkeypatch.setattr(config, "WALK_DEMO_PASSWORD", "")
    review_demo.reset_failures()
    yield
    review_demo.reset_failures()


async def test_demo_code_page_is_absent_without_demo_secrets(monkeypatch):
    import mcp_oauth

    monkeypatch.setattr(config, "REVIEW_DEMO_USERNAME", "")
    monkeypatch.setattr(config, "REVIEW_DEMO_PASSWORD", "")
    monkeypatch.setattr(config, "WALK_DEMO_USERNAME", "")
    monkeypatch.setattr(config, "WALK_DEMO_PASSWORD", "")
    status, _, _ = await _asgi(mcp_server.build_app(), "GET", mcp_oauth.DEMO_CODE_PATH)
    assert status == 404


async def test_reviewer_gets_a_code_that_the_consent_page_accepts(fresh_db, demo_secrets):
    """Ревьюер без Telegram и iPhone: логин демо-аккаунта → код → тот же
    путь через страницу согласия, что у живого атлета."""
    import mcp_oauth

    app = mcp_server.build_app()
    status, _, body = await _asgi(app, "GET", mcp_oauth.DEMO_CODE_PATH)
    assert status == 200 and 'name="password"' in body.decode()

    status, _, body = await _asgi(
        app, "POST", mcp_oauth.DEMO_CODE_PATH, form={"username": "reviewer", "password": "nope"}
    )
    assert status == 401
    assert html.escape(i18n.t_in("en", "oauth.demo.error")) in body.decode()

    status, _, body = await _asgi(
        app, "POST", mcp_oauth.DEMO_CODE_PATH, form={"username": "reviewer", "password": "s3cret-pass"}
    )
    assert status == 200
    code = re.search(r'id="code" value="(\d{6})"', body.decode()).group(1)

    await fresh_db.save_oauth_client("c-review", None, json.dumps({"redirect_uris": ["https://claude.ai/cb"]}))
    await fresh_db.create_oauth_consent_request(
        request_id="req-review", client_id="c-review", redirect_uri="https://claude.ai/cb",
        redirect_uri_provided_explicitly=True, code_challenge="x" * 43, scopes="[]",
        resource=None, state=None, expires_at=9e12,
    )
    verdict, user_id = await fresh_db.verify_oauth_link_code(
        "req-review", code, mcp_oauth.LINK_CODE_MAX_ATTEMPTS, client_ip="1.2.3.4",
        window_seconds=600, window_limit_per_ip=10, window_limit_total=60,
    )
    assert verdict == "ok"
    # Это демо-аккаунт с историей — ревьюеру есть что спросить.
    workouts = await fresh_db.conn().execute(
        "SELECT COUNT(*) FROM workouts WHERE user_id = ?", (user_id,)
    )
    assert (await workouts.fetchone())[0] > 0


async def test_demo_code_page_is_off_by_default(monkeypatch, demo_secrets):
    import mcp_oauth

    monkeypatch.setattr(config, "MCP_DEMO_CODE_ENABLED", False)
    status, _, _ = await _asgi(mcp_server.build_app(), "GET", mcp_oauth.DEMO_CODE_PATH)
    assert status == 404
