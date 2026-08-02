"""AI-тренер собирает программу: разбор предложения, превью и сохранение.

Ключевой инвариант всей фичи — propose_program НИЧЕГО не пишет: пока
пользователь не нажал кнопку, у него не должно появиться ни программы, ни
форкнутого из каталога упражнения (см. ai_trainer._propose_program).
"""

import json

import ai_trainer
import formatting
import keyboards

# asyncio_mode=auto (pytest.ini) — async-тестам маркер не нужен, а часть
# проверок ниже чисто синтетические и синхронные.

# Оба есть в глобальном каталоге (seed_data.EXERCISE_TEMPLATES), поэтому
# резолвятся у любого пользователя, даже пустого.
TEMPLATE_A = "Жим штанги лёжа"
TEMPLATE_B = "Присед со штангой"


def _day(name: str, exercises: list[dict]) -> dict:
    return {"name": name, "exercises": exercises}


async def _propose(user_id: int, tool_input: dict) -> tuple[dict, dict | None]:
    """Вызов инструмента через execute_tool — тем же путём, что и агентный цикл."""
    captured: list[dict] = []

    async def on_program(draft: dict) -> None:
        captured.append(draft)

    raw = await ai_trainer.execute_tool(
        user_id, "propose_program", tool_input, on_program=on_program
    )
    return json.loads(raw), (captured[-1] if captured else None)


# ---------- предложение ничего не сохраняет ----------

async def test_proposal_creates_no_routine_and_no_exercise(fresh_db, user_id):
    payload, draft = await _propose(
        user_id,
        {
            "name": "Фуллбоди",
            "days": [_day("День 1", [{"name": TEMPLATE_A, "sets": 3, "reps_min": 5, "reps_max": 10}])],
        },
    )

    assert payload["saved"] is False
    assert payload["shown_to_user"] is True
    assert draft is not None
    assert await fresh_db.list_routines(user_id) == []
    assert await fresh_db.count_user_exercises(user_id) == 0


async def test_draft_carries_scheme_and_resolved_names(fresh_db, user_id):
    _, draft = await _propose(
        user_id,
        {
            "name": "Фуллбоди",
            "days": [_day("День 1", [{"name": TEMPLATE_A, "sets": 4, "reps_min": 5, "reps_max": 10}])],
        },
    )

    item = draft["days"][0]["items"][0]
    assert item["name"] == TEMPLATE_A
    assert item["target"] == "4×5–10"
    assert item["source"] == "template"


async def test_users_own_exercise_is_matched_before_the_catalog(fresh_db, user_id):
    groups = await fresh_db.list_muscle_groups(None, global_only=True)
    gid = next(g["id"] for g in groups)
    await fresh_db.create_exercise(user_id, "pull down", gid)

    _, draft = await _propose(
        user_id, {"name": "P", "days": [_day("День 1", [{"name": "pull down"}])]}
    )

    item = draft["days"][0]["items"][0]
    assert item["source"] == "own"
    assert item["name"] == "pull down"


# ---------- нерезолвящиеся названия ----------

async def test_unknown_exercise_is_reported_not_silently_dropped(fresh_db, user_id):
    payload, draft = await _propose(
        user_id,
        {
            "name": "Фуллбоди",
            "days": [_day("День 1", [{"name": TEMPLATE_A}, {"name": "Жим Арнольда на босу"}])],
        },
    )

    assert payload["days"][0]["unresolved"] == ["Жим Арнольда на босу"]
    assert payload["days"][0]["resolved"] == [TEMPLATE_A]
    assert [i["name"] for i in draft["days"][0]["items"]] == [TEMPLATE_A]


async def test_nothing_resolves_means_no_draft_and_an_error_for_the_model(fresh_db, user_id):
    payload, draft = await _propose(
        user_id, {"name": "П", "days": [_day("День 1", [{"name": "Полная выдумка"}])]}
    )

    assert draft is None
    assert payload["shown_to_user"] is False
    assert "error" in payload


async def test_empty_days_is_an_error(fresh_db, user_id):
    payload, draft = await _propose(user_id, {"name": "П", "days": []})

    assert draft is None
    assert "error" in payload


