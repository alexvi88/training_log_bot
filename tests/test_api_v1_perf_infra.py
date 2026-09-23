"""Инфраструктура скорости `/v1`: кэш токенов, редкая запись last_used_at,
Server-Timing, gzip, PRAGMA соединения.

Главное здесь — не скорость, а то, что ускорение не сломало отзыв: токен,
погашенный перевыпуском, удалением аккаунта или слиянием, обязан перестать
работать СРАЗУ, а не через TTL кэша.
"""

import datetime as dt
import logging

import httpx
import pytest

import account_deletion
import api_v1
import db
import server_timing


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _linked_client(fresh_db, client_factory, telegram_id=111):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    token = resp.json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client, token


async def _last_used(token):
    cur = await db.conn().execute("SELECT last_used_at FROM api_tokens WHERE token = ?", (token,))
    row = await cur.fetchone()
    return row["last_used_at"]


# ---------- отзыв гасит кэш сразу ----------


async def test_revoked_token_is_rejected_immediately(fresh_db, client_factory):
    client, token = await _linked_client(fresh_db, client_factory)
    assert (await client.get("/me")).status_code == 200  # токен теперь в кэше
    assert token in db._api_token_cache

    assert await db.revoke_api_token(111) is True

    resp = await client.get("/me")
    assert resp.status_code == 401


async def test_reissued_token_kills_the_old_one_immediately(fresh_db, client_factory):
    client, old = await _linked_client(fresh_db, client_factory)
    assert (await client.get("/me")).status_code == 200

    new = await db.issue_api_token(111)

    assert (await client.get("/me")).status_code == 401
    client.headers["Authorization"] = f"Bearer {new}"
    assert (await client.get("/me")).status_code == 200


async def test_deleted_account_token_is_rejected_immediately(fresh_db, client_factory):
    client, _ = await _linked_client(fresh_db, client_factory)
    assert (await client.get("/me")).status_code == 200

    await account_deletion.delete_account(111)

    assert (await client.get("/me")).status_code == 401


async def test_delete_account_endpoint_then_same_token_is_401(fresh_db, client_factory):
    client, _ = await _linked_client(fresh_db, client_factory)
    assert (await client.get("/me")).status_code == 200
    resp = await client.delete("/account?confirm=delete")
    assert resp.status_code < 400, resp.text
    assert (await client.get("/me")).status_code == 401


async def test_merge_moves_cached_token_to_new_owner(fresh_db):
    """Слияние app-only аккаунта с telegram_id переписывает владельца токена —
    закэшированный «токен → старый id» обязан исчезнуть."""
    app_id = (await db.create_app_only_user())["telegram_id"]
    token = await db.issue_api_token(app_id)
    assert await db.resolve_api_token(token) == app_id

    assert await db.link_telegram_to_app_account(app_id, 777) == "ok"

    assert await db.resolve_api_token(token) == 777


async def test_revoke_during_in_flight_lookup_is_not_cached(fresh_db, monkeypatch):
    """Промах начался до отзыва, закончился после — результат в кэш не кладётся,
    иначе отозванный токен прожил бы ещё целый TTL."""
    await db.get_or_create_user(telegram_id=111, username="tester")
    token = await db.issue_api_token(111)
    real_conn = db.conn

    class _Conn:
        def __init__(self, inner):
            self._inner = inner

        async def execute(self, sql, params=()):
            cur = await self._inner.execute(sql, params)
            if sql.startswith("SELECT user_id, last_used_at FROM api_tokens"):
                db._forget_api_tokens()  # отзыв «посреди» чтения
            return cur

        def __getattr__(self, name):
            return getattr(self._inner, name)

    wrapped = _Conn(real_conn())
    monkeypatch.setattr(db, "conn", lambda: wrapped)
    assert await db.resolve_api_token(token) == 111
    monkeypatch.setattr(db, "conn", real_conn)
    assert token not in db._api_token_cache


async def test_unknown_token_is_not_cached(fresh_db):
    assert await db.resolve_api_token("nope") is None
    assert "nope" not in db._api_token_cache


async def test_expired_cache_entry_rechecks_the_db(fresh_db):
    await db.get_or_create_user(telegram_id=111, username="tester")
    token = await db.issue_api_token(111)
    assert await db.resolve_api_token(token) == 111
    # Строку снесли в обход точек отзыва — кэш страхует только TTL.
    await db.conn().execute("DELETE FROM api_tokens WHERE token = ?", (token,))
    await db.conn().commit()
    db._api_token_cache[token] = (111, 0.0)  # TTL истёк
    assert await db.resolve_api_token(token) is None


