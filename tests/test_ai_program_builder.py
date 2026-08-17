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


async def test_a_day_with_nothing_resolved_is_reported_as_dropped(fresh_db, user_id):
    """A10: a day whose exercises all fail to resolve used to just vanish from
    `days` while `report` still listed it with resolved: [] and the payload
    kept claiming shown_to_user=True with the generic note — the model then
    described (say) a 3-day split while the preview showed only 2. Now the
    model is told explicitly which day was dropped and why."""
    payload, draft = await _propose(
        user_id,
        {
            "name": "Сплит",
            "days": [
                _day("День 1", [{"name": TEMPLATE_A}]),
                _day("День 2 — руки", [{"name": "Полная выдумка про бицепс"}]),
            ],
        },
    )

    assert len(draft["days"]) == 1  # день 2 не попал в показанное превью
    assert "dropped_days" in payload
    assert payload["dropped_days"] == [
        {"day": "День 2 — руки", "unresolved": ["Полная выдумка про бицепс"]}
    ]
    assert "dropped_days_note" in payload


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
    payload, draft = await _propose(
        user_id, {"name": "Я" * 200, "days": [_day("", [{"name": TEMPLATE_A}])]}
    )

    assert len(draft["name"]) == ai_trainer.PROGRAM_NAME_LIMIT
    assert draft["days"][0]["name"] == "День 1"
    # A11: truncation used to be completely silent — only truncated_days /
    # truncated_exercises (which are about something else entirely) existed.
    assert "program_name_truncated" in payload


async def test_clamped_sets_and_reps_are_reported_to_the_model(fresh_db, user_id):
    """A11: sets/reps used to be clamped (25→10, 100-200→50-50) with no trace
    beyond the resulting target string ("10×50") looking like an honest
    proposal — the model had no way to notice and mention it."""
    payload, _ = await _propose(
        user_id,
        {
            "name": "П",
            "days": [
                _day(
                    "День 1",
                    [{"name": TEMPLATE_A, "sets": 25, "reps_min": 100, "reps_max": 200}],
                )
            ],
        },
    )

    assert "clamped" in payload["days"][0]
    (note,) = payload["days"][0]["clamped"]
    assert TEMPLATE_A in note and "25" in note and "10" in note and "100" in note and "50" in note


def test_propose_program_schema_declares_real_limits():
    """A11: the limits used to live only in prose descriptions ("Рабочих
    подходов, 1-10"), which a model can and does ignore — they now also exist
    as real JSON-schema constraints the API itself can enforce."""
    (tool,) = [t for t in ai_trainer.TOOLS if t["function"]["name"] == "propose_program"]
    props = tool["function"]["parameters"]["properties"]

    assert props["name"]["maxLength"] == ai_trainer.PROGRAM_NAME_LIMIT
    days_schema = props["days"]
    assert days_schema["minItems"] == 1
    assert days_schema["maxItems"] == ai_trainer.PROGRAM_MAX_DAYS
    day_props = days_schema["items"]["properties"]
    assert day_props["name"]["maxLength"] == ai_trainer.PROGRAM_NAME_LIMIT
    ex_schema = day_props["exercises"]
    assert ex_schema["minItems"] == 1
    assert ex_schema["maxItems"] == ai_trainer.PROGRAM_MAX_EXERCISES_PER_DAY
    ex_props = ex_schema["items"]["properties"]
    assert ex_props["sets"]["minimum"] == 1
    assert ex_props["sets"]["maximum"] == ai_trainer.PROGRAM_MAX_SETS
    assert ex_props["reps_min"]["maximum"] == ai_trainer.PROGRAM_MAX_REPS
    assert ex_props["reps_max"]["maximum"] == ai_trainer.PROGRAM_MAX_REPS


async def test_exercise_given_as_a_bare_string_still_resolves(fresh_db, user_id):
    """Модель нет-нет да пришлёт список строк вместо объектов — не терять же из-за этого программу."""
    _, draft = await _propose(user_id, {"name": "П", "days": [_day("День 1", [TEMPLATE_A])]})

    assert [i["name"] for i in draft["days"][0]["items"]] == [TEMPLATE_A]


# ---------- прогрессия (5.6/3.2) ----------


async def test_progression_schema_declares_the_closed_rule_set():
    (tool,) = [t for t in ai_trainer.TOOLS if t["function"]["name"] == "propose_program"]
    ex_props = tool["function"]["parameters"]["properties"]["days"]["items"]["properties"][
        "exercises"
    ]["items"]["properties"]
    prog_props = ex_props["progression"]["properties"]
    assert set(prog_props["rule"]["enum"]) == set(ai_trainer.PROGRESSION_RULES)
    assert prog_props["step"]["maximum"] == ai_trainer.PROGRESSION_MAX_STEP