# ---------- валидация и лимиты ----------

async def test_days_over_the_limit_are_cut_and_the_model_is_told(fresh_db, user_id):
    days = [_day(f"День {i}", [{"name": TEMPLATE_A}]) for i in range(ai_trainer.PROGRAM_MAX_DAYS + 2)]

    payload, draft = await _propose(user_id, {"name": "П", "days": days})

    assert len(draft["days"]) == ai_trainer.PROGRAM_MAX_DAYS
    assert "truncated_days" in payload


async def test_exercises_over_the_limit_are_cut_within_a_day(fresh_db, user_id):
    exercises = [{"name": TEMPLATE_A}] * (ai_trainer.PROGRAM_MAX_EXERCISES_PER_DAY + 3)

    payload, _ = await _propose(user_id, {"name": "П", "days": [_day("День 1", exercises)]})

    assert payload["days"][0]["truncated_exercises"] is True


async def test_duplicate_exercise_in_a_day_is_collapsed(fresh_db, user_id):
    _, draft = await _propose(
        user_id,
        {"name": "П", "days": [_day("День 1", [{"name": TEMPLATE_A}, {"name": TEMPLATE_A}])]},
    )

    assert len(draft["days"][0]["items"]) == 1


async def test_reversed_rep_range_is_turned_around(fresh_db, user_id):
    _, draft = await _propose(
        user_id,
        {"name": "П", "days": [_day("День 1", [{"name": TEMPLATE_A, "reps_min": 10, "reps_max": 5}])]},
    )

    assert draft["days"][0]["items"][0]["target"] == "5–10"


async def test_absurd_sets_are_clamped_and_garbage_becomes_none(fresh_db, user_id):
    _, draft = await _propose(
        user_id,
        {
            "name": "П",
            "days": [
                _day("День 1", [{"name": TEMPLATE_A, "sets": 999, "reps_min": "много"}]),
            ],
        },
    )

    item = draft["days"][0]["items"][0]
    assert item["sets"] == ai_trainer.PROGRAM_MAX_SETS
    assert item["reps_min"] is None
    assert item["target"] == f"{ai_trainer.PROGRAM_MAX_SETS} подх."


async def test_long_names_are_trimmed_and_empty_ones_get_a_fallback(fresh_db, user_id):
    _, draft = await _propose(
        user_id, {"name": "Я" * 200, "days": [_day("", [{"name": TEMPLATE_A}])]}
    )

    assert len(draft["name"]) == ai_trainer.PROGRAM_NAME_LIMIT
    assert draft["days"][0]["name"] == "День 1"


async def test_exercise_given_as_a_bare_string_still_resolves(fresh_db, user_id):
    """Модель нет-нет да пришлёт список строк вместо объектов — не терять же из-за этого программу."""
    _, draft = await _propose(user_id, {"name": "П", "days": [_day("День 1", [TEMPLATE_A])]})

    assert [i["name"] for i in draft["days"][0]["items"]] == [TEMPLATE_A]


async def test_warns_the_model_when_the_routine_cap_would_overflow(fresh_db, user_id):
    for i in range(ai_trainer.MAX_ROUTINES_PER_USER):
        await fresh_db.create_routine(user_id, f"Программа {i}")

    payload, _ = await _propose(
        user_id, {"name": "П", "days": [_day("День 1", [{"name": TEMPLATE_A}])]}
    )

    assert "warning" in payload


# ---------- сохранение ----------

async def test_saving_a_draft_creates_one_routine_per_day_with_scheme(fresh_db, user_id):
    _, draft = await _propose(
        user_id,
        {
            "name": "Верх/низ",
            "days": [
                _day("День 1 — верх", [{"name": TEMPLATE_A, "sets": 3, "reps_min": 5, "reps_max": 10}]),
                _day("День 2 — низ", [{"name": TEMPLATE_B, "sets": 4, "reps_min": 6, "reps_max": 8}]),
            ],
        },
    )

    for day in draft["days"]:
        await fresh_db.create_routine_from_program(
            user_id, day["name"], [(i["name"], i["target"]) for i in day["items"]]
        )

    routines = await fresh_db.list_routines(user_id)
    assert sorted(r["name"] for r in routines) == ["День 1 — верх", "День 2 — низ"]

    upper = next(r for r in routines if r["name"] == "День 1 — верх")
    rows = await fresh_db.list_routine_exercises(upper["id"])
    assert rows[0]["display_name"] == TEMPLATE_A
    assert rows[0]["target"] == "3×5–10"


