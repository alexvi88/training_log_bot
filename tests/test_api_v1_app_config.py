"""GET /v1/app/config — минимальная версия iOS и ссылка на App Store, без входа."""

from __future__ import annotations

import logging

import httpx
import pytest

import api_v1
import config

pytestmark = pytest.mark.asyncio

STORE = "https://apps.apple.com/app/id123"


async def _get(fresh_db) -> httpx.Response:
    transport = httpx.ASGITransport(app=api_v1.build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/app/config")


async def test_defaults_are_null(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "IOS_MIN_VERSION", "")
    monkeypatch.setattr(config, "APP_STORE_URL", "")
    resp = await _get(fresh_db)
    assert resp.status_code == 200
    assert resp.json() == {"ios": {"min_version": None, "store_url": None}}
    assert resp.headers["cache-control"] == "no-store"


async def test_min_version_and_store_url(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "IOS_MIN_VERSION", " 1.0.7 ")
    monkeypatch.setattr(config, "APP_STORE_URL", STORE)
    resp = await _get(fresh_db)
    assert resp.status_code == 200
    assert resp.json() == {"ios": {"min_version": "1.0.7", "store_url": STORE}}


@pytest.mark.parametrize("bad", ["1.0.x", "v1.0.7", "1..0", "1.2.3.4.5", "latest"])
async def test_invalid_min_version_is_null_and_logged(fresh_db, monkeypatch, caplog, bad):
    monkeypatch.setattr(config, "IOS_MIN_VERSION", bad)
    monkeypatch.setattr(config, "APP_STORE_URL", STORE)
    with caplog.at_level(logging.WARNING, logger="config"):
        resp = await _get(fresh_db)
    assert resp.status_code == 200
    assert resp.json() == {"ios": {"min_version": None, "store_url": STORE}}
    assert any("IOS_MIN_VERSION" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("good", ["2", "1.0", "1.0.5", "10.20.30.40"])
async def test_valid_versions(fresh_db, monkeypatch, good):
    monkeypatch.setattr(config, "IOS_MIN_VERSION", good)
    resp = await _get(fresh_db)
    assert resp.json()["ios"]["min_version"] == good