async def test_progression_is_carried_through_to_the_draft(fresh_db, user_id):
    _, draft = await _propose(
        user_id,
        {
            "name": "П",
            "days": [
                _day(
                    "День 1",
                    [
                        {
                            "name": TEMPLATE_A, "sets": 3, "reps_min": 5, "reps_max": 8,
                            "progression": {"rule": "double_progression", "reps_top": 8, "step": 2.5},
                        }
                    ],
                )
            ],
        },
    )

    item = draft["days"][0]["items"][0]
    assert item["progression"] == {"rule": "double_progression", "reps_top": 8, "step": 2.5}


async def test_progression_with_an_unknown_rule_is_dropped_not_the_exercise(fresh_db, user_id):
    """Мусорное правило не должно стоить упражнения целиком — прогрессия не то
    же самое, что sets/reps, клампить её в разумный дефолт нечем."""
    _, draft = await _propose(
        user_id,
        {
            "name": "П",
            "days": [_day("День 1", [{"name": TEMPLATE_A, "progression": {"rule": "выдумка"}}])],
        },
    )

    item = draft["days"][0]["items"][0]
    assert item["name"] == TEMPLATE_A
    assert item["progression"] is None


async def test_progression_step_is_clamped_to_a_sane_range(fresh_db, user_id):
    _, draft = await _propose(
        user_id,
        {
            "name": "П",
            "days": [
                _day(
                    "День 1",
                    [{"name": TEMPLATE_A, "progression": {"rule": "linear_load", "step": 999}}],
                )
            ],
        },
    )

    assert draft["days"][0]["items"][0]["progression"]["step"] == ai_trainer.PROGRESSION_MAX_STEP


async def test_reps_top_out_of_sync_with_the_range_is_snapped_to_reps_max(fresh_db, user_id):
    """reps_top живёт отдельным полем от схемы, и модель их рассинхронизирует:
    target 5-10 при reps_top=8 значит, что вес добавится раньше, чем человек
    дошёл до верха своего диапазона. Верх диапазона — единственное осмысленное
    значение, и подмена о себе сообщает через clamped."""
    payload, draft = await _propose(
        user_id,
        {
            "name": "П",
            "days": [
                _day(
                    "День 1",
                    [
                        {
                            "name": TEMPLATE_A, "sets": 3, "reps_min": 5, "reps_max": 10,
                            "progression": {"rule": "double_progression", "reps_top": 8, "step": 2.5},
                        }
                    ],
                )
            ],
        },
    )

    assert draft["days"][0]["items"][0]["progression"]["reps_top"] == 10
    (note,) = payload["days"][0]["clamped"]
    assert "reps_top" in note and "8" in note and "10" in note


async def test_double_progression_without_reps_top_gets_the_exercises_reps_max(fresh_db, user_id):
    """Без reps_top правило валидно, но подсказка веса падала на глобальный
    REP_RANGE_MAX (10) — на упражнении с диапазоном до 8 она молчала бы вечно."""
    payload, draft = await _propose(
        user_id,
        {
            "name": "П",
            "days": [
                _day(
                    "День 1",
                    [
                        {
                            "name": TEMPLATE_A, "sets": 3, "reps_min": 5, "reps_max": 8,
                            "progression": {"rule": "double_progression", "step": 2.5},
                        }
                    ],
                )
            ],
        },
    )

    assert draft["days"][0]["items"][0]["progression"]["reps_top"] == 8
    (note,) = payload["days"][0]["clamped"]
    assert "reps_top" in note


async def test_double_progression_without_a_range_is_left_untouched(fresh_db, user_id):
    """Схемы нет — подставлять нечего: правило остаётся как прислано и живёт на
    глобальном дефолте, это честнее, чем выдумывать диапазон."""
    _, draft = await _propose(
        user_id,
        {
            "name": "П",
            "days": [
                _day(
                    "День 1",
                    [{"name": TEMPLATE_A, "progression": {"rule": "double_progression"}}],
                )
            ],
        },
    )

    assert draft["days"][0]["items"][0]["progression"] == {"rule": "double_progression"}


async def test_linear_load_without_a_step_is_dropped_and_reported(fresh_db, user_id):
    """linear_load без step проходил валидацию и молча деградировал в дефолт —
    неработающее правило хуже честного «без прогрессии» с пометкой модели."""
    payload, draft = await _propose(
        user_id,
        {
            "name": "П",
            "days": [
                _day("День 1", [{"name": TEMPLATE_A, "progression": {"rule": "linear_load"}}])
            ],
        },
    )

    assert draft["days"][0]["items"][0]["progression"] is None
    (note,) = payload["days"][0]["clamped"]
    assert "linear_load" in note and "step" in note