async def test_saving_forks_catalog_exercises_into_the_users_list(fresh_db, user_id):
    _, draft = await _propose(
        user_id, {"name": "П", "days": [_day("День 1", [{"name": TEMPLATE_A}])]}
    )
    assert await fresh_db.count_user_exercises(user_id) == 0

    await fresh_db.create_routine_from_program(
        user_id, "День 1", [(i["name"], i["target"]) for i in draft["days"][0]["items"]]
    )

    assert await fresh_db.find_exercise_by_name(user_id, TEMPLATE_A) is not None


async def test_a_program_without_a_scheme_leaves_the_target_empty(fresh_db, user_id):
    """Программа, снятая с тренировки, схемы не несёт — и не выдумывает её."""
    rid = await fresh_db.create_routine_from_program(user_id, "День 1", [TEMPLATE_A])

    assert (await fresh_db.list_routine_exercises(rid))[0]["target"] is None


async def test_ready_made_program_still_instantiates(fresh_db, user_id):
    rid = await fresh_db.create_routine_from_program(
        user_id, "Всё тело", [TEMPLATE_A, TEMPLATE_B, TEMPLATE_A]
    )

    rows = await fresh_db.list_routine_exercises(rid)
    assert [r["display_name"] for r in rows] == [TEMPLATE_A, TEMPLATE_B]


# ---------- превью и клавиатуры ----------

def test_preview_lists_days_scheme_and_new_exercises():
    text = formatting.build_ai_program_preview(
        "Верх/низ",
        [
            {
                "name": "День 1",
                "items": [
                    {"name": "Жим лёжа", "target": "3×5–10", "source": "template"},
                    {"name": "Тяга", "target": "3×8", "source": "own"},
                ],
            },
            {"name": "День 2", "items": [{"name": "Присед", "target": "4×5–10", "source": "own"}]},
        ],
    )

    assert "Верх/низ" in text
    assert "2 дня · 3 упражнения" in text
    assert "1. Жим лёжа — 3×5–10" in text
    assert "2. Тяга — 3×8" in text
    assert "Новых для тебя упражнение: 1" in text


def test_preview_escapes_html_in_names():
    text = formatting.build_ai_program_preview(
        "<b>злое</b>", [{"name": "День <1>", "items": [{"name": "Жим & тяга", "source": "own"}]}]
    )

    assert "&lt;b&gt;злое&lt;/b&gt;" in text
    assert "Жим &amp; тяга" in text


def test_preview_of_a_single_day_does_not_promise_several_programs():
    text = formatting.build_ai_program_preview(
        "Один день", [{"name": "День 1", "items": [{"name": "Жим", "source": "own"}]}]
    )

    assert "Добавлю как программу" in text


def test_target_formats_partial_input():
    assert formatting.build_routine_target(3, 5, 10) == "3×5–10"
    assert formatting.build_routine_target(3, 8, 8) == "3×8"
    assert formatting.build_routine_target(3, None, None) == "3 подх."
    assert formatting.build_routine_target(None, 5, 10) == "5–10"
    assert formatting.build_routine_target(None, None, None) == ""


def test_program_button_appears_only_when_a_program_was_proposed():
    without = keyboards.ai_trainer_keyboard(program_name=None)
    assert not any(
        b.callback_data == "ai:prog:view" for row in without.inline_keyboard for b in row
    )

    with_program = keyboards.ai_trainer_keyboard(program_name="Верх/низ")
    top = with_program.inline_keyboard[0][0]
    assert top.callback_data == "ai:prog:view"
    assert "Верх/низ" in top.text
