"""Демо-аккаунт ревьюеров каталогов AI-коннекторов (catalog_demo.py,
config.CATALOG_DEMO_*): отдельный от демо App Review, заполнен так, чтобы
каждому инструменту коннектора было что ответить, заполнение повторяемое, и
демо App Review оно не трогает."""

import datetime as dt
import json

import httpx
import pytest

import ai_limits
import ai_trainer
import api_v1
import catalog_demo
import config
import db
import review_demo

CATALOG_USER = "catalog-review"
CATALOG_PASS = "catalog pass phrase"
REVIEW_USER = "appreview"
REVIEW_PASS = "apple pass phrase"


@pytest.fixture(autouse=True)
def demo_config(monkeypatch):
    monkeypatch.setattr(config, "CATALOG_DEMO_USERNAME", CATALOG_USER)
    monkeypatch.setattr(config, "CATALOG_DEMO_PASSWORD", CATALOG_PASS)
    monkeypatch.setattr(config, "CATALOG_DEMO_LANG", "en")
    monkeypatch.setattr(config, "REVIEW_DEMO_USERNAME", REVIEW_USER)
    monkeypatch.setattr(config, "REVIEW_DEMO_PASSWORD", REVIEW_PASS)
    monkeypatch.setattr(config, "WALK_DEMO_USERNAME", "")
    monkeypatch.setattr(config, "WALK_DEMO_PASSWORD", "")
    review_demo.reset_failures()
    yield
    review_demo.reset_failures()


async def _snapshot(user_id: int) -> dict[str, list[tuple]]:
    """Все строки аккаунта во всех таблицах с хозяином, плюс его подходы."""
    out: dict[str, list[tuple]] = {}
    for table, column in await db._user_scoped_tables():
        cur = await db.conn().execute(f"SELECT * FROM {table} WHERE {column} = ? ORDER BY rowid", (user_id,))
        out[f"{table}.{column}"] = [tuple(r) for r in await cur.fetchall()]
    cur = await db.conn().execute(
        "SELECT s.* FROM sets s JOIN workout_blocks b ON b.id = s.block_id "
        "JOIN workouts w ON w.id = b.workout_id WHERE w.user_id = ? ORDER BY s.id",
        (user_id,),
    )
    out["sets"] = [tuple(r) for r in await cur.fetchall()]
    return out


# ---------- конфиг ----------


def test_available_only_with_a_full_distinct_pair(monkeypatch):
    assert config.catalog_demo_available()
    monkeypatch.setattr(config, "CATALOG_DEMO_PASSWORD", "")
    assert not config.catalog_demo_available()
    monkeypatch.setattr(config, "CATALOG_DEMO_PASSWORD", CATALOG_PASS)
    for other in ("REVIEW_DEMO_USERNAME", "WALK_DEMO_USERNAME"):
        monkeypatch.setattr(config, "WALK_DEMO_PASSWORD", "walk-pass")
        monkeypatch.setattr(config, other, CATALOG_USER)
        assert not config.catalog_demo_available(), other
        monkeypatch.setattr(config, other, "someone-else")
    # и с логином новичка (WALK + "-new") тоже не совпадает
    monkeypatch.setattr(config, "WALK_DEMO_USERNAME", "walk")
    monkeypatch.setattr(config, "CATALOG_DEMO_USERNAME", "walk-new")
    assert not config.catalog_demo_available()
    # и с логином дорожки прогона витрины (WALK + "-l2") тоже
    monkeypatch.setattr(config, "CATALOG_DEMO_USERNAME", "walk-l2")
    assert not config.catalog_demo_available()


def test_credentials_are_only_the_catalog_pair():
    assert catalog_demo.check_credentials(CATALOG_USER, CATALOG_PASS)
    assert catalog_demo.check_credentials(f" {CATALOG_USER}\n", f"{CATALOG_PASS} ")
    assert not catalog_demo.check_credentials(REVIEW_USER, REVIEW_PASS)
    assert not catalog_demo.check_credentials(CATALOG_USER, REVIEW_PASS)
    assert not catalog_demo.check_credentials("", "")


