"""REST `/v1` для экрана прогресса упражнения — api_v1_progress.py.

Главное, что здесь проверяется: сервер отдаёт ОДНУ точку на тренировку и по
оси e1RM. Приложение раньше считало график само по сырым подходам и ошибалось
ровно в этих двух местах — точка на каждый подход (линия пилила внутри одного
дня) и вес снаряда вместо оценки максимума. Поэтому тесты берут подходы так,
чтобы e1RM заведомо не совпадал с максимальным весом: иначе «правильно» и
«неправильно» дают одно и то же число, и тест ничего не ловит.

По образцу tests/test_api_v1_account.py: httpx поверх ASGI-приложения без
сокета, `client_factory` и `_linked_client` заведены локально (в чужой
тестовый файл не лезем — его может в это же время править другой агент).
"""

import httpx
import pytest

import analytics
import api_v1
import db

pytestmark = pytest.mark.asyncio


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
    return client


async def _log_session(user_id: int, ex_id: int, day: int, sets: list[tuple[float, int]]) -> int:
    """Одна законченная тренировка с перечисленными подходами этого упражнения."""
    workout_id = await db.create_finished_workout(
        user_id,
        started_at=f"2026-03-{day:02d}T10:00:00",
        finished_at=f"2026-03-{day:02d}T11:00:00",
    )
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    for i, (weight, reps) in enumerate(sets):
        await db.add_set(block_id, ex_id, round_index=i + 1, order_in_round=0, weight=weight, reps=reps)
    return workout_id


async def _exercise(user_id: int, name: str = "Жим лёжа") -> int:
    group_id = await db.create_muscle_group(user_id, "Грудь")
    return await db.create_exercise(user_id, name, group_id)


# ---------- одна точка на тренировку ----------


async def test_two_workouts_of_three_sets_give_exactly_two_points(fresh_db, client_factory):
    """Главная проверка. Шесть подходов — две точки, а не шесть: точка на
    тренировку, а не на подход."""
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    ex_id = await _exercise(user_id)
    w1 = await _log_session(user_id, ex_id, 1, [(50.0, 10), (50.0, 9), (50.0, 8)])
    w2 = await _log_session(user_id, ex_id, 8, [(55.0, 10), (55.0, 9), (55.0, 8)])

    resp = await client.get(f"/exercises/{ex_id}/progress/sessions")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert len(body["points"]) == 2
    assert [p["workout_id"] for p in body["points"]] == [w1, w2]
    assert [p["date"] for p in body["points"]] == ["2026-03-01", "2026-03-08"]
    assert [p["sets"] for p in body["points"]] == [3, 3]
    assert body["metric"] == "e1rm"


async def test_point_value_is_e1rm_not_the_heaviest_weight(fresh_db, client_factory):
    """Подходы подобраны так, что тяжелейший подход (60×2) — НЕ лучший по e1RM
    (50×10). Клиент раньше рисовал 60, сервер обязан отдать ~66.7."""
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    ex_id = await _exercise(user_id)
    await _log_session(user_id, ex_id, 1, [(50.0, 10), (60.0, 2)])

    body = (await client.get(f"/exercises/{ex_id}/progress/sessions")).json()
    point = body["points"][0]

    expected = round(analytics.e1rm(50.0, 10, "epley"), 1)
    assert expected == pytest.approx(66.7, abs=0.05)
    assert point["value"] == expected
    assert point["value"] != 60.0
    # Лучший подход подписывается в том же виде, в каком продукт пишет подход везде.
    assert point["top_set"] == "50×10"


# ---------- период ----------


async def test_limit_keeps_the_latest_workouts(fresh_db, client_factory):
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    ex_id = await _exercise(user_id)
    for day, weight in ((1, 40.0), (8, 50.0), (15, 60.0)):
        await _log_session(user_id, ex_id, day, [(weight, 5)])

    body = (await client.get(f"/exercises/{ex_id}/progress/sessions?limit=2")).json()

    assert [p["date"] for p in body["points"]] == ["2026-03-08", "2026-03-15"]


async def test_default_limit_is_the_bots_own(fresh_db, client_factory):
    """Без параметра отдаётся столько же тренировок, сколько показывает бот по
    умолчанию (keyboards.DEFAULT_PROGRESS_LIMIT = 20), а не вся история."""
    import keyboards

    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    ex_id = await _exercise(user_id)
    for day in range(1, 26):
        await _log_session(user_id, ex_id, day, [(40.0 + day, 5)])

    body = (await client.get(f"/exercises/{ex_id}/progress/sessions")).json()
    assert len(body["points"]) == keyboards.DEFAULT_PROGRESS_LIMIT == 20
    assert body["points"][-1]["date"] == "2026-03-25"


# ---------- тренд ----------


async def test_trend_is_null_for_a_single_point(fresh_db, client_factory):
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    ex_id = await _exercise(user_id)
    await _log_session(user_id, ex_id, 1, [(50.0, 10), (50.0, 8)])

    body = (await client.get(f"/exercises/{ex_id}/progress/sessions")).json()

    assert len(body["points"]) == 1
    assert body["trend"] is None
    assert body["comparison"] is None


