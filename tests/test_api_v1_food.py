"""REST `/v1/food*` — дневника еды больше нет нигде (приложение, бот, тренер,
MCP). Каждая ручка отвечает 410 `food_gone` с человеческим текстом на языке
атлета, а записи, сделанные раньше, остаются в базе — их не трогает ни одна
ручка, и новых не появляется.

Гоняется тем же приёмом, что и tests/test_api_v1.py — httpx поверх ASGI, без
сокета, включая настоящую проверку Bearer-токена.
"""

import httpx
import legacy_food
import pytest

import api_v1
import i18n
import i18n_coverage


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _linked_client(fresh_db, client_factory, telegram_id=111, lang=None):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    if lang:
        await fresh_db.set_user_lang(telegram_id, lang)
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


CALLS = [
    ("GET", "/food", None),
    ("POST", "/food", {"name": "Овсянка", "kcal": 300}),
    ("GET", "/food/days", None),
    ("POST", "/food/parse", {"text": "овсянка"}),
    ("POST", "/food/parse", {"image_data_url": "data:image/jpeg;base64,eA=="}),
    ("POST", "/food/goal", {"goal": 2200}),
    ("GET", "/food/1", None),
    ("DELETE", "/food/1", None),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("lang", ["ru", "en"])
@pytest.mark.parametrize("method,path,body", CALLS)
async def test_every_food_route_is_gone_with_a_human_text(fresh_db, client_factory, lang, method, path, body):
    client = await _linked_client(fresh_db, client_factory, lang=lang)
    resp = await client.request(method, path, json=body)
    assert resp.status_code == 410, resp.text
    payload = resp.json()
    assert payload["error"] == "food_gone"
    assert payload["message"] == i18n.t_in(lang, "api.error.food_gone")
    assert i18n_coverage.has_cyrillic(payload["message"]) == (lang == "ru")
    # Ни одна ручка ничего не пишет.
    cur = await fresh_db.conn().execute("SELECT COUNT(*) FROM food_entries")
    assert (await cur.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_food_routes_still_need_a_token(client_factory):
    async with client_factory() as client:
        resp = await client.get("/food")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_old_entries_are_not_touched(fresh_db, client_factory):
    """410 — не удаление: записи, сделанные до выпила, остаются (выгрузка и
    «Удалить аккаунт» работают с ними как раньше)."""
    client = await _linked_client(fresh_db, client_factory)
    entry_id = await legacy_food.insert_food_row(111, "2026-07-20", "Овсянка", calories=300)
    resp = await client.delete(f"/food/{entry_id}")
    assert resp.status_code == 410
    cur = await fresh_db.conn().execute("SELECT description FROM food_entries WHERE id = ?", (entry_id,))
    assert (await cur.fetchone())["description"] == "Овсянка"