@pytest.mark.asyncio
async def test_app_password_login_does_not_accept_catalog_pair(fresh_db):
    """Вход по паролю в приложении — только пары App Review и прогонов."""
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    resp = await client.post("/auth/password", json={"username": CATALOG_USER, "password": CATALOG_PASS})
    assert resp.status_code == 401
    assert await fresh_db.resolve_auth_identity(review_demo.PROVIDER, CATALOG_USER) is None


# ---------- заполнение ----------


@pytest.mark.asyncio
async def test_seeding_fills_every_tool_and_is_idempotent(fresh_db):
    user_id = await catalog_demo.ensure_user()
    assert user_id < 0
    user = await fresh_db.get_user(user_id)
    assert user["lang"] == "en"
    assert await fresh_db.is_catalog_demo(user_id)

    assert await fresh_db.count_workouts(user_id) == review_demo.TOTAL_WORKOUTS
    weights = await fresh_db.list_bodyweight_logs(user_id)
    assert len(weights) >= 4
    programs = await fresh_db.list_programs(user_id)
    assert len(programs) == 1 and programs[0]["day_count"] == 2
    assert programs[0]["program_name"] == "Strength A/B"
    assert user["goal"] and user["experience"] and json.loads(user["equipment"])

    adherence = await ai_trainer._program_adherence(user_id)
    days = adherence["programs"][0]["days"]
    assert adherence["programs"][0]["total_sessions"] == review_demo.TOTAL_WORKOUTS
    assert [d["sessions"] for d in days] == [6, 6]
    assert all(d["days_since_last"] is not None for d in days)

    before = await _snapshot(user_id)
    assert await catalog_demo.ensure_user() == user_id
    assert await catalog_demo.ensure_user() == user_id
    assert await _snapshot(user_id) == before


@pytest.mark.asyncio
async def test_lang_follows_config(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "CATALOG_DEMO_LANG", "ru")
    user_id = await catalog_demo.ensure_user()
    assert (await fresh_db.get_user(user_id))["lang"] == "ru"
    assert (await fresh_db.list_programs(user_id))[0]["program_name"] == "Сила A/B"
    monkeypatch.setattr(config, "CATALOG_DEMO_LANG", "en")
    assert await catalog_demo.ensure_user() == user_id
    assert (await fresh_db.get_user(user_id))["lang"] == "en"


@pytest.mark.asyncio
async def test_reviewer_edits_are_not_overwritten(fresh_db):
    user_id = await catalog_demo.ensure_user()
    await fresh_db.update_user(user_id, goal="Run a marathon")
    program = (await fresh_db.list_programs(user_id))[0]
    await fresh_db.rename_program_by_id(program["id"], "Mine")
    await catalog_demo.ensure_user()
    assert (await fresh_db.get_user(user_id))["goal"] == "Run a marathon"
    assert [p["program_name"] for p in await fresh_db.list_programs(user_id)] == ["Mine"]


@pytest.mark.asyncio
async def test_deleted_account_is_recreated_and_reseeded(fresh_db):
    user_id = await catalog_demo.ensure_user()
    await fresh_db.wipe_user_account(user_id)
    assert await fresh_db.get_user(user_id) is None
    again = await catalog_demo.ensure_user()
    assert await fresh_db.resolve_auth_identity(review_demo.PROVIDER, CATALOG_USER) == again
    assert await fresh_db.count_workouts(again) == review_demo.TOTAL_WORKOUTS
    assert len(await fresh_db.list_programs(again)) == 1


@pytest.mark.asyncio
async def test_app_review_account_is_untouched(fresh_db):
    """Демо App Review (в проде — -1000000000, первый синтетический id) не
    получает ни одной строки и не теряет ни одной, а аккаунт каталогов — свой."""
    review_id = await review_demo.ensure_demo_user("en", REVIEW_USER)
    assert review_id == db.SYNTHETIC_TELEGRAM_ID_START == -1_000_000_000
    before = await _snapshot(review_id)

    catalog_id = await catalog_demo.ensure_user()
    await catalog_demo.ensure_user()
    assert catalog_id != review_id
    assert await _snapshot(review_id) == before
    # у App Review по-прежнему ни программы, ни профиля — его заполнение прежнее
    assert await fresh_db.list_programs(review_id) == []
    assert (await fresh_db.get_user(review_id))["goal"] is None
    assert not await fresh_db.is_catalog_demo(review_id)
    assert await fresh_db.resolve_auth_identity(review_demo.PROVIDER, REVIEW_USER) == review_id


