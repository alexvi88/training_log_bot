"""Схема «топ-сет + бэкоффы» и разгрузка каждые N недель.

Живой прогон тренера: продвинутому силовику (присед 180 / жим 125 / тяга 220)
досталось 5×3–5 с двойной прогрессией «+2.5, когда возьмёшь 5×5» — на ~89 %
это невыполнимо, а «топ-сет + бэкоффы» из текста тренера схема выразить не
могла. Разгрузки не было ни в одной из девяти программ.
"""

import datetime as dt
import json

import httpx
import pytest

import ai_program_actions
import ai_trainer
import analytics
import api_v1
import db
import formatting
import i18n
import progression_data

TSB = {
    "rule": "top_set_backoff", "top_reps_min": 1, "top_reps_max": 3,
    "backoff_sets": 3, "backoff_pct": 90, "step": 2.5,
}


# ---------- чистка правила ----------

def test_clean_keeps_and_clamps_the_fields():
    out = ai_trainer._clean_progression({
        "rule": "top_set_backoff", "top_reps_min": 12, "top_reps_max": 0,
        "backoff_sets": 9, "backoff_pct": 40, "step": 100,
    })
    assert out == {
        "rule": "top_set_backoff", "top_reps_min": 1, "top_reps_max": 8,
        "backoff_sets": 5, "backoff_pct": 70, "step": analytics.progression_max_step("kg"),
    }


def test_clean_fills_missing_fields_with_typical_values():
    out = ai_trainer._clean_progression({"rule": "top_set_backoff", "top_reps_max": 3})
    assert out == {
        "rule": "top_set_backoff", "top_reps_min": 3, "top_reps_max": 3,
        "backoff_sets": 3, "backoff_pct": 85,
    }


def test_program_item_scheme_follows_the_rule():
    """План на карточке — один топ плюс бэкоффы, повторы — диапазон топа:
    иначе «🎯 Топ-сет» спорил бы с «План: 5×3–5» строкой выше."""
    item = ai_trainer._clean_program_item({
        "name": "Присед со штангой", "sets": 5, "reps_min": 3, "reps_max": 5,
        "progression": TSB,
    })
    assert (item["sets"], item["reps_min"], item["reps_max"]) == (4, 1, 3)
    assert any("топ-сет" in note for note in item["clamped"])
    assert item["progression"]["rule"] == "top_set_backoff"


def test_bodyweight_exercise_gets_double_progression_instead():
    item = ai_trainer._clean_program_item({
        "name": "Подтягивания", "sets": 4, "reps_min": 5, "reps_max": 8, "progression": TSB,
    })
    assert item["progression"] == {"rule": "double_progression", "reps_top": 8}
    assert (item["sets"], item["reps_min"], item["reps_max"]) == (4, 5, 8)


# ---------- подсказка ----------

def test_first_set_is_the_top_set_with_a_step_when_the_top_was_hit_with_reserve():
    s = analytics.suggest_top_set_backoff([(160, 3, 8.0), (145, 3, None)], [], TSB)
    assert (s.role, s.action, s.target_weight, s.target_reps) == ("top", "add_weight", 162.5, 1)


def test_top_set_without_reserve_holds_the_weight():
    s = analytics.suggest_top_set_backoff([(160, 3, 9.5)], [], TSB)
    assert (s.role, s.target_weight, s.target_reps) == ("top", 160, 3)
    s = analytics.suggest_top_set_backoff([(160, 2, None)], [], TSB)
    assert (s.action, s.target_weight, s.target_reps) == ("add_reps", 160, 3)


def test_following_sets_are_backoffs_from_todays_top_rounded_to_the_plate():
    s = analytics.suggest_top_set_backoff([(160, 3, None)], [(161, 3)], TSB)
    # 161 × 0.9 = 144.9 → 145 на блинах по 2.5.
    assert (s.role, s.target_weight, s.target_reps) == ("backoff", 145.0, 3)


def test_hint_text_and_fields_for_top_and_backoff():
    with i18n.use_lang("ru"):
        top = progression_data.hint([(157.5, 3, 7.0)], [], unit="kg", formula="epley", rule=TSB)
        assert top["text"].startswith("🎯 Топ-сет: 160×1")
        assert (top["role"], top["is_deload"], top["achieved"]) == ("top", False, False)
        back = progression_data.hint(
            [(157.5, 3, 7.0)], [(160, 3)], unit="kg", formula="epley", rule=TSB
        )
        assert back["text"] == "🎯 Бэкофф: 145×3 — 90% от топа"
        assert back["role"] == "backoff" and back["target_weight"] == 145.0
        done = progression_data.hint(
            [(157.5, 3, 7.0)], [(160, 3), (145, 3), (145, 3), (145, 3)],
            unit="kg", formula="epley", rule=TSB,
        )
        assert done["achieved"] is True and done["text"].startswith("✅ Бэкоффы сделаны")
    with i18n.use_lang("en"):
        back = progression_data.hint(
            [(157.5, 3, 7.0)], [(160, 3)], unit="kg", formula="epley", rule=TSB
        )
        assert back["text"] == "🎯 Backoff: 145×3 — 90% of your top set"


