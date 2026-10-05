"""`nan`/`inf` в ячейке CSV — не число; полный провал импорта не оставляет пачку."""
import httpx
import pytest

import ai_trainer
import api_v1
from handlers import csv_import

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_ai(monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: False)


async def _client(fresh_db, telegram_id=111):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test"
    )
    resp = await client.post("/auth/link", json={"code": code})
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


@pytest.mark.parametrize("bad", ["nan", "NaN", "inf", "-inf", "Infinity"])
async def test_parse_number_rejects_non_finite(bad):
    with pytest.raises(ValueError):
        csv_import._parse_number(bad)


@pytest.mark.parametrize("cell", ["nan", "inf"])
@pytest.mark.parametrize("column", ["weight", "reps"])
async def test_preview_answers_400_not_500(fresh_db, cell, column):
    client = await _client(fresh_db)
    weight, reps = (cell, "5") if column == "weight" else ("100", cell)
    csv_text = f"date,exercise,weight,reps\n2024-01-01,Присед,{weight},{reps}\n"
    resp = await client.post("/import/csv/preview", json={"csv": csv_text})
    assert resp.status_code == 400, resp.text


async def test_total_failure_leaves_no_batch(fresh_db, user_id, monkeypatch):
    async def failing_apply(*a, **k):
        return 0, 1

    monkeypatch.setattr(csv_import, "apply_import", failing_apply)
    workouts = [{
        "date": "2024-01-01", "start": None, "title": None,
        "entries": [{"name": "Новое", "sets": [(100.0, 5, None, None)]}],
    }]
    decisions = {"Новое": {"kind": "new"}}
    result = await csv_import.run_import(user_id, workouts, decisions, "csv")
    assert result["batch_id"] is None
    cur = await fresh_db.conn().execute("SELECT COUNT(*) AS n FROM import_batches")
    assert (await cur.fetchone())["n"] == 0