@pytest.mark.asyncio
async def test_seeding_without_app_review_account_writes_nothing_for_it(fresh_db):
    """Нет аккаунта App Review в базе — заполнение каталогов его не заводит и
    не пишет ни строки под его логином."""
    await fresh_db.create_app_only_user(language_code="en", track_signup=False)  # занял -1000000000
    catalog_id = await catalog_demo.ensure_user()
    assert catalog_id != -1_000_000_000
    assert await fresh_db.resolve_auth_identity(review_demo.PROVIDER, REVIEW_USER) is None
    snapshot = await _snapshot(-1_000_000_000)
    assert all(not rows for key, rows in snapshot.items() if not key.startswith("users."))


# ---------- отсечки ----------


@pytest.mark.asyncio
async def test_quota_exempt_but_money_caps_still_apply(fresh_db, monkeypatch):
    catalog_id = await catalog_demo.ensure_user()
    review_id = await review_demo.ensure_demo_user("en", REVIEW_USER)
    assert await ai_limits.quota_exempt(catalog_id)
    assert not await ai_limits.quota_exempt(review_id)

    async def hard():
        return ai_limits.KIND_SPEND_HARD

    async def spend():
        return 999.0

    monkeypatch.setattr(ai_limits, "spend_level", hard)
    monkeypatch.setattr(ai_limits, "daily_spend_usd", spend)
    block = await ai_limits.check(catalog_id, ai_limits.KIND_QUESTION)
    assert block is not None and block.kind == ai_limits.KIND_SPEND_HARD


@pytest.mark.asyncio
async def test_excluded_from_site_numbers_feedback_push_and_leaderboards(fresh_db):
    catalog_id = await catalog_demo.ensure_user()
    since = (dt.datetime.utcnow() - dt.timedelta(days=60)).isoformat(timespec="seconds")
    numbers = await fresh_db.site_week_numbers(since, set())
    assert numbers.get("workouts", 0) == 0

    now = fresh_db.now_iso()
    await fresh_db.conn().execute(
        "INSERT INTO push_tokens (user_id, platform, device_token, created_at, updated_at) VALUES (?, 'ios', 'tok', ?, ?)",
        (catalog_id, now, now),
    )
    await fresh_db.conn().execute(
        "INSERT INTO minigame_results (user_id, game, score, duration_ms, created_at) VALUES (?, 'catch', 50, 60000, ?)",
        (catalog_id, now),
    )
    await fresh_db.conn().commit()
    candidates = await fresh_db.list_feedback_push_candidates()
    assert catalog_id not in {r["telegram_id"] for r in candidates}
    rows = await fresh_db.game_leaderboard_rows("catch", None, set(), review_demo.PROVIDER)
    assert catalog_id not in {r["user_id"] for r in rows}


@pytest.mark.asyncio
async def test_every_read_tool_has_something_to_say(fresh_db):
    """Ревьюер каталога зовёт каждый инструмент — пустых ответов быть не должно."""
    import i18n
    import mcp_server

    user_id = await catalog_demo.ensure_user()
    args = {"get_exercise_progress": {"exercise_name": "Barbell Bench Press"}, "compare_periods": {"days": 28}}
    with i18n.use_lang("en"):
        for tool in sorted(mcp_server.EXPOSED_TOOLS - mcp_server.WRITE_TOOLS):
            raw = await ai_trainer.execute_tool(user_id, tool, args.get(tool, {}))
            assert raw and "error" not in json.loads(raw), (tool, raw[:300])
        saved = await ai_trainer.execute_tool(user_id, "get_saved_programs", {})
        assert "Strength A/B" in saved
        progress = json.loads(await ai_trainer.execute_tool(user_id, "get_exercise_progress", args["get_exercise_progress"]))
        assert progress