# ---------- last_used_at пишется редко ----------


async def test_last_used_at_is_written_once_then_throttled(fresh_db, monkeypatch):
    await db.get_or_create_user(telegram_id=111, username="tester")
    token = await db.issue_api_token(111)
    assert await _last_used(token) is None

    assert await db.resolve_api_token(token) == 111
    first = await _last_used(token)
    assert first is not None

    commits = 0
    real_commit = db.conn().commit

    async def counting_commit():
        nonlocal commits
        commits += 1
        await real_commit()

    monkeypatch.setattr(db.conn(), "commit", counting_commit)
    # Промах кэша (как после TTL), но отметка свежая — ни UPDATE, ни commit.
    db._api_token_cache.clear()
    assert await db.resolve_api_token(token) == 111
    assert commits == 0
    assert await _last_used(token) == first


async def test_stale_last_used_at_is_refreshed(fresh_db):
    await db.get_or_create_user(telegram_id=111, username="tester")
    token = await db.issue_api_token(111)
    stale = (dt.datetime.now() - dt.timedelta(minutes=11)).isoformat(timespec="seconds")
    await db.conn().execute("UPDATE api_tokens SET last_used_at = ? WHERE token = ?", (stale, token))
    await db.conn().commit()

    assert await db.resolve_api_token(token) == 111
    assert await _last_used(token) > stale


async def test_cache_hit_skips_the_db_entirely(fresh_db, monkeypatch):
    await db.get_or_create_user(telegram_id=111, username="tester")
    token = await db.issue_api_token(111)
    assert await db.resolve_api_token(token) == 111

    def _no_db():
        raise AssertionError("cache hit must not touch the DB")

    monkeypatch.setattr(db, "conn", _no_db)
    assert await db.resolve_api_token(token) == 111


# ---------- Server-Timing и медленные запросы ----------


async def test_server_timing_header_present(fresh_db, client_factory):
    client, _ = await _linked_client(fresh_db, client_factory)
    resp = await client.get("/me")
    assert resp.status_code == 200
    header = resp.headers["server-timing"]
    assert header.startswith("app;dur=")
    assert float(header.split("=", 1)[1]) >= 0


async def test_server_timing_on_errors_too(fresh_db, client_factory):
    resp = await client_factory().get("/me")
    assert resp.status_code == 401
    assert "server-timing" in resp.headers


async def test_slow_request_logged_with_template_and_no_token(fresh_db, client_factory, caplog, monkeypatch):
    monkeypatch.setattr(server_timing, "SLOW_REQUEST_MS", 0.0)
    client, token = await _linked_client(fresh_db, client_factory)
    with caplog.at_level(logging.WARNING, logger="server_timing"):
        resp = await client.get("/exercises/999999/progress?secret=1")
    assert resp.status_code == 404
    lines = [r.getMessage() for r in caplog.records if r.name == "server_timing"]
    assert lines, "slow request must be logged"
    line = lines[-1]
    assert "GET /exercises/{exercise_id}/progress | 404" in line
    assert token not in line
    assert "secret" not in line


# ---------- gzip ----------


async def test_large_json_is_gzipped(fresh_db, client_factory):
    client, _ = await _linked_client(fresh_db, client_factory)
    resp = await client.get("/exercise-templates?query=жим&limit=200", headers={"Accept-Encoding": "gzip"})
    assert resp.status_code == 200
    assert resp.headers.get("content-encoding") == "gzip"
    assert len(resp.content) > 1024  # httpx уже распаковал
    assert resp.json()


async def test_small_json_is_not_gzipped(fresh_db, client_factory):
    resp = await client_factory().get("/health", headers={"Accept-Encoding": "gzip"})
    assert resp.status_code == 200
    assert "content-encoding" not in resp.headers


# ---------- PRAGMA ----------


async def test_file_db_uses_wal_with_normal_sync(tmp_path):
    await db.close_db()
    await db.init_db(str(tmp_path / "t.db"))
    try:
        cur = await db.conn().execute("PRAGMA journal_mode")
        assert (await cur.fetchone())[0].lower() == "wal"
        cur = await db.conn().execute("PRAGMA synchronous")
        assert (await cur.fetchone())[0] == 1  # NORMAL
        cur = await db.conn().execute("PRAGMA temp_store")
        assert (await cur.fetchone())[0] == 2  # MEMORY
        cur = await db.conn().execute("PRAGMA cache_size")
        assert (await cur.fetchone())[0] == -20000
    finally:
        await db.close_db()