def test_other_rules_have_no_role():
    hint = progression_data.hint([(50, 10, None)], [], unit="kg", formula="epley")
    assert hint["role"] is None and hint["is_deload"] is False


# ---------- разгрузка ----------

def test_deload_week_is_every_nth_calendar_week_from_the_program_start():
    start = dt.date(2026, 9, 2)  # среда, неделя 1 — с понедельника 31.08
    weeks = [
        analytics.is_deload_week(start, start + dt.timedelta(weeks=n), 4) for n in range(9)
    ]
    assert weeks == [False, False, False, True, False, False, False, True, False]
    # Понедельник четвёртой недели — уже разгрузка, воскресенье третьей — ещё нет.
    assert analytics.is_deload_week(start, dt.date(2026, 9, 21), 4)
    assert not analytics.is_deload_week(start, dt.date(2026, 9, 20), 4)
    assert not analytics.is_deload_week(start, dt.date(2026, 9, 21), None)


def test_deload_hint_drops_the_weight_and_says_to_cut_sets():
    with i18n.use_lang("ru"):
        hint = progression_data.hint(
            [(100, 12, None)], [], unit="kg", formula="epley", is_deload=True
        )
        assert hint["is_deload"] is True
        # Без прибавки и на 90 % прошлого: 100 → 90.
        assert (hint["target_weight"], hint["target_reps"]) == (90.0, 12)
        assert hint["text"] == "🪫 Неделя разгрузки: 90×12 — сделай ~60% подходов на ~90% веса"
    with i18n.use_lang("en"):
        hint = progression_data.hint(
            [(100, 12, None)], [], unit="kg", formula="epley", is_deload=True
        )
        assert hint["text"] == "🪫 Deload week: 90×12 — do ~60% of your sets at ~90% weight"


def test_clean_deload_every_weeks():
    assert db.clean_deload_every_weeks(5) == 5
    assert db.clean_deload_every_weeks(2) == 4
    assert db.clean_deload_every_weeks(12) == 6
    assert db.clean_deload_every_weeks(None) is None
    assert db.clean_deload_every_weeks(0) is None
    assert db.clean_deload_every_weeks("x") is None


# ---------- текст правила и превью ----------

def test_rule_and_deload_read_as_text():
    with i18n.use_lang("ru"):
        assert formatting.format_progression_rule(TSB, "kg") == (
            "топ-сет 1–3, дальше 3 подхода по 90%; прибавка 2.5кг"
        )
        assert formatting.format_deload_note(4) == (
            "🪫 Разгрузка каждые 4 недели: в эту неделю ~60% подходов на ~90% веса"
        )
        assert "каждые 5 недель" in formatting.format_deload_note(5)
        assert formatting.format_deload_note(None) == ""
    with i18n.use_lang("en"):
        assert formatting.format_progression_rule(TSB, "kg") == (
            "top set 1–3, then 3 sets at 90%; add 2.5kg"
        )
        assert formatting.format_deload_note(6).startswith("🪫 Deload every 6 weeks")


def test_preview_shows_the_deload_line():
    days = [{"name": "День 1", "items": [
        {"name": "Присед", "target": "4×1–3", "progression": TSB},
    ]}]
    with i18n.use_lang("ru"):
        text = formatting.build_ai_program_preview("Сила", days, deload_every_weeks=4)
    assert "Разгрузка каждые 4 недели" in text
    assert "топ-сет 1–3" in text


# ---------- propose_program → сохранение → тренировка ----------

async def _noop(_draft):
    return None


