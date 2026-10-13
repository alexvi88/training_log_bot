"""Мини-игры между подходами — games.py и REST `/v1/games*` (api_v1_games.py)."""

import datetime as dt

import httpx
import pytest

import account_deletion
import admin_tasks
import api_v1
import api_v1_games
import config
import db
import games
import i18n
import timeutil


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    api_v1_games.reset_rate_limit()
    yield
    api_v1_games.reset_rate_limit()


@pytest.fixture(autouse=True)
def _no_own_accounts(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", None)
    monkeypatch.setattr(config, "TEST_USER_ID", None)


async def _client(fresh_db, telegram_id=111, username="tester", lang="ru") -> httpx.AsyncClient:
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username=username)
    await fresh_db.set_user_lang(telegram_id, lang)
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def _post(client, game="catch", score=10, duration_ms=60_000):
    return await client.post("/games/results", json={"game": game, "score": score, "duration_ms": duration_ms})


async def _insert(user_id: int, game: str, score: int, created_at: str, duration_ms: int = 60_000) -> None:
    await db.conn().execute(
        "INSERT INTO minigame_results (user_id, game, score, duration_ms, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, game, score, duration_ms, created_at),
    )
    await db.conn().commit()


async def _count_rows(user_id: int) -> int:
    cur = await db.conn().execute("SELECT COUNT(*) FROM minigame_results WHERE user_id = ?", (user_id,))
    return (await cur.fetchone())[0]


# --- правдоподобие ---------------------------------------------------------