async def test_saving_a_draft_persists_the_progression_rule(fresh_db, user_id, monkeypatch):
    """5.6: правило должно пережить сохранение — иначе оно, как и раньше,
    существует только в прозе ответа и теряется вместе с чатом."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from handlers import ai_trainer as handler

    _, draft = await _propose(
        user_id,
        {
            "name": "П",
            "days": [
                _day(
                    "День 1",
                    [
                        {
                            "name": TEMPLATE_A,
                            "progression": {"rule": "double_progression", "reps_top": 8, "step": 2.5},
                        }
                    ],
                )
            ],
        },
    )
    draft["id"] = 1
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    state = FSMContext(storage=MemoryStorage(), key=key)
    await state.update_data(ai_program_draft=draft)

    message = MagicMock()
    message.edit_text = AsyncMock()
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    callback.message = message
    callback.data = "ai:prog:save:1"
    callback.answer = AsyncMock()

    await handler.ai_program_save(callback, state)

    (routine,) = await fresh_db.list_routines(user_id)
    (row,) = await fresh_db.list_routine_exercises(routine["id"])
    assert json.loads(row["progression"]) == {"rule": "double_progression", "reps_top": 8, "step": 2.5}


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
    # A13: родительный падеж множественного числа фиксирован («Новых для тебя
    # упражнений»), а не согласуется с числом через plural_ru — «упражнение: 1»
    # читалось бы как согласование с количеством, а не с «для тебя».
    assert "Новых для тебя упражнений: 1" in text


def test_preview_escapes_html_in_names():
    text = formatting.build_ai_program_preview(
        "<b>злое</b>", [{"name": "День <1>", "items": [{"name": "Жим & тяга", "source": "own"}]}]
    )

    assert "&lt;b&gt;злое&lt;/b&gt;" in text
    assert "Жим &amp; тяга" in text


def test_preview_surfaces_notes_the_model_would_otherwise_keep_to_itself():
    """5.3: обрезки/потери раньше уходили только модели JSON-полем — если она
    забывала упомянуть их в ответе, пользователь не узнавал, что попросил семь
    дней, а получил шесть."""
    text = formatting.build_ai_program_preview(
        "П",
        [{"name": "День 1", "items": [{"name": "Жим", "source": "own"}]}],
        notes=["Дней получилось больше 6 — показал только первые 6.", "Не нашёл в боте: гакк-приседания."],
    )

    assert "На заметку" in text
    assert "Дней получилось больше 6" in text
    assert "гакк-приседания" in text


def test_preview_shows_the_progression_rule_next_to_the_exercise():
    text = formatting.build_ai_program_preview(
        "П",
        [
            {
                "name": "День 1",
                "items": [
                    {
                        "name": "Жим лёжа", "target": "3×5–8", "source": "own",
                        "progression": {"rule": "double_progression", "reps_top": 8, "step": 2.5},
                    }
                ],
            }
        ],
    )

    assert "дошёл до 8 повторов — прибавь 2.5" in text


def test_preview_of_a_single_day_does_not_promise_several_programs():
    text = formatting.build_ai_program_preview(
        "Один день", [{"name": "День 1", "items": [{"name": "Жим", "source": "own"}]}]
    )

    assert "Добавлю как программу" in text


def test_preview_uses_genitive_case_after_the_preposition():
    """Шапка «3 дня · N упражнений» верна в именительном, но та же переменная
    day_word раньше переиспользовалась после предлога «из», где нужен
    родительный: «из 3 дня» вместо «из 3 дней». Ломалось на 2–4 днях — самом
    частом сплите."""
    days = [
        {"name": f"День {i}", "items": [{"name": "Жим", "source": "own"}]} for i in range(1, 4)
    ]
    text = formatting.build_ai_program_preview("Сплит", days)

    assert "3 дня · 3 упражнения" in text
    assert "из 3 дней —" in text
    assert "из 3 дня —" not in text


def test_target_formats_partial_input():
    assert formatting.build_routine_target(3, 5, 10) == "3×5–10"
    assert formatting.build_routine_target(3, 8, 8) == "3×8"
    assert formatting.build_routine_target(3, None, None) == "3 подх."
    assert formatting.build_routine_target(None, 5, 10) == "5–10"
    assert formatting.build_routine_target(None, None, None) == ""


def test_program_button_appears_only_when_a_program_was_proposed():
    without = keyboards.ai_trainer_keyboard(program_name=None)
    assert not any(
        b.callback_data.startswith("ai:prog:view") for row in without.inline_keyboard for b in row
    )

    with_program = keyboards.ai_trainer_keyboard(program_name="Верх/низ", draft_id=7)
    top = with_program.inline_keyboard[0][0]
    assert top.callback_data == "ai:prog:view:7"
    # 5.1: подпись — предложение забрать программу, а не голое название (иначе
    # неотличимо от кнопки навигации «открыть программу»).
    assert "Верх/низ" in top.text
    assert "Забрать" in top.text
    assert "🗂" in top.text


def test_program_button_shares_the_mention_page_limit():
    """The program shares AI_MENTION_PAGE_SIZE with mentioned exercises rather
    than always occupying an extra row above the limit."""
    exercises = [
        {"id": i, "is_template": False, "display_name": f"Упражнение {i}"} for i in range(1, 5)
    ]
    kb = keyboards.ai_trainer_keyboard(exercises=exercises, program_name="Верх/низ", draft_id=3)
    item_rows = kb.inline_keyboard[: keyboards.AI_MENTION_PAGE_SIZE]
    assert len(item_rows) == keyboards.AI_MENTION_PAGE_SIZE
    assert item_rows[0][0].callback_data == "ai:prog:view:3"
    assert item_rows[1][0].callback_data == "ai:excard:1"
    assert item_rows[2][0].callback_data == "ai:excard:2"
    # Одно упражнение не влезло на первую страницу из-за программы — есть стрелка дальше.
    nav_row = kb.inline_keyboard[keyboards.AI_MENTION_PAGE_SIZE]
    assert any(b.callback_data.startswith("ai:mpage:1:") for b in nav_row)


# ---------- правка уже сохранённой программы ----------


async def _saved_two_day_program(db, user_id: int) -> None:
    """Сохранённая программа «Верх/низ» из двух дней — как после кнопки «Добавить»."""
    await db.create_routine_from_program(user_id, "Низ", [(TEMPLATE_B, "3×5")], program_name="Верх/низ")
    await db.create_routine_from_program(user_id, "Верх", [(TEMPLATE_A, "3×8")], program_name="Верх/низ")


async def test_saved_programs_tool_shows_days_and_composition(fresh_db, user_id):
    """Без этого инструмента тренеру нечем править: он не видит, что у человека
    сохранено и из чего оно состоит."""
    await _saved_two_day_program(fresh_db, user_id)

    payload = json.loads(await ai_trainer.execute_tool(user_id, "get_saved_programs", {}))

    (program,) = payload["programs"]
    assert program["name"] == "Верх/низ"
    assert [d["name"] for d in program["days"]] == ["Низ", "Верх"]
    assert program["days"][0]["exercises"] == [
        {"name": TEMPLATE_B, "target": "3×5", "progression": None}
    ]


async def test_saved_programs_tool_shows_the_stored_progression_rule(fresh_db, user_id):
    """Правка идёт через propose_program «программа ЦЕЛИКОМ» — пока модель не
    видела сохранённые правила прогрессии, ей нечем было их перенести, и правка
    одного упражнения молча стирала прогрессию всех остальных."""
    db_ = fresh_db
    group_id = await db_.create_muscle_group(user_id, "Грудь")
    ex_id = await db_.create_exercise(user_id, "Жим лёжа", group_id)
    program_id = await db_.create_program(user_id, "Верх/низ")
    day = await db_.create_routine(user_id, "Верх", program_id=program_id)
    await db_.add_routine_exercise(day, ex_id, 0, "3×5–10")
    entry = (await db_.list_routine_exercises(day))[0]
    await db_.set_routine_exercise_progression(
        entry["id"], json.dumps({"rule": "double_progression", "reps_top": 10, "step": 2.5})
    )

    payload = json.loads(await ai_trainer.execute_tool(user_id, "get_saved_programs", {}))

    (program,) = payload["programs"]
    (exercise,) = program["days"][0]["exercises"]
    assert exercise["progression"] == {
        "rule": "double_progression", "reps_top": 10, "step": 2.5,
    }


async def test_editing_marks_the_draft_as_replacing_the_saved_program(fresh_db, user_id):
    await _saved_two_day_program(fresh_db, user_id)
    before = {r["id"] for r in await fresh_db.list_routines(user_id)}

    payload, draft = await _propose(
        user_id,
        {
            "name": "Верх/низ",
            "replaces_program": "Верх/низ",
            "days": [_day("Низ", [{"name": TEMPLATE_B, "sets": 4, "reps_min": 6, "reps_max": 8}])],
        },
    )

    assert payload["replaces_program"] == "Верх/низ"
    assert set(draft["replaces"]["routine_ids"]) == before
    # Инвариант всей фичи держится и для правки: до тапа ничего не изменилось.
    assert {r["id"] for r in await fresh_db.list_routines(user_id)} == before


async def test_proposal_carries_the_description_into_the_draft(fresh_db, user_id):
    """Описание едет в черновике и ложится в programs.description при
    сохранении — иначе экран программы остаётся списком упражнений."""
    _, draft = await _propose(
        user_id,
        {
            "name": "Верх/низ",
            "description": "  Два дня\n\nна всё тело.  ",
            "days": [_day("Низ", [{"name": TEMPLATE_B}])],
        },
    )

    # Заодно схлопнутые переносы: описание живёт одной строкой над списком дней.
    assert draft["description"] == "Два дня на всё тело."


async def test_proposal_without_a_description_still_reaches_the_user(fresh_db, user_id):
    """description обязательно в схеме, но обязательность у модели — не
    гарантия. Пропущенная пара фраз не должна стоить человеку всей программы:
    превью показываем, а модели говорим, чего не хватает."""
    payload, draft = await _propose(
        user_id, {"name": "Верх/низ", "days": [_day("Низ", [{"name": TEMPLATE_B}])]}
    )

    assert draft["description"] is None
    assert payload["shown_to_user"] is True
    assert "description_missing" in payload


def test_program_proposal_requires_a_description():
    """Программа без пары фраз о себе — это экран из одних списков упражнений."""
    (tool,) = [t for t in ai_trainer.TOOLS if t["function"]["name"] == "propose_program"]
    assert "description" in tool["function"]["parameters"]["required"]


async def test_unknown_program_name_falls_back_to_a_new_program(fresh_db, user_id):
    """Промах по имени не должен молча снести что-то похожее — правка просто
    становится новым предложением, а модели говорят, что имя не найдено."""
    await _saved_two_day_program(fresh_db, user_id)

    payload, draft = await _propose(
        user_id,
        {
            "name": "Верх/низ",
            "replaces_program": "Верх-низ",  # почти то же имя, но не то
            "days": [_day("Низ", [{"name": TEMPLATE_B}])],
        },
    )

    assert draft["replaces"] is None
    assert "replaces_program" not in payload
    assert "replaces_program_error" in payload


async def test_program_name_matches_case_insensitively(fresh_db, user_id):
    await _saved_two_day_program(fresh_db, user_id)

    _, draft = await _propose(
        user_id,
        {
            "name": "Верх/низ",
            "replaces_program": "  верх/НИЗ  ",
            "days": [_day("Низ", [{"name": TEMPLATE_B}])],
        },
    )

    assert draft["replaces"]["name"] == "Верх/низ"


async def test_saved_programs_tool_marks_multi_day_and_standalone_kind(fresh_db, user_id):
    """5.4: без пометки kind список программ и одиночных routines был плоским —
    при совпадении имён между ними резолвер мог поправить не ту, на которую
    рассчитывал пользователь."""
    await _saved_two_day_program(fresh_db, user_id)
    await fresh_db.create_routine(user_id, "Одиночная")

    payload = json.loads(await ai_trainer.execute_tool(user_id, "get_saved_programs", {}))

    kinds = {p["name"]: p["kind"] for p in payload["programs"]}
    assert kinds["Верх/низ"] == "program"
    assert kinds["Одиночная"] == "routine"


async def test_replaces_program_resolves_case_folded_cyrillic_names_correctly(fresh_db, user_id):
    """A4: две программы, различающиеся только регистром («Сплит»/«сплит»), —
    раньше резолвер лоуеркейсил имена вручную поверх db.list_programs (обычной
    группировки по строке), а Python str.lower() и старое SQL-сравнение
    расходились ровно на не-ASCII буквах — «Сплит» и «сплит» были одной
    программой для резолвера и разными для БД, и промах удалял не ту, о которой
    просил пользователь. db.find_program_by_name использует ту же Unicode-
    свёртку (name_key), что и уникальный индекс программ, так что резолвится
    именно та программа, что и должна."""
    upper_id = await fresh_db.create_program(user_id, "Сплит")
    await fresh_db.create_routine(user_id, "Старый день", program_id=upper_id)

    payload, draft = await _propose(
        user_id,
        {
            "name": "Сплит",
            "replaces_program": "Сплит",
            "days": [_day("Новый день", [{"name": TEMPLATE_A}])],
        },
    )

    assert payload["replaces_program"] == "Сплит"
    assert draft["replaces"]["kind"] == "program"
    assert draft["replaces"]["id"] == upper_id


def test_preview_spells_out_that_the_edit_replaces_the_old_version():
    days = [{"name": "Низ", "items": [{"name": TEMPLATE_B, "target": "4×6–8", "source": "own"}]}]
    replaces = {"name": "Верх/низ", "days": [{"name": "Низ", "items": [{"name": TEMPLATE_B, "target": "3×5"}]}]}

    text = formatting.build_ai_program_preview("Верх/низ", days, replaces=replaces)

    assert "ПРАВКА ПРОГРАММЫ" in text
    assert "Заменю" in text and "Верх/низ" in text
    # Без replaces — прежний текст про добавление, ничего не поехало.
    assert "Добавлю" in formatting.build_ai_program_preview("Верх/низ", days)


# ---------- «что меняется» в превью правки ----------


def _old_day(name: str, items: list[tuple[str, str | None]]) -> dict:
    return {"name": name, "items": [{"name": n, "target": t} for n, t in items]}


def _new_day(name: str, items: list[tuple[str, str | None]]) -> dict:
    return {"name": name, "items": [{"name": n, "target": t, "source": "own"} for n, t in items]}


def test_changes_block_names_what_came_went_and_changed():
    """Без разницы со старой версией человеку пришлось бы сличать два списка по
    десятку упражнений глазами."""
    old = [_old_day("Ноги", [("Присед", "3×5"), ("Сведение ног", "3×12")])]
    new = [_new_day("Ноги", [("Присед", "4×6–8"), ("Ягодичный мостик", "3×10")])]

    lines = formatting.build_program_changes(old, new)

    assert any("Присед" in ln and "3×5" in ln and "4×6–8" in ln for ln in lines)  # схема
    assert any("➕" in ln and "Ягодичный мостик" in ln for ln in lines)           # пришло
    assert any("➖" in ln and "Сведение ног" in ln for ln in lines)               # ушло


def test_changes_block_reports_whole_days():
    old = [_old_day("Ноги", [("Присед", "3×5")]), _old_day("Верх", [("Жим", "3×8")])]
    new = [_new_day("Ноги", [("Присед", "3×5")]), _new_day("Пресс", [("Скручивания", "3×15")])]

    lines = formatting.build_program_changes(old, new)

    assert any("➕" in ln and "Пресс" in ln for ln in lines)
    assert any("➖" in ln and "Верх" in ln for ln in lines)
    # Нетронутый день в разницу не лезет — иначе блок перестаёт быть разницей.
    assert not any(ln.strip().startswith("<b>Ноги") for ln in lines)


def test_identical_composition_says_so_instead_of_an_empty_block():
    day_old = [_old_day("Ноги", [("Присед", "3×5")])]
    day_new = [_new_day("Ноги", [("Присед", "3×5")])]

    assert formatting.build_program_changes(day_old, day_new) == []

    text = formatting.build_ai_program_preview(
        "Сплит", day_new, replaces={"name": "Сплит", "days": day_old}
    )
    assert "Состав тот же" in text


def test_preview_of_a_huge_edit_still_fits_into_one_telegram_message():
    """Шесть дней по двенадцать упражнений — потолки propose_program: без
    обрезки состава сообщение не отправилось бы вообще."""
    long_name = "Разгибание ног в тренажёре сидя широким хватом"
    old = [
        _old_day(f"День {d} — длинное название", [(f"{long_name} {i}", "3×8–12") for i in range(12)])
        for d in range(1, 7)
    ]
    new = [
        _new_day(f"Новый день {d} — длинное", [(f"{long_name} нов {i}", "4×6–8") for i in range(12)])
        for d in range(1, 7)
    ]

    text = formatting.build_ai_program_preview("Программа", new, replaces={"name": "Программа", "days": old})

    assert formatting.telegram_length(text) <= formatting.MESSAGE_LIMIT
    assert "…и ещё" in text
    # Резать можно только состав: и разница, и предупреждение о замене на месте.
    assert "Что меняется" in text
    assert "Заменю" in text


def test_preview_of_an_edit_that_keeps_day_names_still_fits_into_one_message():
    """A1: the previous huge-edit test renamed every day, which collapses the
    whole diff to a handful of "new day"/"removed day" lines and hides the
    real bug — when day names are KEPT and every exercise inside changes, each
    day contributes its own full add/remove block (~2 lines per exercise), and
    with real catalog-length names that block alone measured ~5300 chars for a
    6-day×12-exercise replacement — build_ai_program_preview only ever
    budgeted for trimming `composition`, never for the changes block sitting
    right next to it, so the budget went negative and nothing was sent at all.
    """
    long_name = "Разгибание ног в тренажёре сидя широким хватом"
    old = [
        _old_day(f"День {d}", [(f"{long_name} {i}", "3×8–12") for i in range(12)])
        for d in range(1, 7)
    ]
    new = [
        _new_day(f"День {d}", [(f"{long_name} нов {i}", "4×6–8") for i in range(12)])
        for d in range(1, 7)
    ]

    text = formatting.build_ai_program_preview("Программа", new, replaces={"name": "Программа", "days": old})

    assert formatting.telegram_length(text) <= formatting.MESSAGE_LIMIT
    assert "Что меняется" in text
    assert "Заменю" in text


async def test_the_edit_preview_compares_the_stored_progression_rule(fresh_db, user_id):
    """Старое правило берётся из routine_exercises.progression — без него дифф
    сравнивал бы новое правило с пустотой и всегда объявлял его изменившимся."""
    db_ = fresh_db
    group_id = await db_.create_muscle_group(user_id, "Грудь")
    ex_id = await db_.create_exercise(user_id, "Жим лёжа", group_id)
    program_id = await db_.create_program(user_id, "Верх/низ")
    day = await db_.create_routine(user_id, "Верх", program_id=program_id)
    await db_.add_routine_exercise(day, ex_id, 0, "4×8")
    entry = (await db_.list_routine_exercises(day))[0]
    await db_.set_routine_exercise_progression(
        entry["id"], json.dumps({"rule": "linear_load", "step": 2.5})
    )

    replaces, error = await ai_trainer._resolve_replaced_program(user_id, "Верх/низ")

    assert error is None
    assert replaces["days"][0]["items"][0]["progression"] == {"rule": "linear_load", "step": 2.5}


async def test_a_corrupt_stored_progression_reads_as_no_rule(fresh_db, user_id):
    db_ = fresh_db
    group_id = await db_.create_muscle_group(user_id, "Грудь")
    ex_id = await db_.create_exercise(user_id, "Жим лёжа", group_id)
    program_id = await db_.create_program(user_id, "Верх/низ")
    day = await db_.create_routine(user_id, "Верх", program_id=program_id)
    await db_.add_routine_exercise(day, ex_id, 0, "4×8")
    entry = (await db_.list_routine_exercises(day))[0]
    await db_.set_routine_exercise_progression(entry["id"], "{не json")

    replaces, _error = await ai_trainer._resolve_replaced_program(user_id, "Верх/низ")

    assert replaces["days"][0]["items"][0]["progression"] is None


# ---------- блок «На заметку» ----------

async def test_preview_with_a_huge_notes_block_still_fits_into_one_message(fresh_db, user_id):
    """Заметки растут от того, что нафантазировала модель, и раньше в обрезке
    не участвовали вовсе — приклеивались к шапке целиком.

    Шесть дней по дюжине ненайденных упражнений с длинными выдуманными
    названиями давали под шесть тысяч символов одними заметками. Превью
    переваливало за лимит Telegram, а ai_program_view отправку не страхует —
    так что тап по кнопке «📋 Программа» не показывал ровно ничего.
    """
    junk = [
        {"name": "Тяга гантели в наклоне с супинацией под углом " + str(i) * 20}
        for i in range(ai_trainer.PROGRAM_MAX_EXERCISES_PER_DAY - 1)
    ]
    days = [_day("День 1", [{"name": TEMPLATE_A}] + junk)]
    for d in range(2, 7):
        days.append(
            _day(f"День {d}", [
                {"name": f"Выпад болгарский в Смите вариант {d}-{i} " + "х" * 30}
                for i in range(ai_trainer.PROGRAM_MAX_EXERCISES_PER_DAY - 1)
            ])
        )

    _payload, draft = await _propose(user_id, {"name": "Фуллбоди", "days": days})

    text = formatting.build_ai_program_preview(
        draft["name"], draft["days"], notes=draft["notes"]
    )
    assert formatting.telegram_length(text) <= formatting.MESSAGE_LIMIT
    # Резать заметки можно, а молчать о том, что они были, — нет.
    assert "На заметку" in text
    # Состав того единственного дня, который всё-таки собрался, не должен
    # оказаться съеден заметками целиком.
    assert TEMPLATE_A in text


async def test_a_day_that_dropped_entirely_gets_one_note_not_two(fresh_db, user_id):
    """Раньше про такой день было две заметки подряд — «не нашёл в боте — …» и
    «целиком пропал» — и блок читался как повтор."""
    _payload, draft = await _propose(
        user_id,
        {
            "name": "Сплит",
            "days": [
                _day("Грудь", [{"name": TEMPLATE_A}]),
                _day("Спина", [{"name": "Тяга Кузнецова"}, {"name": "Жим Петрова"}]),
            ],
        },
    )

    about_back = [n for n in draft["notes"] if "Спина" in n]
    assert len(about_back) == 1, about_back
    # Одна строка, но обе вещи в ней: и что день не вошёл, и что именно не нашлось.
    assert "не вошёл" in about_back[0]
    assert "Тяга Кузнецова" in about_back[0]
    assert "Жим Петрова" in about_back[0]


# ---------- имена дней ----------

async def test_days_with_the_same_name_are_told_apart(fresh_db, user_id):
    """Модель нет-нет да пришлёт два «Дня 1», забыв переименовать шаблонные дни.

    Дни программы — кнопки на одном экране (keyboards.program_days_keyboard):
    два одинаковых там неразличимы, какой из них какой видно только открыв оба.
    """
    _payload, draft = await _propose(
        user_id,
        {
            "name": "Сплит",
            "days": [
                _day("День 1", [{"name": TEMPLATE_A}]),
                _day("День 1", [{"name": TEMPLATE_B}]),
                _day("день 1", [{"name": TEMPLATE_A}]),
            ],
        },
    )

    names = [d["name"] for d in draft["days"]]
    assert len(names) == 3
    assert len({n.lower() for n in names}) == 3, names
    # Разводим суффиксом, а не отбрасыванием дня: состав у них разный.
    assert names[0] == "День 1"
    assert TEMPLATE_B in [item["name"] for item in draft["days"][1]["items"]]


def test_a_disambiguated_day_name_still_respects_the_length_cap():
    taken: set[str] = set()
    long_name = "Д" * ai_trainer.PROGRAM_NAME_LIMIT
    first = ai_trainer._unique_day_name(long_name, taken)
    second = ai_trainer._unique_day_name(long_name, taken)

    assert first != second
    assert len(second) <= ai_trainer.PROGRAM_NAME_LIMIT


def test_notes_that_do_not_fit_are_counted_not_dropped_silently():
    """Обрезанный блок обязан сказать, сколько заметок не влезло, и оставить
    место составу ниже — иначе «на заметку» съедало бы весь экран."""
    notes = [
        f"«День {i}»: не нашёл в боте — "
        + ", ".join(f"Упражнение выдуманное номер {i}-{j}" for j in range(12))
        for i in range(1, 40)
    ]
    days = [_new_day("День 1", [("Жим лёжа", "4×8")])]

    text = formatting.build_ai_program_preview("Программа", days, notes=notes)

    assert formatting.telegram_length(text) <= formatting.MESSAGE_LIMIT
    assert "замечани" in text and "…и ещё" in text
    assert "Жим лёжа" in text


# ---------- «▶️ Начать по ней»: тренировка без сохранения ----------

async def test_recovery_tool_gives_the_trainer_what_the_screen_shows(fresh_db, user_id):
    """Тренер про восстановление не знал вообще — при том что «что сегодня
    качать» это первый вопрос, с которым к нему приходят, а число уже считалось
    для экрана выбора. Считаться оно должно одно и то же."""
    import datetime as dt

    import analytics
    import timeutil

    group_id = await fresh_db.create_muscle_group(user_id, "Ноги")
    ex_id = await fresh_db.create_exercise(user_id, "Присед", group_id)
    # Местный день пользователя, а не UTC: свежий атлет заводится на
    # config.DEFAULT_TZ_OFFSET (+3), и recovery считает "days_ago" по нему же —
    # иначе тест плавает возле полуночи UTC.
    day = timeutil.user_today(await fresh_db.get_user(user_id)) - dt.timedelta(days=1)
    workout_id = await fresh_db.create_workout(user_id, started_at=f"{day.isoformat()}T10:00:00")
    block_id = await fresh_db.create_block(workout_id, "single")
    await fresh_db.add_block_exercise(block_id, ex_id, 0)
    for _ in range(6):
        await fresh_db.append_set(block_id, ex_id, 0, 100, 5)
    await fresh_db.finish_workout(workout_id, finished_at=f"{day.isoformat()}T11:00:00")

    payload = json.loads(await ai_trainer.execute_tool(user_id, "get_muscle_recovery", {}))

    legs = next(row for row in payload["groups"] if row["group"] == "Ноги")
    assert legs["days_ago"] == 1
    assert legs["sets_that_session"] == 6
    assert legs["recovery_percent"] == analytics.recovery_percent(
        day, 6, timeutil.user_today(await fresh_db.get_user(user_id))
    )


async def test_never_trained_groups_are_not_passed_off_as_fully_rested(fresh_db, user_id):
    """«Свежая» и «ни разу не тронутая» — разные вещи, и вторая обычно
    интереснее: она означает пробел, а не готовность."""
    await fresh_db.create_muscle_group(user_id, "Спина")

    payload = json.loads(await ai_trainer.execute_tool(user_id, "get_muscle_recovery", {}))

    assert "Спина" in payload["never_trained"]
    assert all(row["group"] != "Спина" for row in payload["groups"])


def test_single_day_draft_can_be_trained_without_saving():
    """Разовую тренировку не надо заводить программой: сессия попадёт в
    историю, а «🔁 Повторить тренировку» перезапустит любую прошлую."""
    one_day = keyboards.ai_program_preview_keyboard(draft_id="abc", can_train_now=True)
    callbacks = [b.callback_data for row in one_day.inline_keyboard for b in row]

    assert callbacks[0] == "ai:prog:train:abc"
    assert "ai:prog:save:abc" in callbacks


def test_a_multi_day_draft_has_no_train_now_button():
    """Программу из нескольких дней «начать» нельзя — непонятно, каким днём."""
    many = keyboards.ai_program_preview_keyboard(draft_id="abc", can_train_now=False)

    assert not any(
        (b.callback_data or "").startswith("ai:prog:train:")
        for row in many.inline_keyboard for b in row
    )


def test_an_edit_of_a_saved_program_is_not_offered_as_a_workout():
    """У правки смысл тапа — обновить сохранённое, а не сходить разок."""
    edit = keyboards.ai_program_preview_keyboard(
        replacing=True, draft_id="abc", can_train_now=True
    )

    assert not any(
        (b.callback_data or "").startswith("ai:prog:train:")
        for row in edit.inline_keyboard for b in row
    )


async def test_weekly_volume_is_counted_by_code_not_by_the_model(fresh_db, user_id):
    """Регрессия: модель называла объём по своему же черновику и ошибалась —
    обещала «~12 подходов на грудь» в программе, где их 19. Теперь инструмент
    возвращает посчитанное, а промпт велит брать числа только оттуда."""
    payload, _draft = await _propose(
        user_id,
        {
            "name": "Жим 3× в неделю",
            "days": [
                _day("A", [{"name": TEMPLATE_A, "sets": 5, "reps_min": 5, "reps_max": 8}]),
                _day("B", [{"name": TEMPLATE_B, "sets": 4, "reps_min": 5, "reps_max": 10}]),
                _day("C", [{"name": TEMPLATE_A, "sets": 4, "reps_min": 6, "reps_max": 10}]),
            ],
        },
    )

    # Жим штанги лёжа стоит в двух днях: 5 + 4 подхода на грудь за неделю.
    assert payload["weekly_sets_by_group"]["Грудь"] == 9
    assert payload["weekly_sets_by_group"]["Ноги"] == 4


async def test_weekly_volume_skips_exercises_without_a_set_count(fresh_db, user_id):
    """Схема необязательна: упражнение без числа подходов не должно ни падать,
    ни завышать объём."""
    payload, _draft = await _propose(
        user_id,
        {
            "name": "Без схем",
            "days": [_day("A", [{"name": TEMPLATE_A}, {"name": TEMPLATE_B, "sets": 3}])],
        },
    )

    assert "Грудь" not in payload["weekly_sets_by_group"]
    assert payload["weekly_sets_by_group"]["Ноги"] == 3
