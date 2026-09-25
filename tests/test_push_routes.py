"""Маршрут по тапу на iOS-баннер: ключ `route` рядом с `aps` в APNs payload.

Приложение (training_log_bot_ios, `PushRoute`) по нему открывает нужный экран:
плато — график того самого упражнения, недельная сводка — сводку. Без ключа
приложение просто открывается, как до маршрутов, — это и держат тесты на
«без маршрута».
"""

import datetime as dt

import pytest

import apns
import engagement
import push_ios
import push_texts
from tests.test_apns import FakeClient, FakeResponse, _configured  # noqa: F401 — autouse-фикстура конфига APNs

BENCH = "Жим штанги лёжа"


def _patch_client(monkeypatch) -> FakeClient:
    client = FakeClient(FakeResponse(200))

    async def get_client():
        return client

    monkeypatch.setattr(apns, "_get_client", get_client)
    monkeypatch.setattr(apns, "_provider_token_jwt", lambda: "jwt")
    return client


# ---------- apns.send_alert: route кладётся рядом с aps ----------


async def test_route_goes_next_to_aps_in_the_payload(monkeypatch):
    client = _patch_client(monkeypatch)
    route = {"screen": "exercise_progress", "exercise_id": 7, "exercise_name": "Жим"}

    ok = await apns.send_alert(1, "devtoken", "T", "B", category="plateau", route=route)

    assert ok is True
    payload = client.calls[0]["json"]
    assert payload["route"] == route
    assert payload["aps"] == {"alert": {"title": "T", "body": "B"}}


async def test_no_route_means_no_route_key(monkeypatch):
    client = _patch_client(monkeypatch)

    await apns.send_alert(1, "devtoken", "T", "B", category="announcement")

    assert "route" not in client.calls[0]["json"]


# ---------- push_ios.ios_route: категория → экран ----------


def test_plateau_routes_to_that_exercise_progress():
    assert push_ios.ios_route(push_texts.PLATEAU, exercise_id=12, exercise_name="Присед") == {
        "screen": "exercise_progress", "exercise_id": 12, "exercise_name": "Присед",
    }


def test_plateau_without_exercise_has_no_route():
    assert push_ios.ios_route(push_texts.PLATEAU) is None


@pytest.mark.parametrize("category", [push_texts.WEEKLY_DIGEST, push_texts.AI_WEEKLY])
def test_weekly_digest_routes_to_dashboard(category):
    assert push_ios.ios_route(category) == {"screen": "dashboard"}


@pytest.mark.parametrize(
    "category",
    [*push_texts.SKIP_CATEGORY_BY_DAY.values(), push_texts.STREAK_AT_RISK,
     push_texts.WIN_BACK, push_texts.NEWBIE_NUDGE],
)
def test_go_train_pushes_route_to_workout(category):
    assert push_ios.ios_route(category) == {"screen": "workout"}


@pytest.mark.parametrize("category", [push_texts.RANK_NEAR, push_texts.STREAK_MILESTONE])
def test_rank_pushes_route_to_achievements(category):
    assert push_ios.ios_route(category) == {"screen": "achievements"}


@pytest.mark.parametrize("category", [push_ios.ANNOUNCEMENT, "admin_test"])
def test_announcement_and_admin_test_have_no_route(category):
    assert push_ios.ios_route(category) is None


def test_every_ios_category_is_decided():
    """Новая категория пуша не должна молча остаться без маршрута: либо она
    есть в таблице, либо это осознанное исключение (плато — с параметрами,
    анонс — без маршрута)."""
    undecided = [
        c for c in push_ios.CATEGORIES
        if c not in push_ios.ROUTE_SCREEN_BY_CATEGORY
        and c not in (push_texts.PLATEAU, push_ios.ANNOUNCEMENT)
    ]
    assert undecided == []


# ---------- engagement: маршрут доезжает до payload ----------


async def _capture_send(monkeypatch) -> list[dict]:
    sent: list[dict] = []

    async def fake_send_alert(user_id, device_token, title, body, *, category=None, route=None):
        sent.append({"category": category, "route": route})
        return True

    monkeypatch.setattr(apns, "send_alert", fake_send_alert)

    async def tokens(_telegram_id):
        return ["devtoken"]

    monkeypatch.setattr(engagement, "_ios_device_tokens", tokens)
    return sent


async def test_plateau_push_carries_exercise_route(fresh_db, user_id, monkeypatch):
    sent = await _capture_send(monkeypatch)
    decision = engagement.PushDecision(
        push_texts.PLATEAU, "текст", ios_params={"exercise": "Жим"},
        ios_route_params={"exercise_id": 42, "exercise_name": "Жим"},
    )

    await engagement._send_apns_push(user_id, decision)

    assert sent == [{
        "category": push_texts.PLATEAU,
        "route": {"screen": "exercise_progress", "exercise_id": 42, "exercise_name": "Жим"},
    }]


async def test_weekly_digest_push_carries_dashboard_route(fresh_db, user_id, monkeypatch):
    sent = await _capture_send(monkeypatch)
    decision = engagement.PushDecision(
        push_texts.WEEKLY_DIGEST, "текст", with_cta=False,
        ios_params={"tonnage": "1.2т", "week_count": "3 тренировки"},
    )

    await engagement._send_apns_push(user_id, decision)

    assert sent == [{"category": push_texts.WEEKLY_DIGEST, "route": {"screen": "dashboard"}}]


async def test_plateau_decision_knows_which_exercise(fresh_db, user_id):
    """build_daily_push сам находит упражнение на плато и отдаёт его id —
    именно по нему приложение открывает график."""
    db = fresh_db
    template = next(t for t in await db.list_all_exercise_templates() if t["name"] == BENCH)
    ex_id = await db.fork_exercise_from_template(user_id, template["id"])
    # Три тренировки одним весом на 12 повторов — плато. Последняя в пятницу,
    # сегодня воскресенье: ни пропуск-веха, ни «серия под угрозой» не встают
    # раньше в цепочке приоритетов.
    for day in ("2026-07-06", "2026-07-08", "2026-07-10"):
        stamp = f"{day}T10:00:00"
        workout_id = await db.create_finished_workout(user_id, stamp, stamp)
        block_id = await db.create_block(workout_id, "single")
        await db.add_block_exercise(block_id, ex_id, 0)
        await db.add_set(block_id, ex_id, 0, 0, 100.0, 12)

    today = dt.date(2026, 7, 12)
    assert today.weekday() == 6
    decision = await engagement.build_daily_push(user_id, today)

    assert decision is not None
    assert decision.category == push_texts.PLATEAU
    assert decision.ios_route_params["exercise_id"] == ex_id
    assert decision.ios_route_params["exercise_name"] == decision.ios_params["exercise"]