@pytest.mark.parametrize(
    ("game", "duration_ms", "cap"),
    [
        ("catch", 60_000, 60_000 // 150 + 2),
        ("weigh", 60_000, 60_000 // 2500 + 1),
        ("sort", 60_000, 60_000 // 3000 + 1),
        ("runner", 60_000, 60_000 // 80 + 5),
    ],
)
def test_caps_accept_up_to_the_limit_and_reject_above(game, duration_ms, cap):
    assert games.is_plausible(game, cap, duration_ms)
    assert not games.is_plausible(game, cap + 1, duration_ms)


def test_caps_with_fractional_bound():
    # 1000 / 150 + 2 = 8.67 — 8 проходит, 9 нет.
    assert games.is_plausible("catch", 8, 1000)
    assert not games.is_plausible("catch", 9, 1000)


def test_negative_score_and_short_game_rejected():
    assert not games.is_plausible("catch", -1, 60_000)
    assert not games.is_plausible("catch", 0, 999)
    assert games.is_plausible("catch", 0, 1000)


def test_every_game_has_a_cap():
    assert set(games.SCORE_CAPS) == set(games.GAMES)


# --- POST /games/results ----------------------------------------------------


async def test_post_result_best_and_new_best(fresh_db):
    client = await _client(fresh_db)
    first = await _post(client, score=10)
    assert first.status_code == 201
    assert first.json() == {"best": 10, "is_new_best": True, "week_rank": 1}

    lower = (await _post(client, score=5)).json()
    assert lower == {"best": 10, "is_new_best": False, "week_rank": 1}

    equal = (await _post(client, score=10)).json()
    assert equal["is_new_best"] is False

    higher = (await _post(client, score=12)).json()
    assert higher == {"best": 12, "is_new_best": True, "week_rank": 1}
    assert await _count_rows(111) == 4


async def test_first_zero_is_not_a_record(fresh_db):
    client = await _client(fresh_db)
    body = (await _post(client, score=0)).json()
    assert body == {"best": 0, "is_new_best": False, "week_rank": 1}


async def test_implausible_result_is_202_and_not_stored(fresh_db):
    client = await _client(fresh_db)
    await _post(client, score=7)
    for payload in (
        {"score": -1, "duration_ms": 60_000},
        {"score": 1, "duration_ms": 999},
        {"score": 60_000 // 150 + 3, "duration_ms": 60_000},
    ):
        resp = await _post(client, **payload)
        assert resp.status_code == 202
        assert resp.json() == {"best": 7, "is_new_best": False, "week_rank": None, "accepted": False}
    assert await _count_rows(111) == 1


async def test_implausible_without_history_reports_zero_best(fresh_db):
    client = await _client(fresh_db)
    resp = await _post(client, game="sort", score=50, duration_ms=10_000)
    assert resp.status_code == 202
    assert resp.json()["best"] == 0


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_unknown_game_is_400_in_athletes_language(fresh_db, lang):
    client = await _client(fresh_db, lang=lang)
    resp = await _post(client, game="chess")
    assert resp.status_code == 400
    assert resp.json()["error"] == "unknown_game"
    assert resp.json()["message"] == i18n.t_in(lang, "api.error.unknown_game")
    assert (await client.get("/games/chess/leaderboard")).status_code == 400


@pytest.mark.parametrize(
    "payload",
    [
        {"game": "catch", "score": "10", "duration_ms": 60_000},
        {"game": "catch", "score": 10},
        {"game": "catch", "score": True, "duration_ms": 60_000},
        {"game": "catch", "score": 1.5, "duration_ms": 60_000},
        {"game": "catch", "score": 1, "duration_ms": 2**60},
    ],
)
async def test_malformed_body_is_400(fresh_db, payload):
    client = await _client(fresh_db)
    resp = await client.post("/games/results", json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_request"


async def test_requires_auth(fresh_db):
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    assert (await client.get("/games")).status_code == 401
    assert (await client.post("/games/results", json={})).status_code == 401


async def test_rate_limit(fresh_db, monkeypatch):
    monkeypatch.setattr(api_v1_games, "RATE_LIMIT_PER_USER", 3)
    client = await _client(fresh_db)
    for _ in range(3):
        assert (await _post(client)).status_code == 201
    resp = await _post(client)
    assert resp.status_code == 429
    assert resp.json()["error"] == "rate_limited"
    # У другого атлета своё окно.
    other = await _client(fresh_db, telegram_id=222, username="other")
    assert (await _post(other)).status_code == 201


async def test_default_rate_limit_is_modest():
    assert 60 <= api_v1_games.RATE_LIMIT_PER_USER <= 200
    assert api_v1_games.RATE_WINDOW_SECONDS == 3600


# --- GET /games ---------------------------------------------------------------


async def test_games_empty_state(fresh_db):
    client = await _client(fresh_db)
    body = (await client.get("/games")).json()
    assert [g["game"] for g in body["games"]] == ["catch", "weigh", "sort", "runner"]
    assert all(g == {"game": g["game"], "best": None, "best_at": None, "plays": 0} for g in body["games"])
    assert [b["code"] for b in body["badges"]] == ["catch_100", "catch_50_run", "weigh_10", "sort_5", "runner_1km"]
    assert all(b["earned_at"] is None for b in body["badges"])
    assert body["show_name"] is False


async def test_games_best_at_is_first_time_best_reached(fresh_db):
    client = await _client(fresh_db)
    await _insert(111, "weigh", 4, "2026-10-01T10:00:00")
    await _insert(111, "weigh", 6, "2026-10-03T10:00:00")
    await _insert(111, "weigh", 6, "2026-10-05T10:00:00")
    await _insert(111, "weigh", 2, "2026-10-06T10:00:00")
    body = (await client.get("/games")).json()
    weigh = next(g for g in body["games"] if g["game"] == "weigh")
    assert weigh == {"game": "weigh", "best": 6, "best_at": "2026-10-03", "plays": 4}


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_badges_earned_at_and_texts(fresh_db, lang):
    client = await _client(fresh_db, lang=lang)
    # catch_100 — суммой: 40 + 30 + 35 = 105 на третьей партии.
    await _insert(111, "catch", 40, "2026-10-01T10:00:00")
    await _insert(111, "catch", 30, "2026-10-02T10:00:00")
    await _insert(111, "catch", 35, "2026-10-04T10:00:00")
    await _insert(111, "catch", 55, "2026-10-07T10:00:00")  # catch_50_run
    await _insert(111, "weigh", 9, "2026-10-02T10:00:00")  # ещё не weigh_10
    await _insert(111, "sort", 5, "2026-10-03T10:00:00")
    await _insert(111, "runner", 1000, "2026-10-08T10:00:00")
    badges = {b["code"]: b for b in (await client.get("/games")).json()["badges"]}
    assert badges["catch_100"]["earned_at"] == "2026-10-04"
    assert badges["catch_50_run"]["earned_at"] == "2026-10-07"
    assert badges["weigh_10"]["earned_at"] is None
    assert badges["sort_5"]["earned_at"] == "2026-10-03"
    assert badges["runner_1km"]["earned_at"] == "2026-10-08"
    # Взятый — констатацией, невзятый — целью.
    assert badges["sort_5"]["description"] == i18n.t_in(lang, "games.badge.sort_5.description")
    assert badges["weigh_10"]["description"] == i18n.t_in(lang, "games.badge.weigh_10.goal")
    assert badges["catch_100"]["title"] == i18n.t_in(lang, "games.badge.catch_100.title")
    if lang == "en":
        assert not any(ch.isalpha() and ord(ch) > 0x400 for b in badges.values() for ch in b["title"] + b["description"])


def test_cumulative_reached_at():
    assert games.cumulative_reached_at([(50, "a"), (49, "b"), (1, "c"), (5, "d")], 100) == "c"
    assert games.cumulative_reached_at([(50, "a")], 100) is None


# --- PATCH /games/settings ----------------------------------------------------


async def test_settings_roundtrip(fresh_db):
    client = await _client(fresh_db)
    resp = await client.patch("/games/settings", json={"show_name": True})
    assert resp.status_code == 200
    assert resp.json() == {"show_name": True}
    assert (await client.get("/games")).json()["show_name"] is True
    assert (await client.patch("/games/settings", json={"show_name": False})).json() == {"show_name": False}
    assert (await client.patch("/games/settings", json={"show_name": 1})).status_code == 400
    assert (await client.patch("/games/settings", json={})).status_code == 400


# --- таблица лидеров ----------------------------------------------------------


def _this_week(hour: int = 0) -> str:
    return (dt.datetime.fromisoformat(api_v1_games.week_start()) + dt.timedelta(minutes=1, hours=hour)).isoformat(
        timespec="seconds"
    )


def _last_week() -> str:
    return (dt.datetime.fromisoformat(api_v1_games.week_start()) - dt.timedelta(seconds=1)).isoformat(
        timespec="seconds"
    )


def test_week_starts_monday_utc():
    assert api_v1_games.week_start(dt.datetime(2026, 10, 15, 13, 5)) == "2026-10-12T00:00:00"
    assert api_v1_games.week_start(dt.datetime(2026, 10, 12, 0, 0)) == "2026-10-12T00:00:00"
    assert api_v1_games.week_start(dt.datetime(2026, 10, 18, 23, 59)) == "2026-10-12T00:00:00"


async def test_leaderboard_week_vs_all(fresh_db):
    client = await _client(fresh_db)
    for uid in (222, 333):
        await fresh_db.get_or_create_user(telegram_id=uid, username=f"u{uid}")
    await _insert(111, "catch", 20, _this_week())
    await _insert(222, "catch", 90, _last_week())  # только за всё время
    await _insert(222, "catch", 10, _this_week())
    await _insert(333, "catch", 15, _this_week())

    week = (await client.get("/games/catch/leaderboard")).json()
    assert week["period"] == "week"
    assert [(e["rank"], e["score"]) for e in week["entries"]] == [(1, 20), (2, 15), (3, 10)]
    assert [e["is_me"] for e in week["entries"]] == [True, False, False]
    assert week["me"] == {"rank": 1, "total": 3, "best": 20}

    all_time = (await client.get("/games/catch/leaderboard?period=all")).json()
    assert all_time["period"] == "all"
    assert [e["score"] for e in all_time["entries"]] == [90, 20, 15]
    assert all_time["me"] == {"rank": 2, "total": 3, "best": 20}

    assert (await client.get("/games/catch/leaderboard?period=year")).status_code == 400


async def test_leaderboard_best_per_user_and_ties_by_earlier_time(fresh_db):
    client = await _client(fresh_db)
    for uid in (222, 333):
        await fresh_db.get_or_create_user(telegram_id=uid, username=f"u{uid}")
    await _insert(333, "runner", 500, _this_week(1))
    await _insert(111, "runner", 300, _this_week(0))
    await _insert(111, "runner", 500, _this_week(2))  # тот же счёт, но позже 333
    await _insert(222, "runner", 500, _this_week(3))
    await _insert(222, "runner", 500, _this_week(0))  # у 222 первый раз — раньше всех
    board = (await client.get("/games/runner/leaderboard")).json()
    names = [e["name"] for e in board["entries"]]
    assert len(board["entries"]) == 3  # одна строка на атлета
    assert names == [
        games.leaderboard_name(222, "u222", False),
        games.leaderboard_name(333, "u333", False),
        games.leaderboard_name(111, "tester", False),
    ]
    assert board["me"] == {"rank": 3, "total": 3, "best": 500}


async def test_leaderboard_top_20_and_me_outside(fresh_db):
    client = await _client(fresh_db)
    for i in range(25):
        uid = 1000 + i
        await fresh_db.get_or_create_user(telegram_id=uid, username=f"u{uid}")
        await _insert(uid, "sort", 100 + i, _this_week())
    await _insert(111, "sort", 1, _this_week())
    board = (await client.get("/games/sort/leaderboard")).json()
    assert len(board["entries"]) == 20
    assert [e["rank"] for e in board["entries"]] == list(range(1, 21))
    assert not any(e["is_me"] for e in board["entries"])
    assert board["me"] == {"rank": 26, "total": 26, "best": 1}


async def test_leaderboard_me_null_without_results(fresh_db):
    client = await _client(fresh_db)
    board = (await client.get("/games/weigh/leaderboard")).json()
    assert board == {"entries": [], "me": None, "period": "week"}


async def test_leaderboard_excludes_own_and_review_accounts(fresh_db, monkeypatch):
    client = await _client(fresh_db)
    for uid in (500, 600, 700):
        await fresh_db.get_or_create_user(telegram_id=uid, username=f"u{uid}")
        await _insert(uid, "catch", uid, _this_week())
    monkeypatch.setattr(config, "ADMIN_ID", 500)
    monkeypatch.setattr(config, "TEST_USER_ID", 600)
    await fresh_db.link_auth_identity(700, api_v1_games.REVIEW_DEMO_PROVIDER, "appreview")
    await _insert(111, "catch", 3, _this_week())
    board = (await client.get("/games/catch/leaderboard?period=all")).json()
    assert [e["score"] for e in board["entries"]] == [3]
    assert board["me"] == {"rank": 1, "total": 1, "best": 3}


async def test_own_account_gets_no_week_rank(fresh_db, monkeypatch):
    client = await _client(fresh_db)
    monkeypatch.setattr(config, "ADMIN_ID", 111)
    body = (await _post(client, score=5)).json()
    assert body == {"best": 5, "is_new_best": True, "week_rank": None}


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_leaderboard_names_anonymous_unless_shown(fresh_db, lang):
    client = await _client(fresh_db, lang=lang)
    await fresh_db.get_or_create_user(telegram_id=222, username="a_very_long_telegram_username")
    await fresh_db.get_or_create_user(telegram_id=333, username=None)
    await _insert(222, "catch", 30, _this_week())
    await _insert(333, "catch", 20, _this_week())
    await _insert(111, "catch", 10, _this_week())
    await db.set_game_show_name(222, True)
    await db.set_game_show_name(333, True)  # имени нет — остаётся «Атлетом»

    entries = (await client.get("/games/catch/leaderboard")).json()["entries"]
    names = [e["name"] for e in entries]
    assert names[0] == "a_very_long_telegram"  # 20 символов
    prefix = "Атлет #" if lang == "ru" else "Athlete #"
    assert names[1] == f"{prefix}{games.anon_number(333):04d}"
    assert names[2] == f"{prefix}{games.anon_number(111):04d}"

    await client.patch("/games/settings", json={"show_name": True})
    entries = (await client.get("/games/catch/leaderboard")).json()["entries"]
    assert entries[2]["name"] == "tester"


def test_anon_number_is_stable_four_digits_and_not_the_id():
    n = games.anon_number(123456789)
    assert n == games.anon_number(123456789)
    assert 0 <= n < 10000
    assert {games.anon_number(i) for i in range(1, 200)} != set(range(1, 200))
    with i18n.use_lang("en"):
        assert games.leaderboard_name(5, None, True) == f"Athlete #{games.anon_number(5):04d}"


# --- снос и чистка ------------------------------------------------------------


async def test_account_deletion_wipes_games(fresh_db):
    client = await _client(fresh_db)
    await _post(client, score=5)
    await client.patch("/games/settings", json={"show_name": True})
    left = await account_deletion.delete_account(111)
    assert left == {}
    assert await _count_rows(111) == 0
    cur = await db.conn().execute("SELECT COUNT(*) FROM minigame_settings WHERE user_id = 111")
    assert (await cur.fetchone())[0] == 0


async def test_retention_prunes_old_results(fresh_db):
    await fresh_db.get_or_create_user(telegram_id=111, username="tester")
    now = timeutil.utc_now()
    old = (now - dt.timedelta(days=config.GAME_RESULTS_RETENTION_DAYS + 1)).isoformat(timespec="seconds")
    fresh = (now - dt.timedelta(days=config.GAME_RESULTS_RETENTION_DAYS - 1)).isoformat(timespec="seconds")
    await _insert(111, "catch", 5, old)
    await _insert(111, "catch", 6, fresh)
    assert await admin_tasks._run_retention_cleanup()
    cur = await db.conn().execute("SELECT score FROM minigame_results WHERE user_id = 111")
    assert [r[0] for r in await cur.fetchall()] == [6]


def test_retention_is_400_days():
    assert config.GAME_RESULTS_RETENTION_DAYS == 400


async def test_games_follow_app_account_into_telegram_account(fresh_db):
    """Партии app-only аккаунта переезжают при привязке Telegram и не мешают
    слиянию с аккаунтом, у которого уже есть история."""
    await fresh_db.get_or_create_user(telegram_id=555, username="tg")
    await fresh_db.create_finished_workout(555, "2026-01-01T10:00:00", "2026-01-01T11:00:00")
    app = await fresh_db.create_app_only_user(language_code="ru")
    app_id = app["telegram_id"]
    await _insert(app_id, "catch", 42, "2026-10-01T10:00:00")
    await db.set_game_show_name(app_id, True)

    assert await fresh_db.link_telegram_to_app_account(app_id, 555) == "ok"
    assert await db.game_best(555, "catch") == 42
    assert await db.get_game_show_name(555) is True
    assert await _count_rows(app_id) == 0
