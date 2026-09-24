"""Сквозная проверка `/v1` настоящими HTTP-запросами: шесть мест, где REST
расходился либо с ботом, либо сам с собой.

Каждый тест здесь падал до правки — это не регрессионная сетка «на всякий
случай», а зафиксированные находки живого прогона.
"""

import asyncio
import datetime as dt

import httpx
import pytest

import api_v1
import db
import timeutil
import view_builder


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


# ---------- 1. перенос даты не должен стирать длительность ----------

@pytest.mark.asyncio
async def test_move_workout_date_keeps_duration_and_drops_ai_comment(fresh_db, client_factory):
    """PATCH /workouts/{id}/date звал только db.update_workout_date: подходы
    оставались на старом дне, целиком выпадали из окна «не позже finished_at»,
    и карточка теряла своё «· 55 мин» (а зал славы — longest_workout_seconds).
    Плюс AI-комментарий описывал сравнение, которого после переноса нет."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Присед"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]
    await client.post(
        f"/workouts/{workout_id}/sets", json={"exercise_id": exercise_id, "weight": 100, "reps": 5}
    )
    await client.post(
        f"/workouts/{workout_id}/sets", json={"exercise_id": exercise_id, "weight": 100, "reps": 5}
    )

    # Живая тренировка: первый подход в начале, последний — через 55 минут.
    started = dt.datetime.fromisoformat((await fresh_db.get_workout(workout_id))["started_at"])
    sets = await fresh_db.list_sets_for_workout_exercise(workout_id, exercise_id)
    for offset, row in zip((0, 55 * 60), sets, strict=True):
        await fresh_db.conn().execute(
            "UPDATE sets SET created_at = ? WHERE id = ?",
            ((started + dt.timedelta(seconds=offset)).isoformat(timespec="seconds"), row["id"]),
        )
    await fresh_db.conn().commit()
    await fresh_db.finish_workout(
        workout_id,
        finished_at=(started + dt.timedelta(minutes=56)).isoformat(timespec="seconds"),
    )
    await fresh_db.set_workout_ai_comment(workout_id, "разбор старых чисел")

    before = await view_builder.workout_duration_seconds(await fresh_db.get_workout(workout_id))
    assert before == 55 * 60

    # День — местный, не UTC: перенос ставит тренировку на выбранный
    # календарный день человека (workout_edit_data.move_workout_to_date).
    # Сравнение UTC-дат падало каждый вечер, как только местное время
    # пользователя (по умолчанию UTC+3) уходило в следующие сутки.
    offset = dt.timedelta(hours=await fresh_db.user_tz_offset(111))
    new_date = (started + offset - dt.timedelta(days=3)).date()
    resp = await client.patch(
        f"/workouts/{workout_id}/date", json={"date": new_date.isoformat()}
    )
    assert resp.status_code == 200, resp.text

    workout = await fresh_db.get_workout(workout_id)
    assert (dt.datetime.fromisoformat(workout["started_at"]) + offset).date() == new_date
    assert await view_builder.workout_duration_seconds(workout) == before
    assert workout["ai_comment"] is None


@pytest.mark.asyncio
async def test_bot_and_api_move_date_through_one_function(fresh_db, monkeypatch):
    """Второй реализации переноса быть не должно: экран правки в боте зовёт
    ту же workout_edit_data.move_workout_to_date, что и `/v1`."""
    import handlers.edit_workout as edit_workout
    import workout_edit_data

    calls = []

    async def fake_move(workout_id, new_date):
        calls.append((workout_id, new_date))

    monkeypatch.setattr(workout_edit_data, "move_workout_to_date", fake_move)
    monkeypatch.setattr(edit_workout, "move_workout_to_date", fake_move)
    await edit_workout._apply_edit_workout_date(7, dt.date(2024, 5, 1))
    assert calls == [(7, dt.date(2024, 5, 1))]


# ---------- 2. гонка параллельных подходов ----------

@pytest.mark.asyncio
async def test_concurrent_sets_do_not_split_exercise_into_blocks(fresh_db, client_factory):
    """Пять одновременных POST /workouts/{id}/sets с одним exercise_id
    успевали все пятеро не найти блок и завести по своему: упражнение
    показывалось в карточке пять раз по одному подходу, round_index у всех 1."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]

    responses = await asyncio.gather(
        *(
            client.post(
                f"/workouts/{workout_id}/sets",
                json={"exercise_id": exercise_id, "weight": 80, "reps": 5},
            )
            for _ in range(5)
        )
    )
    assert [r.status_code for r in responses] == [201] * 5

    blocks = await fresh_db.list_blocks_for_workout(workout_id)
    assert len(blocks) == 1

    active = (await client.get("/workouts/active")).json()
    assert len(active["blocks"]) == 1
    exercises = active["blocks"][0]["exercises"]
    assert len(exercises) == 1
    assert len(exercises[0]["sets"]) == 5

    rounds = sorted(
        r["round_index"]
        for r in await fresh_db.list_sets_for_block(blocks[0]["id"])
    )
    assert rounds == [1, 2, 3, 4, 5]