async def test_trend_rises_over_two_workouts(fresh_db, client_factory):
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    ex_id = await _exercise(user_id)
    await _log_session(user_id, ex_id, 1, [(50.0, 5)])
    await _log_session(user_id, ex_id, 8, [(60.0, 5)])

    body = (await client.get(f"/exercises/{ex_id}/progress/sessions")).json()

    values = [p["value"] for p in body["points"]]
    assert body["trend"]["total_change"] == pytest.approx(values[1] - values[0], abs=0.05)
    # Ровно неделя между тренировками — наклон за неделю равен всему приросту.
    assert body["trend"]["slope_per_week"] == pytest.approx(values[1] - values[0], abs=0.05)


# ---------- формула и единицы пользователя ----------


async def test_user_formula_changes_the_point_values(fresh_db, client_factory):
    """e1RM считается по формуле из users.e1rm_formula — именно это приложение
    игнорировало, считая всё по Эпли (а чаще — не считая вовсе)."""
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    ex_id = await _exercise(user_id)
    await _log_session(user_id, ex_id, 1, [(100.0, 8)])

    epley = (await client.get(f"/exercises/{ex_id}/progress/sessions")).json()
    await db.update_user(user_id, e1rm_formula="brzycki")
    brzycki = (await client.get(f"/exercises/{ex_id}/progress/sessions")).json()

    assert epley["points"][0]["value"] != brzycki["points"][0]["value"]
    assert epley["points"][0]["value"] == round(analytics.e1rm(100.0, 8, "epley"), 1)
    assert brzycki["points"][0]["value"] == round(analytics.e1rm(100.0, 8, "brzycki"), 1)


async def test_records_come_from_the_whole_history(fresh_db, client_factory):
    """Рекорды — по всей истории, даже когда показан короткий период: иначе
    рекорд «терялся» бы при переключении кнопки периода."""
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    ex_id = await _exercise(user_id)
    await _log_session(user_id, ex_id, 1, [(120.0, 1)])
    await _log_session(user_id, ex_id, 8, [(50.0, 5)])
    await _log_session(user_id, ex_id, 15, [(55.0, 5)])

    body = (await client.get(f"/exercises/{ex_id}/progress/sessions?limit=1")).json()

    assert len(body["points"]) == 1
    assert body["records"]["best_weight"] == 120.0
    assert body["records"]["best_set"] == "120×1"
    assert body["records"]["best_e1rm"] == round(analytics.e1rm(120.0, 1, "epley"), 1)


# ---------- упражнение своим весом ----------


async def test_bodyweight_exercise_is_measured_in_reps(fresh_db, client_factory):
    """У подтягиваний своим весом e1RM нулевой у всех подходов сразу, поэтому
    метрика — повторы, и клиент подписывает ось по `metric`."""
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    ex_id = await _exercise(user_id, "Подтягивания")
    await _log_session(user_id, ex_id, 1, [(0.0, 10), (0.0, 8)])
    await _log_session(user_id, ex_id, 8, [(0.0, 12), (0.0, 9)])

    body = (await client.get(f"/exercises/{ex_id}/progress/sessions")).json()

    assert body["metric"] == "reps"
    assert body["unit"] is None
    assert [p["value"] for p in body["points"]] == [10, 12]


async def test_only_the_last_modes_sessions_are_plotted(fresh_db, client_factory):
    """Упражнение сменило режим: килограммы e1RM и голые повторы на одной оси
    читаются как обвал силы, поэтому в график идёт только режим последней
    тренировки (то же решение, что у бота)."""
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    ex_id = await _exercise(user_id, "Подтягивания")
    await _log_session(user_id, ex_id, 1, [(20.0, 5)])  # с весом
    await _log_session(user_id, ex_id, 8, [(0.0, 12)])  # своим весом
    await _log_session(user_id, ex_id, 15, [(0.0, 14)])

    body = (await client.get(f"/exercises/{ex_id}/progress/sessions")).json()

    assert body["metric"] == "reps"
    assert [p["date"] for p in body["points"]] == ["2026-03-08", "2026-03-15"]


# ---------- доступ ----------


async def test_requires_auth(fresh_db, client_factory):
    client = client_factory()
    resp = await client.get("/exercises/1/progress/sessions")
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"


async def test_someone_elses_exercise_is_404(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory, telegram_id=111)
    await fresh_db.get_or_create_user(telegram_id=222, username="stranger")
    stranger_ex = await _exercise(222, "Чужой жим")

    resp = await client.get(f"/exercises/{stranger_ex}/progress/sessions")
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


# ---------- язык ----------


async def test_texts_follow_the_users_language(fresh_db, client_factory):
    """Тексты рендерятся под i18n.use_lang(users.lang): у англоязычного атлета
    в ответе не должно остаться ни одной кириллической буквы."""
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    await db.set_user_lang(user_id, "en")
    ex_id = await _exercise(user_id, "Bench press")
    await _log_session(user_id, ex_id, 1, [(50.0, 5)])
    await _log_session(user_id, ex_id, 8, [(60.0, 5)])

    body = (await client.get(f"/exercises/{ex_id}/progress/sessions")).json()

    texts = [body["metric_label"], body["unit"], body["comparison"]["text"]]
    for text in texts:
        assert not any("а" <= ch.lower() <= "я" for ch in text), text
    assert body["comparison"]["text"]
