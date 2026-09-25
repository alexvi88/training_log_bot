"""POST /v1/diagnostics — отчёты о сбоях iOS из MetricKit (api_v1_diagnostics.py)
и админская сводка /crashes поверх них."""

import datetime as dt
import json

import httpx
import pytest

import admin_tasks
import api_v1
import api_v1_diagnostics
import config
import db
from handlers import admin

pytestmark = pytest.mark.asyncio

CRASH = {
    "version": "1.0.0",
    "callStackTree": {"callStacks": [], "callStackPerThread": True},
    "diagnosticMetaData": {
        "appVersion": "1.4",
        "appBuildVersion": "57",
        "osVersion": "iPhone OS 17.5.1 (21F90)",
        "deviceType": "iPhone15,2",
        "exceptionType": 1,
        "signal": 11,
        "terminationReason": "Namespace SIGNAL, Code 11",
    },
}


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    api_v1_diagnostics.reset_rate_limit()
    yield
    api_v1_diagnostics.reset_rate_limit()


@pytest.fixture
def client():
    transport = httpx.ASGITransport(app=api_v1.build_app())
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _rows():
    cur = await db.conn().execute("SELECT * FROM diagnostics ORDER BY id")
    return await cur.fetchall()


async def test_anonymous_report_is_stored_with_crashed_run_metadata(fresh_db, client):
    resp = await client.post("/diagnostics", json={
        "kind": "crash", "payload": CRASH,
        # Прислала уже обновлённая сборка — в строку идёт та, что упала.
        "app_version": "1.5", "build": "60", "os_version": "18.0", "device": "iPhone16,1",
    })
    assert resp.status_code == 201, resp.text
    (row,) = await _rows()
    assert row["user_id"] is None
    assert row["kind"] == "crash"
    assert (row["app_version"], row["build"]) == ("1.4", "57")
    assert row["device"] == "iPhone15,2"
    assert json.loads(row["payload"]) == CRASH


async def test_body_fields_are_the_fallback_without_metadata(fresh_db, client):
    resp = await client.post("/diagnostics", json={
        "kind": "hang", "payload": {"hangDuration": "3 sec"},
        "app_version": "1.5", "build": "60", "os_version": "18.0", "device": "iPhone16,1",
    })
    assert resp.status_code == 201
    (row,) = await _rows()
    assert (row["app_version"], row["build"], row["os_version"], row["device"]) == (
        "1.5", "60", "18.0", "iPhone16,1",
    )


async def test_valid_token_attaches_user_and_bad_token_stays_anonymous(fresh_db, client):
    await fresh_db.get_or_create_user(telegram_id=111, username="tester")
    token = await fresh_db.issue_api_token(111)
    ok = await client.post(
        "/diagnostics", json={"kind": "crash", "payload": CRASH},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert ok.status_code == 201
    # Отозванный/чужой токен — не повод терять отчёт о падении.
    bad = await client.post(
        "/diagnostics", json={"kind": "crash", "payload": CRASH},
        headers={"Authorization": "Bearer nope"},
    )
    assert bad.status_code == 201
    rows = await _rows()
    assert [r["user_id"] for r in rows] == [111, None]
    # Отчёт о сбое — не действие атлета: в ленту /activity он не пишется.
    assert "user_events" not in await fresh_db.user_data_left(111)


async def test_body_over_the_limit_is_413(fresh_db, client):
    big = {"kind": "crash", "payload": {"blob": "x" * api_v1_diagnostics.MAX_BODY_BYTES}}
    resp = await client.post("/diagnostics", json=big)
    assert resp.status_code == 413
    assert resp.json()["error"] == "payload_too_large"
    assert await _rows() == []


@pytest.mark.parametrize("raw", [
    b"not json at all",
    b"[1, 2, 3]",
    json.dumps({"kind": "crash"}).encode(),
    json.dumps({"kind": "crash", "payload": "string"}).encode(),
    json.dumps({"kind": "crash", "payload": {}}).encode(),
    json.dumps({"kind": "segfault", "payload": CRASH}).encode(),
    "{\"kind\": \"crash\", \"payload\": {\"a\": \"\xff\"}}".encode("latin-1"),
])
async def test_garbage_is_400(fresh_db, client, raw):
    resp = await client.post(
        "/diagnostics", content=raw, headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400, resp.text
    assert await _rows() == []


async def test_rate_limit_per_ip(fresh_db, client, monkeypatch):
    monkeypatch.setattr(api_v1_diagnostics, "RATE_LIMIT_PER_IP", 2)
    body = {"kind": "crash", "payload": CRASH}
    assert (await client.post("/diagnostics", json=body)).status_code == 201
    assert (await client.post("/diagnostics", json=body)).status_code == 201
    third = await client.post("/diagnostics", json=body)
    assert third.status_code == 429
    assert third.json()["error"] == "rate_limited"
    other_ip = await client.post("/diagnostics", json=body, headers={"fly-client-ip": "10.0.0.2"})
    assert other_ip.status_code == 201


async def test_retention_cleanup_drops_old_reports(fresh_db, client):
    await client.post("/diagnostics", json={"kind": "crash", "payload": CRASH})
    old = (dt.datetime.now() - dt.timedelta(days=config.DIAGNOSTICS_RETENTION_DAYS + 2)).isoformat()
    await db.conn().execute(
        "INSERT INTO diagnostics (created_at, kind, payload) VALUES (?, 'hang', '{}')", (old,),
    )
    await db.conn().commit()
    await admin_tasks._run_retention_cleanup()
    assert [r["kind"] for r in await _rows()] == ["crash"]


async def test_account_wipe_takes_diagnostics_along(fresh_db, client):
    await fresh_db.get_or_create_user(telegram_id=111, username="tester")
    token = await fresh_db.issue_api_token(111)
    await client.post(
        "/diagnostics", json={"kind": "crash", "payload": CRASH},
        headers={"Authorization": f"Bearer {token}"},
    )
    await client.post("/diagnostics", json={"kind": "hang", "payload": {"hangDuration": "2 sec"}})
    await fresh_db.wipe_user_account(111)
    assert [r["user_id"] for r in await _rows()] == [None]


async def test_admin_summary_groups_by_version(fresh_db, client):
    await client.post("/diagnostics", json={"kind": "crash", "payload": CRASH})
    await client.post("/diagnostics", json={"kind": "crash", "payload": CRASH})
    await client.post("/diagnostics", json={
        "kind": "hang", "payload": {"hangDuration": "3 sec"}, "app_version": "1.5", "build": "60",
    })
    summary = await db.diagnostics_summary(7)
    assert [(r["app_version"], r["build"], r["kind"], r["n"]) for r in summary] == [
        ("1.5", "60", "hang", 1),
        ("1.4", "57", "crash", 2),
    ]
    recent = await db.recent_crashes(7, 5)
    assert len(recent) == 2 and recent[0]["signal"] == 11

    text = admin.format_crash_report(summary, recent, 7)
    assert "1.4 (57)" in text and "падения: 2" in text and "зависания: 1" in text
    assert "sig 11" in text
    assert "не пришло" in admin.format_crash_report([], [], 7)
