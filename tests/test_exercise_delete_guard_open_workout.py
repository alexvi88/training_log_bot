"""«Снести упражнение, если по нему ничего нет» — и открытая тренировка.

`db.delete_exercise_if_unused` (откат «тренер завёл упражнение» и снятие
импорта) проверяет подходы только в ЗАКОНЧЕННЫХ тренировках
(`list_sets_for_exercise`) и места в программах. Упражнение, которое стоит в
идущей тренировке — с подходом или просто открытым блоком, — проходит эту
проверку, и `DELETE` валится на внешнем ключе `sets`/`block_exercises`.
Данные целы (их держит FK), но вместо честного «не вышло» наружу летит
`IntegrityError`: кнопка отката в боте падает в общий обработчик ошибок, а
`POST /v1/import/batches/{id}/undo` отвечает 500, успев снять тренировки
импорта, но не пометив пачку отменённой и не убрав её значки.

Поведение, которое здесь закреплено, — то, что обещает докстринг функции:
«по нему уже что-то записали — отказываемся». Тесты помечены xfail(strict):
`db.py` в этом заходе правят параллельно, починка — одной правкой проверки в
`delete_exercise_if_unused` (см. отчёт), после неё пометку надо снять.
"""

import httpx
import pytest

import ai_trainer
import ai_undo
import api_v1

pytestmark = pytest.mark.asyncio

BUG = (
    "delete_exercise_if_unused не видит подходы и блоки открытой тренировки — "
    "IntegrityError вместо отказа"
)


async def _open_workout_with(db, user_id, exercise_id, with_set: bool):
    workout = await db.create_workout(user_id)
    block = await db.create_block(workout, "single")
    await db.add_block_exercise(block, exercise_id, 0)
    if with_set:
        await db.add_set(block, exercise_id, 0, 0, 60.0, 10, None)
    return workout, block


@pytest.mark.xfail(strict=True, reason=BUG)
@pytest.mark.parametrize("with_set", [True, False], ids=["with-set", "open-block"])
async def test_ai_new_exercise_undo_refuses_when_it_is_in_the_open_workout(fresh_db, user_id, with_set):
    db = fresh_db
    gid = await db.create_muscle_group(user_id, "Грудь")
    ex = await db.create_exercise(user_id, "Заведено тренером", gid)
    _, block = await _open_workout_with(db, user_id, ex, with_set)

    assert await ai_undo.apply(user_id, {"kind": "exercise_new", "id": ex}) is None
    assert await db.get_exercise(ex) is not None
    assert len(await db.list_sets_for_block(block)) == (1 if with_set else 0)


@pytest.mark.xfail(strict=True, reason=BUG)
async def test_import_undo_keeps_exercise_used_in_the_open_workout(fresh_db, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: False)
    db = fresh_db
    await db.get_or_create_user(telegram_id=111, username="tester")
    code = await db.issue_oauth_link_code(111, ttl_seconds=600, digits=8)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    token = (await client.post("/auth/link", json={"code": code})).json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"

    result = (await client.post("/import/csv", json={
        "csv": "date,exercise,weight,reps\n2024-01-01,Присед,100,5\n",
    })).json()
    squat = (await db.find_exercise_by_name(111, "Присед"))["id"]
    # После переезда человек начал тренировку и сделал подход в импортированном
    # упражнении — и тут решил отменить импорт.
    await _open_workout_with(db, 111, squat, with_set=True)

    resp = await client.post(f"/import/batches/{result['batch_id']}/undo")

    assert resp.status_code == 200, resp.text
    assert resp.json()["removed_workouts"] == 1
    assert resp.json()["removed_exercises"] == 0
    assert await db.get_exercise(squat) is not None
    # Пачка помечена отменённой — повтор не находит её.
    assert (await client.post(f"/import/batches/{result['batch_id']}/undo")).status_code == 404