# ---------- 3. два способа отменить подход — один экран ----------

@pytest.mark.asyncio
async def test_undo_last_set_leaves_no_ghost_exercise(fresh_db, client_factory):
    """DELETE .../last-set оставлял блок с пустым списком подходов
    («упражнение есть, подходов нет»), а DELETE .../sets/{id} того же
    единственного подхода блок вычищал."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Тяга"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]
    await client.post(
        f"/workouts/{workout_id}/sets", json={"exercise_id": exercise_id, "weight": 70, "reps": 8}
    )

    resp = await client.delete(f"/workouts/{workout_id}/exercises/{exercise_id}/last-set")
    assert resp.status_code == 200
    via_last_set = (await client.get("/workouts/active")).json()["blocks"]
    assert via_last_set == []

    # тот же подход, снятый вторым маршрутом, — тот же экран
    set_id = (
        await client.post(
            f"/workouts/{workout_id}/sets",
            json={"exercise_id": exercise_id, "weight": 70, "reps": 8},
        )
    ).json()["id"]
    resp = await client.delete(f"/workouts/{workout_id}/sets/{set_id}")
    assert resp.status_code == 200
    assert (await client.get("/workouts/active")).json()["blocks"] == via_last_set


# ---------- 4. единые границы чисел подхода ----------

@pytest.mark.parametrize(
    "payload",
    [
        {"weight": -100, "reps": 8},
        {"weight": 100, "reps": -8},
        {"weight": 100, "reps": 0},
        {"weight": 100, "reps": 1000000000},
        {"weight": 100, "reps": 501},
        {"weight": 2000, "reps": 8},
        {"weight": 100, "reps": 8, "rpe": 99},
    ],
)
@pytest.mark.asyncio
async def test_live_set_rejects_what_its_editor_rejects(fresh_db, client_factory, payload):
    """POST /workouts/{id}/sets принимал вес -100, 10^9 повторов и RPE 99 —
    всё то, что PATCH того же подхода и разбор строки отвергают."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Присед"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]

    resp = await client.post(
        f"/workouts/{workout_id}/sets", json={"exercise_id": exercise_id, **payload}
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "bad_request"

    # и ни один такой подход не записался
    assert await fresh_db.list_sets_for_workout_exercise(workout_id, exercise_id) == []


@pytest.mark.asyncio
async def test_live_set_still_accepts_plausible_values(fresh_db, client_factory):
    """Границы не должны задевать честные подходы: 0 кг — это собственный вес
    (подтягивания), а RPE 10 — верх шкалы, а не перебор."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Подтягивания"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]

    resp = await client.post(
        f"/workouts/{workout_id}/sets",
        json={"exercise_id": exercise_id, "weight": 0, "reps": 12, "rpe": 10},
    )
    assert resp.status_code == 201, resp.text


# ---------- 5-6. «завтра» не бывает ----------

async def _user_dates(telegram_id: int = 111) -> tuple[dt.date, dt.date]:
    """«Сегодня» и «завтра» глазами сервера, а не контейнера.

    Сервер отбивает будущее по часовому поясу пользователя
    (`timeutil.user_today`), а тест раньше брал `dt.date.today()` машины. Пока
    часовые пояса совпадали, разницы не было, но у раннера UTC, и каждый вечер
    после определённого часа «завтра по UTC» оказывалось сегодняшним днём для
    пользователя — тест краснел по часам, а не по коду.
    """
    user = await db.get_user(telegram_id)
    today = timeutil.user_today(user)
    return today, today + dt.timedelta(days=1)



@pytest.mark.asyncio
async def test_patch_workout_date_rejects_future(fresh_db, client_factory):
    """POST /workouts/backfill будущее отвергает, а перенос даты принимал."""
    client = await _linked_client(fresh_db, client_factory)
    workout_id = (await client.post("/workouts/active")).json()["id"]
    today_date, tomorrow = await _user_dates()

    resp = await client.patch(f"/workouts/{workout_id}/date", json={"date": tomorrow.isoformat()})
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "date is in the future"

    today = await client.patch(
        f"/workouts/{workout_id}/date", json={"date": today_date.isoformat()}
    )
    assert today.status_code == 200


@pytest.mark.asyncio
async def test_add_food_entry_rejects_future_date(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    _, tomorrow = await _user_dates()

    resp = await client.post(
        "/food", json={"name": "Овсянка", "kcal": 300, "eaten_on": tomorrow.isoformat()}
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "eaten_on is in the future"

    ok = await client.post(
        "/food", json={"name": "Овсянка", "kcal": 300, "eaten_on": dt.date.today().isoformat()}
    )
    assert ok.status_code == 201, ok.text