async def test_propose_and_save_carry_deload_and_rule(fresh_db, user_id):
    drafts = []

    async def _keep(draft):
        drafts.append(draft)

    await ai_trainer.execute_tool(
        user_id, "propose_program",
        {
            "name": "Сила", "description": "Силовой блок.", "deload_every_weeks": 5,
            "days": [{"name": "День 1", "exercises": [
                {"name": "Присед со штангой", "sets": 4, "reps_min": 1, "reps_max": 3,
                 "progression": TSB},
            ]}],
        },
        on_program=_keep,
    )
    draft = drafts[-1]
    assert draft["deload_every_weeks"] == 5
    result = await ai_program_actions.finalize_program_save(user_id, draft)
    program = await fresh_db.get_program(result["program_id"])
    assert program["deload_every_weeks"] == 5
    day = (await fresh_db.list_program_days_by_id(program["id"]))[0]
    entry = (await fresh_db.list_routine_exercises(day["id"]))[0]
    assert json.loads(entry["progression"])["rule"] == "top_set_backoff"

    # Тренер видит разгрузку в сохранённой программе — чтобы перенести при правке.
    saved = await ai_trainer._saved_programs(user_id, "Сила")
    assert saved["program"]["deload_every_weeks"] == 5


async def _program_workout(fresh_db, user_id, created_at, started_at, weeks=4):
    program_id = await fresh_db.create_program(user_id, "Сила", deload_every_weeks=weeks)
    await fresh_db.conn().execute(
        "UPDATE programs SET created_at = ? WHERE id = ?", (created_at, program_id)
    )
    await fresh_db.conn().commit()
    routine_id = await fresh_db.create_routine(user_id, "День 1", program_id=program_id)
    return await fresh_db.create_workout(user_id, started_at=started_at, routine_id=routine_id)


async def test_deload_week_for_workout(fresh_db, user_id):
    deload = await _program_workout(
        fresh_db, user_id, "2026-03-02T10:00:00", "2026-03-23T10:00:00"
    )
    assert await fresh_db.deload_week_for_workout(deload) is True
    await fresh_db.delete_program_by_id((await fresh_db.find_program_by_name(user_id, "Сила"))["id"])
    normal = await _program_workout(
        fresh_db, user_id, "2026-03-02T10:00:00", "2026-03-16T10:00:00"
    )
    assert await fresh_db.deload_week_for_workout(normal) is False
    loose = await fresh_db.create_workout(user_id, started_at="2026-03-23T10:00:00")
    assert await fresh_db.deload_week_for_workout(loose) is False


# ---------- REST /v1 ----------

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
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def test_api_program_carries_deload_and_patch_sets_it(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    created = (await client.post("/programs", json={"name": "Сила"})).json()
    assert created["deload_every_weeks"] is None
    resp = await client.patch(f"/programs/{created['id']}", json={"deload_every_weeks": 9})
    assert resp.status_code == 200, resp.text
    assert resp.json()["deload_every_weeks"] == 6
    got = (await client.get(f"/programs/{created['id']}")).json()
    assert got["deload_every_weeks"] == 6
    bad = await client.patch(f"/programs/{created['id']}", json={"deload_every_weeks": "x"})
    assert bad.status_code == 400
    cleared = await client.patch(f"/programs/{created['id']}", json={"deload_every_weeks": None})
    assert cleared.json()["deload_every_weeks"] is None


async def test_api_next_target_has_role_and_deload(fresh_db, client_factory):
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    group_id = await db.create_muscle_group(user_id, "Ноги")
    ex_id = await db.create_exercise(user_id, "Присед", group_id)
    past = await db.create_finished_workout(
        user_id, started_at="2026-03-09T10:00:00", finished_at="2026-03-09T11:00:00"
    )
    block = await db.create_block(past, "single")
    await db.add_block_exercise(block, ex_id, 0)
    await db.add_set(block, ex_id, round_index=1, order_in_round=0, weight=160.0, reps=3)

    workout_id = await _program_workout(
        fresh_db, user_id, "2026-03-02T10:00:00", "2026-03-23T10:00:00"
    )
    routine_id = (await db.get_workout(workout_id))["routine_id"]
    await db.append_routine_exercise(routine_id, ex_id, "4×1–3")
    entry = (await db.list_routine_exercises(routine_id))[-1]
    await db.set_routine_exercise_progression(entry["id"], json.dumps(TSB))

    hint = (await client.get(f"/workouts/{workout_id}/exercises/{ex_id}/next-target")).json()["hint"]
    assert hint["role"] == "top" and hint["is_deload"] is True
    # Разгрузка: без прибавки, 160 × 0.9 = 144 → 145.
    assert hint["target_weight"] == 145.0

    block = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block, ex_id, 0)
    await db.add_set(block, ex_id, round_index=1, order_in_round=0, weight=145.0, reps=3)
    hint = (await client.get(f"/workouts/{workout_id}/exercises/{ex_id}/next-target")).json()["hint"]
    assert hint["role"] == "backoff" and hint["is_deload"] is True
    assert hint["target_weight"] == 130.0
