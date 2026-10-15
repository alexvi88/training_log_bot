"""Карточки движений и отчёты для AI-тренера (coach_facts).

Тренер не пересчитывает подходы в голове: код считает сигналы, модель
рассуждает. Здесь — что именно код ей отдаёт: живой случай с бицепсом
(77×6@9, 73×12@9, 73×10@10 на 09-28), атлет в lb, подтягивания с весом тела,
английский атлет целиком по-английски, отчёт о балансе с разминкой, становой
и программой.
"""

import datetime as dt
import json
import re

import pytest

import ai_trainer
import coach_facts
import i18n
import seed_data
import timeutil

pytestmark = pytest.mark.asyncio

TODAY = dt.date(2026, 10, 15)
CYRILLIC = re.compile(r"[А-Яа-яЁё]")


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr(timeutil, "user_today", lambda user: TODAY)


async def _group_id(db, user_id, name):
    for g in await db.list_muscle_groups(user_id):
        if g["name"] == name:
            return g["id"]
    return await db.create_muscle_group(user_id, name)


async def _session(db, user_id, ex_id, day, sets, *, note=None, ex_note=None):
    """sets — (вес, повторы) или (вес, повторы, rpe), в порядке выполнения."""
    wid = await db.create_finished_workout(
        user_id, started_at=f"{day}T10:00:00", finished_at=f"{day}T11:00:00", note=note,
    )
    block_id = await db.create_block(wid, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    for i, entry in enumerate(sets, start=1):
        rpe = entry[2] if len(entry) > 2 else None
        await db.add_set(block_id, ex_id, round_index=i, order_in_round=0,
                         weight=entry[0], reps=entry[1], rpe=rpe)
    if ex_note:
        await db.set_workout_exercise_note(wid, ex_id, ex_note)
    return wid


# Живой случай: тренер сказал «повторы на 77 упали с 12 до 7», а 12 было на 73.
_CURL = [
    ("2026-08-03", [(73, 8)], None, None),
    ("2026-08-09", [(73, 9)], None, None),
    ("2026-08-31", [(73, 10)], None, None),
    ("2026-09-14", [(73, 11)], None, None),
    ("2026-09-28", [(40, 10), (77, 6, 9), (73, 12, 9), (73, 10, 10)], None, "плечо ноет"),
    ("2026-10-05", [(73, 11)], "болел всю неделю, простыл", "плечо ноет"),
    ("2026-10-12", [(77, 7, 9), (77, 6), (73, 7)], None, "плечо ноет"),
]


_EN_NOTES = {"плечо ноет": "shoulder aches", "болел всю неделю, простыл": "sick all week, caught a cold"}


async def _seed_curl(db, user_id, name="Сгибание рук в блоке", group="Бицепс", *, english_notes=False):
    ex_id = await db.create_exercise(user_id, name, await _group_id(db, user_id, group))
    for day, sets, note, ex_note in _CURL:
        if english_notes:
            note, ex_note = _EN_NOTES.get(note, note), _EN_NOTES.get(ex_note, ex_note)
        await _session(db, user_id, ex_id, day, sets, note=note, ex_note=ex_note)
    return ex_id


async def test_biceps_card_compares_reps_only_at_one_weight(fresh_db, user_id):
    await _seed_curl(fresh_db, user_id)

    payload = json.loads(await ai_trainer.execute_tool(
        user_id, "get_exercise_progress", {"exercise_name": "Сгибание рук в блоке"}))
    card = payload["card"]
    lines = card.split("\n")

    assert lines[0].startswith("Сгибание рук в блоке · Бицепс · ")
    assert "последний раз 10-12 (3 дня назад)" in lines[0]
    assert "(перешёл на новый вес)" in lines[0]
    # Рабочий вес с датой и повторы ТОЛЬКО на нём.
    assert "рабочий 77 с 09-28: 77×6@9 → 77×7@9 (2 сессии)" in card
    assert "диапазон 5–12 — добирать повторы" in card
    # Прошлый вес: 8 → 11 за четыре сессии, бэкоффы на нём — отдельно.
    assert "до этого 73: 8 → 9 → 10 → 11 за 4 сессии; бэкофф 73×12@9 на 09-28, 73×7 на 10-12" in card
    # e1RM без RPE, с датами: лучший — 73×12, последний — 77×7.
    assert "e1RM: лучший 102 (73×12@9, 09-28) → последний 95 (77×7@9, 10-12)" in card
    # Разминка 40×10 в серии не показывается, бэкоффы — компактно.
    assert "09-28 77×6@9 (+73×12@9,10@10)" in card
    assert "40×10" not in card
    assert "10-12 77×7@9 (+77×6 + 73×7)" in card
    # Контекст: перерыв, повторяющаяся жалоба и болезнь.
    # Перерыв — только тот, что попал в показанную серию (с 09-14).
    assert "перерыв 09-15–09-27 (13 дней)" in card
    assert "08-10" not in card
    assert "заметка «плечо ноет» ×3 с 09-28" in card
    assert "заметка «болел всю неделю, простыл» 10-05" in card
    # Ни одного сравнения повторов через разные веса.
    assert "12→7" not in json.dumps(payload, ensure_ascii=False)
    # Подходы в сессиях — с RPE, сессии старые первыми.
    assert payload["sessions"][-1]["sets"] == ["77x7@9", "77x6", "73x7"]
    assert payload["sessions"][0]["date"] < payload["sessions"][-1]["date"]
    assert payload["records"]["max_e1rm_date"] == "2026-09-28"
    assert payload["records"]["max_weight_date"] == "2026-09-28"


async def test_card_status_is_the_stall_verdict_not_its_own(fresh_db, user_id, monkeypatch):
    """Статус берётся из analytics.stall_facts — карточка вердикт не пересчитывает."""
    await _seed_curl(fresh_db, user_id)
    import analytics

    real = analytics.stall_facts

    def fake(sessions, today, unit):
        out = real(sessions, today, unit)
        return {**out, "verdict": "dead_end", "why": "outlier: 20×8 looks like a light day"}

    monkeypatch.setattr(analytics, "stall_facts", fake)
    payload = json.loads(await ai_trainer.execute_tool(
        user_id, "get_exercise_progress", {"exercise_name": "Сгибание рук в блоке"}))
    assert "статус: ТУПИК" in payload["card"]
    assert "разбор: outlier: 20×8 looks like a light day" in payload["card"]


async def test_personal_rep_range_drives_the_advice(fresh_db, user_id):
    await fresh_db.conn().execute(
        "UPDATE users SET rep_range_min = 3, rep_range_max = 6 WHERE telegram_id = ?", (user_id,))
    await fresh_db.conn().commit()
    await _seed_curl(fresh_db, user_id)

    report = json.loads(await ai_trainer.execute_tool(user_id, "get_progress_report", {}))
    (card,) = report["cards"]
    assert "диапазон 3–6 — добрал верх — пора прибавить вес" in card
    assert "личный диапазон повторов 3–6" in report["context"]


async def test_stalled_lifts_carry_the_card(fresh_db, user_id):
    ex_id = await _seed_curl(fresh_db, user_id)
    payload = json.loads(await ai_trainer.execute_tool(user_id, "get_stalled_lifts", {}))
    (lift,) = payload["lifts"]
    assert lift["reps_at_top_weight"] == "6→7"
    assert lift["card"] == await coach_facts.exercise_card(user_id, ex_id)
    assert lift["last_done"] == "2026-10-12" and lift["days_since_last"] == 3
    assert payload["rep_range"] == "5-12"


async def test_ready_to_add_weight_uses_the_personal_range(fresh_db, user_id):
    """Живой ответ звал «добить до 12» — это захардкоженная 12, а не диапазон атлета."""
    await fresh_db.conn().execute(
        "UPDATE users SET rep_range_min = 3, rep_range_max = 6 WHERE telegram_id = ?", (user_id,))
    await fresh_db.conn().commit()
    ex = await fresh_db.create_exercise(user_id, "Жим", await _group_id(fresh_db, user_id, "Грудь"))
    for day in ["2026-09-17", "2026-09-24", "2026-10-01", "2026-10-08"]:
        await _session(fresh_db, user_id, ex, day, [(100, 7)])
    payload = json.loads(await ai_trainer.execute_tool(user_id, "get_stalled_lifts", {}))
    (lift,) = payload["lifts"]
    assert lift["ready_to_add_weight"] is True
    assert payload["rep_range"] == "3-6"
    assert "диапазон 3–6 — добрал верх — пора прибавить вес" in lift["card"]
    progress = json.loads(await ai_trainer.execute_tool(user_id, "get_exercise_progress", {"exercise_name": "Жим"}))
    assert progress["rep_range"] == "3-6"


async def test_headline_is_the_window_change_not_a_one_rep_delta(fresh_db, user_id):
    """Прод: тренер назвал ростом «10→11 на 68.3» (сессии через 4 дня) и
    пропустил главное — тяга сидя e1RM 76.7→93.3 (+22%) за 7 недель, рабочий
    59→68.3. Изменение за окно стоит в заголовке, повторы на весе — ниже."""
    ex = await fresh_db.create_exercise(user_id, "Тяга сидя", await _group_id(fresh_db, user_id, "Спина"))
    for day, sets in [("2026-08-24", [(59, 9)]), ("2026-09-07", [(63.5, 9)]), ("2026-09-21", [(68.3, 8)]),
                      ("2026-10-08", [(68.3, 10)]), ("2026-10-12", [(68.3, 11)])]:
        await _session(fresh_db, user_id, ex, day, sets)
    card = await coach_facts.exercise_card(user_id, ex)
    head, *rest = card.split("\n")
    assert head.endswith(" · e1RM 76.7→93.3 (+22%) за 7 нед; рабочий 59→68.3")
    assert any(line.startswith("  рабочий 68.3 с 09-21: 68.3×8 → 68.3×10 → 68.3×11") for line in rest)


async def test_stale_lift_is_not_called_growing_and_a_dip_shows_its_peak(fresh_db, user_id):
    """Прод: жим одной рукой, последний раз 18.09, был назван «прогрессом 6→8»,
    хотя серия была 39×6 → 39×9 → 39×8 и почти четыре недели паузы."""
    ex = await fresh_db.create_exercise(user_id, "Жим одной рукой", await _group_id(fresh_db, user_id, "Грудь"))
    for day, reps in [("2026-09-04", 6), ("2026-09-11", 9), ("2026-09-18", 8)]:
        await _session(fresh_db, user_id, ex, day, [(39, reps)])
    card = await coach_facts.exercise_card(user_id, ex)
    head = card.split("\n")[0]
    assert "последний раз 09-18 (27 дней назад)" in head
    assert "не делал 27 дней — брошено или пауза, не «растёт»" in head
    assert "39×6 → 39×9 → 39×8 (пик 39×9 на 09-11) (3 сессии)" in card


async def test_progress_report_context_and_order(fresh_db, user_id):
    await _seed_curl(fresh_db, user_id)
    chest = await _group_id(fresh_db, user_id, "Грудь")
    bench = await fresh_db.create_exercise(user_id, "Жим штанги лёжа", chest)
    for day in ["2026-09-01", "2026-09-08", "2026-09-15", "2026-09-22", "2026-10-06"]:
        await _session(fresh_db, user_id, bench, day, [(100, 8)])
    old = await fresh_db.create_exercise(user_id, "Брошенное", chest)
    await _session(fresh_db, user_id, old, "2026-05-01", [(50, 10)])
    await fresh_db.add_bodyweight_log(user_id, 82.0, logged_at="2026-08-25T08:00:00")
    await fresh_db.add_bodyweight_log(user_id, 80.5, logged_at="2026-10-10T08:00:00")

    report = json.loads(await ai_trainer.execute_tool(user_id, "get_progress_report", {}))

    # Тупик — первым, заброшенного нет совсем.
    assert report["cards"][0].startswith("Жим штанги лёжа · Грудь")
    assert "ТУПИК" in report["cards"][0]
    assert not any("Брошенное" in c for c in report["cards"])
    ctx = report["context"]
    assert "сегодня 2026-10-15; веса в кг" in ctx
    assert "вес тела: 82кг (08-25) → 80.5кг (10-10), -1.5кг" in ctx
    assert "перерывы за 8 нед: перерыв 08-10–08-30 (21 день)\n" in ctx
    assert "заметка «болел всю неделю, простыл» 10-05" in ctx
    assert "тренировок в неделю: " in ctx


async def test_lb_athlete_card_stays_in_pounds(fresh_db, user_id):
    await fresh_db.conn().execute("UPDATE users SET unit = 'lb' WHERE telegram_id = ?", (user_id,))
    await fresh_db.conn().commit()
    ex_id = await fresh_db.create_exercise(user_id, "Сгибания", await _group_id(fresh_db, user_id, "Бицепс"))
    for day, sets in [("2026-09-10", [(160, 8)]), ("2026-09-17", [(165, 6)]),
                      ("2026-09-24", [(165, 7)]), ("2026-10-01", [(165, 8)])]:
        await _session(fresh_db, user_id, ex_id, day, sets)

    report = json.loads(await ai_trainer.execute_tool(user_id, "get_progress_report", {}))
    (card,) = report["cards"]
    assert "веса в lb" in report["context"]
    assert "рабочий 165 с 09-17: 165×6 → 165×7 → 165×8 (3 сессии)" in card
    assert "до этого 160: 8 за 1 сессию" in card
    assert "lb/week" in card  # «why» из stall_facts — в единицах атлета


async def test_bodyweight_lift_shows_bw_and_added_weight(fresh_db, user_id):
    back = await _group_id(fresh_db, user_id, "Спина")
    ex_id = await fresh_db.create_exercise(user_id, "Подтягивания", back)
    await fresh_db.conn().execute(
        "UPDATE exercises SET bodyweight_load = 'full', bodyweight_factor = 1.0 WHERE id = ?", (ex_id,))
    await fresh_db.conn().commit()
    await fresh_db.add_bodyweight_log(user_id, 80.0, logged_at="2026-08-01T08:00:00")
    for day, sets in [("2026-09-01", [(0, 8), (0, 7)]), ("2026-09-08", [(0, 9)]),
                      ("2026-09-15", [(10, 6), (0, 9)]), ("2026-09-22", [(10, 7)])]:
        await _session(fresh_db, user_id, ex_id, day, sets)

    card = await coach_facts.exercise_card(user_id, ex_id)
    assert "рабочий +10 с 09-15: +10×6 → +10×7 (2 сессии)" in card
    assert "до этого вес тела: 8 → 9 за 2 сессии; бэкофф вес тела×9 на 09-15" in card
    assert "не делал 23 дня — брошено или пауза" in card
    assert not re.search(r"(?<![\d.+])0×", card) and "0.0" not in card
    # e1RM — по нагрузке с весом тела (80 + 10), а не по нулю.
    assert "e1RM: лучший 111 (+10×7, 09-22)" in card

    cmp = json.loads(await ai_trainer.execute_tool(user_id, "compare_periods", {"days": 30}))
    (row,) = cmp["exercises"]
    assert row["after"]["top_e1rm"] > 0
    assert row["after"]["tonnage_per_session"] > 0


async def test_english_athlete_gets_english_cards_end_to_end(fresh_db, user_id):
    await fresh_db.set_user_lang(user_id, "en")
    await _seed_curl(fresh_db, user_id, name="Cable Curl", english_notes=True)
    legs = await _group_id(fresh_db, user_id, "Ноги")
    squat = await fresh_db.create_exercise(user_id, "Присед со штангой", legs)
    await fresh_db.conn().execute(
        "UPDATE exercises SET display_name = 'Barbell Squat', name = 'Barbell Squat' WHERE id = ?", (squat,))
    await fresh_db.conn().commit()
    await _session(fresh_db, user_id, squat, "2026-10-01", [(60, 5), (100, 5), (100, 5)],
                   ex_note="knee pain again")

    with i18n.use_lang("en"):
        for tool in ("get_progress_report", "get_balance_report", "get_stalled_lifts"):
            payload = json.loads(await ai_trainer.execute_tool(user_id, tool, {}))
            text = json.dumps(payload, ensure_ascii=False)
            assert not CYRILLIC.search(text), (tool, text)
        report = json.loads(await ai_trainer.execute_tool(user_id, "get_progress_report", {}))
        progress = json.loads(await ai_trainer.execute_tool(
            user_id, "get_exercise_progress", {"exercise_name": "Cable Curl"}))

    card = next(c for c in report["cards"] if c.startswith("Cable Curl"))
    assert card.startswith("Cable Curl · Biceps · ")
    assert "working 77 since 09-28: 77×6@9 → 77×7@9 (2 sessions)" in card
    assert "before that 73: 8 → 9 → 10 → 11 over 4 sessions" in card
    assert "last done 10-12 (3 days ago)" in card
    assert "break 09-15–09-27 (13 days)" in card
    assert 'note "shoulder aches" ×3 since 09-28' in card
    assert "today 2026-10-15; weights in kg" in report["context"]
    assert not CYRILLIC.search(progress["card"])
    squat_card = next(c for c in report["cards"] if c.startswith("Barbell Squat"))
    assert 'note "knee pain again" 10-01' in squat_card
    assert "TOO LITTLE DATA" in squat_card


async def test_balance_report_counts_working_sets_splits_deadlift_and_reads_the_plan(fresh_db, user_id):
    back = await _group_id(fresh_db, user_id, "Спина")
    legs = await _group_id(fresh_db, user_id, "Ноги")
    dl = await fresh_db.create_exercise(user_id, "Становая тяга", back)
    squat = await fresh_db.create_exercise(user_id, "Присед со штангой", legs)
    program_id = await fresh_db.create_program(user_id, "Верх/низ")
    day_a = await fresh_db.create_routine(user_id, "Низ", program_id=program_id)
    await fresh_db.create_routine(user_id, "Верх", program_id=program_id)
    # Дневник ведётся дольше окна — средние делятся на полные 4 недели.
    await _session(fresh_db, user_id, squat, "2026-08-01", [(100, 5)])
    for day in ["2026-09-21", "2026-09-28", "2026-10-05", "2026-10-12"]:
        # Разминки 60 и 80 перед 140 — не рабочие (<65% от 140 = 91).
        wid = await _session(fresh_db, user_id, dl, day, [(60, 5), (80, 5), (140, 5), (140, 5)])
        await fresh_db.conn().execute("UPDATE workouts SET routine_id = ? WHERE id = ?", (day_a, wid))
        await _session(fresh_db, user_id, squat, day, [(100, 5), (100, 5), (100, 5)])
    await fresh_db.conn().commit()

    payload = json.loads(await ai_trainer.execute_tool(user_id, "get_balance_report", {}))
    report = payload["report"]

    assert "окно 2026-09-18..2026-10-15 (4 нед)" in report
    # Становая: 2 рабочих в неделю → 1 спине, 1 ногам.
    assert "Спина: 1 подх./нед, 1×/нед — ниже нормы" in report
    # Ноги: присед 3 + половина становой; частота — по дням, а не тренировкам.
    assert "Ноги: 4 подх./нед, 1×/нед — ниже нормы" in report
    assert "  Квадрицепс: 3/нед (минимум 6) — ниже минимума" in report
    assert "  Бицепс бедра: 0.5/нед (минимум 6) — ниже минимума" in report
    assert "присед 3, наклон (становая, румынская) 2" in report
    # Нетронутая группа — одной строкой, без нулевых мишеней.
    assert "Плечи: 0 подх./нед, 0×/нед — не тренировал" in report
    assert "Средняя дельта" not in report
    assert "ни одного подхода за окно: горизонтальный жим, вертикальный жим, горизонтальная тяга, вертикальная тяга" in report
    assert "программа «Верх/низ» (2 дня): план 2×/нед, факт 1×/нед; по дням: Низ ×4" in report
    assert "становая" in report


def test_movement_patterns_cover_the_basics():
    p = seed_data.movement_pattern
    assert p("Присед со штангой", "Ноги") == "squat"
    assert p("Румынская тяга", "Ноги") == "hinge"
    assert p("Становая тяга сумо", "Спина") == "hinge"
    assert p("Жим штанги лёжа", "Грудь") == "horizontal_push"
    assert p("Жим штанги стоя", "Плечи") == "vertical_push"
    assert p("Тяга штанги в наклоне", "Спина") == "horizontal_pull"
    assert p("Австралийские подтягивания", "Спина") == "horizontal_pull"
    assert p("Подтягивания", "Спина") == "vertical_pull"
    assert p("Тяга верхнего блока", "Спина") == "vertical_pull"
    assert p("Разведение гантелей в стороны", "Плечи") is None
    assert p("Barbell Row", "Спина") == "horizontal_pull"


def test_deadlift_volume_split_is_half_back_half_legs():
    groups, targets = seed_data.volume_split("Становая тяга", "Становая тяга", "Спина")
    assert groups == {"Спина": 0.5, "Ноги": 0.5}
    assert targets == {"hamstrings": 0.25, "glutes": 0.25}
    # Перенесённая атлетом в «Ноги» — его решение старше.
    assert seed_data.volume_split("Становая тяга", "Становая тяга", "Ноги")[0] == {"Ноги": 1.0}


def test_warmups_only_at_the_start_are_dropped():
    S = coach_facts.LoggedSet
    sets = [S(40, 40, 10, None), S(60, 60, 5, None), S(100, 100, 5, None), S(60, 60, 12, None)]
    assert [s.load for s in coach_facts.working_sets(sets)] == [100, 60]


def test_note_flags_match_both_languages_and_skip_bolshe():
    f = coach_facts.note_is_flagged
    assert f("плечо ноет") and f("болит колено") and f("не выспался") and f("простыл")
    assert f("knee pain") and f("slept 4 hours") and f("sick")
    assert not f("сделал больше повторов") and not f("good session")


async def test_short_diary_is_not_averaged_over_the_full_window(fresh_db, user_id):
    chest = await _group_id(fresh_db, user_id, "Грудь")
    ex = await fresh_db.create_exercise(user_id, "Жим штанги лёжа", chest)
    for day in ["2026-10-02", "2026-10-04", "2026-10-09", "2026-10-11", "2026-10-14"]:
        await _session(fresh_db, user_id, ex, day, [(100, 8), (100, 8)])
    report = json.loads(await ai_trainer.execute_tool(user_id, "get_balance_report", {}))["report"]
    # Две недели дневника: 10 подходов / 2 нед, а не / 4.
    assert "(2 нед)" in report and "Грудь: 5 подх./нед, 2.5×/нед — ниже нормы" in report
    progress = json.loads(await ai_trainer.execute_tool(user_id, "get_progress_report", {}))
    assert "2.5×/нед" in progress["cards"][0]
    assert "тренировок в неделю: 2.5 за 4 нед, 2.5 за 8 нед" in progress["context"]
    assert "дневник ведётся 2 нед — средние посчитаны по ним" in progress["context"]


async def test_recent_workouts_are_old_to_new(fresh_db, user_id):
    chest = await _group_id(fresh_db, user_id, "Грудь")
    ex = await fresh_db.create_exercise(user_id, "Жим", chest)
    for day in ["2026-10-01", "2026-10-05", "2026-10-09"]:
        await _session(fresh_db, user_id, ex, day, [(100, 8)])
    payload = json.loads(await ai_trainer.execute_tool(user_id, "list_recent_workouts", {}))
    assert [w["date"] for w in payload["workouts"]] == ["2026-10-01", "2026-10-05", "2026-10-09"]


async def test_compare_periods_windows_do_not_overlap(fresh_db, user_id):
    chest = await _group_id(fresh_db, user_id, "Грудь")
    ex = await fresh_db.create_exercise(user_id, "Жим", chest)
    await _session(fresh_db, user_id, ex, "2026-09-16", [(100, 8), (100, 8)])
    await _session(fresh_db, user_id, ex, "2026-09-15", [(100, 8)])
    payload = json.loads(await ai_trainer.execute_tool(user_id, "compare_periods", {"days": 30}))
    assert payload["recent"] == {"from": "2026-09-16", "to": "2026-10-15"}
    assert payload["previous"] == {"from": "2026-08-17", "to": "2026-09-15"}
    (row,) = payload["exercises"]
    assert row["after"]["workouts"] == 1 and row["before"]["workouts"] == 1
    assert row["after"]["tonnage_per_session"] == 1600
    assert row["e1rm_delta"] == 0.0
    assert "-0.0" not in json.dumps(payload)


def test_negative_zero_is_scrubbed():
    assert json.dumps(ai_trainer._without_negative_zero({"a": [-0.0, (1.0, -0.0)]})) == '{"a": [0.0, [1.0, 0.0]]}'


def test_prompt_has_a_static_section_on_judging_progress():
    prompt = ai_trainer.SYSTEM_PROMPT
    assert "Как судить о прогрессе." in prompt
    for rule in (
        "прогресс называй изменением e1RM и рабочего веса за окно — с датами и %",
        "+1 повтор между соседними сессиями — не «прогресс»",
        "повторы на одном весе — только чтобы объяснить застой или свежую прибавку",
        "бэкофф и разминка — не рабочий вес",
        "меньше трёх сессий на весе — начало, а не тупик",
        "посмотри перерывы, болезнь и заметки и назови их",
        "числа цитируй из полей инструментов вместе с датой",
        "застой или регресс называй, только если так сказал status; не переворачивай",
        "свежая прибавка веса, на которой повторы держатся, — это прогресс",
        "не делал движение 14 дней и больше — оно не «растёт»",
        "личный диапазон атлета (rep_range",
        "был пик выше — назови его",
        "не пересказывай, сколько упражнений без данных",
        "без дежурных оговорок",
    ):
        assert rule in prompt, rule
    # Поиск отключён гейтом (search=False) и инструментом модели не был никогда.
    assert "Если среди доступных инструментов есть веб-поиск" not in prompt
    assert "не притворяйся, что искал в интернете" in prompt
    # Промпт — статичный префикс: ни даты, ни данных атлета.
    assert ai_trainer._system_prompt() == ai_trainer._with_language_tail(ai_trainer.SYSTEM_PROMPT)


def test_prompt_shapes_a_progress_review():
    """Живой ответ Luna на «how's my progress»: верный по фактам, но с
    оговорками «can't confirm muscle-size», «don't prove the cause», с
    пересказом своих правил («I shouldn't describe them as progressing») и
    одним шагом на весь разбор. Grok вдобавок ссылался на «status says»."""
    prompt = ai_trainer.SYSTEM_PROMPT
    for rule in (
        # Оговорка — только если меняет действие; неизвестная причина не называется.
        "оговорка — только если от неё\n  меняется, что делать атлету",
        "причина неизвестна — просто не называй её",
        # Мета-разговор о своих правилах вместо вывода.
        "свои правила вслух не пересказывай",
        "«**молотки** — на паузе с 26.09»",
        # Вывод не приписывается инструменту.
        "вывод не приписывай инструменту",
        "«status says»",
        # Структура разбора.
        "Разбор прогресса или застоя",
        "короткие разделы «## »: Растёт, На паузе, Под вопросом",
        "до 6 движений, остальные — только если спросят",
        "один следующий шаг вес×повторы",
        "в личном диапазоне атлета",
        "в конце — ОДИН главный шаг",
    ):
        assert rule in prompt, rule
    # Пара «плохо → хорошо» в обоих языках, короткая: она в кэшируемом префиксе.
    start = prompt.index("Разбор прогресса или застоя")
    example = prompt[start:prompt.index("Main step:", start)]
    assert "I shouldn't call it progressing" in example
    assert "## Растёт" in example and "## На паузе" in example
    assert "## Moving" in example and "## On pause" in example
    assert "Главное:" in example
    assert len(example) < 1200, "пример разбора разросся — он живёт в кэшируемом префиксе"
    # Плохие фразы из живого ответа звучат только в «Нет», не в образце.
    good = example[example.index("Да:"):]
    for leak in ("can't confirm", "shouldn't", "не должен", "status says"):
        assert leak not in good, leak
