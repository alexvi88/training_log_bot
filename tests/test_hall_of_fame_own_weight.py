"""Рекорд «своим весом» в зале славы.

Подтягивания с записанным взвешиванием считают нагрузкой вес тела
(sets.load_weight), и строка рекорда шла как «81.5×10» — читалось как
подтягивания с блином 81.5. Теперь такой рекорд — «свой вес × 10» (с
добавкой на поясе — «свой вес +10кг × 8»), а вес тела и e1RM остаются
числами рядом: `weight`/`e1rm` в /v1 и e1RM в строке бота.
"""

import httpx
import pytest

import api_v1
import formatting
import hall_of_fame_data
import i18n

PULLUPS = "Подтягивания"
BENCH = "Жим штанги лёжа"


async def _own(db, user_id, template_name):
    template = next(t for t in await db.list_all_exercise_templates() if t["name"] == template_name)
    return await db.fork_exercise_from_template(user_id, template["id"])


async def _log(db, user_id, ex_id, weight, reps):
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.append_set(block_id, ex_id, 0, weight, reps)
    await db.finish_workout(workout_id)


async def _seed(db, user_id, *, belt: float = 0.0):
    await db.add_bodyweight_log(user_id, 81.5)
    pullups = await _own(db, user_id, PULLUPS)
    bench = await _own(db, user_id, BENCH)
    await _log(db, user_id, pullups, belt, 10)
    await _log(db, user_id, bench, 72.5, 5)
    return pullups, bench


async def test_pullups_record_is_marked_as_own_weight(fresh_db, user_id):
    pullups, bench = await _seed(fresh_db, user_id)
    hof = await hall_of_fame_data.collect(user_id)
    by_id = dict(zip(hof.top_lift_ids, zip(hof.top_lifts, hof.top_lift_own_weight, strict=True), strict=True))
    (_, weight, reps, _), own = by_id[pullups]
    assert (weight, reps, own) == (81.5, 10, 0.0)
    # Обычное железо так и остаётся числом.
    assert by_id[bench][1] is None


async def test_bot_line_says_own_weight_not_a_plate(fresh_db, user_id):
    await _seed(fresh_db, user_id)
    hof = await hall_of_fame_data.collect(user_id)
    with i18n.use_lang("ru"):
        text = formatting.build_hall_of_fame(
            total_workouts=hof.total_workouts, tonnage_kg=hof.tonnage_kg,
            tonnage_equivalent=None, best_week_streak=0, longest_workout_seconds=0,
            top_lifts=hof.top_lifts, own_weight=hof.top_lift_own_weight, unit=hof.unit,
        )
    assert "Подтягивания — свой вес × 10 · e1RM" in text
    assert "81.5×10" not in text
    assert "72.5×5" in text


def test_own_weight_record_texts():
    with i18n.use_lang("ru"):
        assert formatting.format_own_weight_record(10, 0.0) == "свой вес × 10"
        assert formatting.format_own_weight_record(8, 10.0) == "свой вес +10кг × 8"
        assert formatting.format_own_weight_record(10, -20.0) == "свой вес −20кг × 10"
    with i18n.use_lang("en"):
        assert formatting.format_own_weight_record(10, 0.0) == "bodyweight × 10"
        assert formatting.format_own_weight_record(8, 22.5, "lb") == "bodyweight +22.5lb × 8"


@pytest.mark.parametrize("lang,record", [("ru", "свой вес +10кг × 10"), ("en", "bodyweight +10kg × 10")])
async def test_v1_lift_carries_with_bodyweight(fresh_db, user_id, lang, record):
    await fresh_db.set_user_lang(user_id, lang)
    pullups, bench = await _seed(fresh_db, user_id, belt=10.0)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    code = await fresh_db.issue_oauth_link_code(user_id, ttl_seconds=600, digits=8)
    client.headers["Authorization"] = f"Bearer {(await client.post('/auth/link', json={'code': code})).json()['token']}"
    lifts = {lift["exercise_id"]: lift for lift in (await client.get("/hall-of-fame")).json()["top_lifts"]}
    pull = lifts[pullups]
    assert pull["with_bodyweight"] is True and pull["is_bodyweight"] is False
    assert pull["record"] == record
    # Вся нагрузка — вес тела плюс пояс, по ней e1RM.
    assert pull["weight"] == 91.5 and pull["e1rm"] > 91.5
    assert lifts[bench]["with_bodyweight"] is False
    assert lifts[bench]["record"] == "72.5×5"
