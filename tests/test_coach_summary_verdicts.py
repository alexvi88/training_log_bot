"""AI-сводка тренера: вердикты по упражнениям считает сервер, а не модель.

Прод, англоязычный тренер: «Demo: Progressing — Barbell Squat went from
92.5×5 to 70×8; Barbell Bench Press went from 40×8 to 40×8, no change», «Вика:
Progressing — Жим в тренажёре 32×10 → 34×8», «encourage her». На деле присед —
спад e1RM (~107.9 → ~88.7), жим лёжа — ровно (лучший подход 40×8 в обеих
сессиях), жим в тренажёре — ровно в пределах шума (~42.7 → ~43.1). Вердикты
теперь посчитаны до модели (analytics.lift_change — та же мерка, что у
прогресса атлета) и приходят ей готовыми строками; промпт велит брать их как
есть, без вопросов, догадок и местоимений с родом.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from luna_fakes import as_luna

import ai_trainer
import analytics
import coach_service
import i18n

COACH = 701
VIKA = 702
DEMO = 703


def _session(day: str, *sets: tuple[float, int], wid: int = 1) -> analytics.SessionStats:
    return analytics.SessionStats(
        workout_id=wid, started_at=f"{day}T10:00:00",
        sets=[analytics.SetRow(weight=w, reps=r, workout_id=wid, started_at=f"{day}T10:00:00") for w, r in sets],
    )


# ---------- чистый расчёт ----------


def test_squat_lighter_load_more_reps_is_a_drop():
    change = analytics.lift_change([
        _session("2026-09-25", (92.5, 5), wid=1),
        _session("2026-10-15", (70.0, 8), wid=2),
    ])
    assert change.verdict == analytics.LIFT_DROP
    assert (change.first_e1rm, change.last_e1rm) == (107.9, 88.7)
    assert round(change.pct) == -18


def test_same_best_set_is_flat_even_with_extra_sets():
    """40×8 → 40×8, 40×8, 40×6: лучший подход тот же — ровно, не «improved»."""
    change = analytics.lift_change([
        _session("2026-09-25", (40.0, 8), wid=1),
        _session("2026-10-15", (40.0, 8), (40.0, 8), (40.0, 6), wid=2),
    ])
    assert change.verdict == analytics.LIFT_FLAT
    assert change.same_best_set


def test_machine_press_within_noise_is_near_flat():
    change = analytics.lift_change([
        _session("2026-10-02", (32.0, 10), wid=1),
        _session("2026-10-11", (34.0, 8), wid=2),
    ])
    assert change.verdict == analytics.LIFT_NEAR_FLAT
    assert (change.first_e1rm, change.last_e1rm) == (42.7, 43.1)


def test_real_rise_and_weight_bump_are_up():
    rise = analytics.lift_change([
        _session("2026-09-25", (60.0, 5), wid=1),
        _session("2026-10-15", (70.0, 8), wid=2),
    ])
    assert rise.verdict == analytics.LIFT_UP
    # Прибавка веса при тех же повторах — рост, даже если e1RM сдвинулся
    # меньше порога шума (200×5 → 202.5×5 — +1.25%).
    bump = analytics.lift_change([
        _session("2026-09-25", (200.0, 5), wid=1),
        _session("2026-10-15", (202.5, 5), wid=2),
    ])
    assert bump.verdict == analytics.LIFT_UP


def test_sets_over_twenty_reps_are_not_counted():
    change = analytics.lift_change([
        _session("2026-09-25", (50.0, 8), wid=1),
        _session("2026-10-15", (50.0, 8), (40.0, 30), wid=2),
    ])
    assert change.verdict == analytics.LIFT_FLAT


def test_single_session_has_no_verdict():
    assert analytics.lift_change([_session("2026-10-15", (50.0, 8))]) is None


def test_client_verdict_progressing_only_when_something_rose():
    def c(verdict):
        return analytics.LiftChange(verdict, ("a", 1, 1), ("b", 1, 1), 1, 1, 0, 2)

    assert analytics.client_lift_verdict([c("drop"), c("flat")], False) == "dropping"
    assert analytics.client_lift_verdict([c("near_flat")], False) == "flat"
    assert analytics.client_lift_verdict([c("up"), c("drop")], False) == "mixed"
    assert analytics.client_lift_verdict([c("up")], False) == "progressing"
    assert analytics.client_lift_verdict([], True) == "progressing"
    assert analytics.client_lift_verdict([], False) is None


# ---------- то, что уходит модели ----------


class _Completions:
    """Заглушка модели: строит сводку только из готовых вердиктов брифа — так
    сводка не может назвать ростом то, что сервер посчитал спадом."""

    def __init__(self):
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        data = kwargs["messages"][1]["content"]
        lines = []
        for block in data.split("\n\n")[1:]:
            name = re.search(r"^Athlete: (.+)$", block, re.M).group(1)
            overall = re.search(r"^Overall verdict \(computed, use as given\): (.+)$", block, re.M).group(1)
            lifts = re.findall(r"^(.+?): .* — (\S[^\n]*)$", block, re.M)
            lines.append(f"{name}: {overall.capitalize()} — " + "; ".join(f"{n} {v}" for n, v in lifts))
        text = "\n".join(lines)
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=50, total_tokens=150,
                                prompt_tokens_details=None, completion_tokens_details=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))], usage=usage)


@pytest.fixture
def model(monkeypatch):
    completions = _Completions()
    fake = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: as_luna(fake))
    return completions


async def _workout(db, user_id: int, day: str, items: list[tuple[str, list[tuple[float, int]]]]) -> None:
    started = f"{day}T10:00:00"
    wid = await db.create_workout(user_id, started_at=started)
    for order, (name, sets) in enumerate(items):
        cur = await db.conn().execute("SELECT id FROM exercises WHERE user_id = ? AND name = ?", (user_id, name))
        row = await cur.fetchone()
        if row is None:
            gid = await db.create_muscle_group(user_id, "Ноги")
            ex = await db.create_exercise(user_id, name, gid)
        else:
            ex = row["id"]
        block = await db.create_block(wid, "single")
        await db.add_block_exercise(block, ex, order)
        for i, (weight, reps) in enumerate(sets, start=1):
            await db.add_set(block, ex, i, 0, weight, reps)
    await db.finish_workout(wid, finished_at=started)


async def _prod_case(db) -> None:
    """Ровно данные из прод-сводки."""
    for uid, lang in ((COACH, "en"), (VIKA, "ru"), (DEMO, "en")):
        await db.get_or_create_user(telegram_id=uid, username=f"user{uid}")
        await db.set_user_lang(uid, lang)
    await db.upsert_coach_profile(COACH, "Coach")
    assert await db.create_coach_link(COACH, VIKA, "Вика", show_bodyweight=False)
    assert await db.create_coach_link(COACH, DEMO, "Demo", show_bodyweight=False)
    await _workout(db, VIKA, "2026-10-02", [("Жим в тренажёре", [(32.0, 10)])])
    await _workout(db, VIKA, "2026-10-11", [("Жим в тренажёре", [(34.0, 8)])])
    await _workout(db, DEMO, "2026-09-25", [
        ("Barbell Squat", [(92.5, 5)]), ("Barbell Bench Press", [(40.0, 8)]),
    ])
    await _workout(db, DEMO, "2026-10-15", [
        ("Barbell Squat", [(70.0, 8)]), ("Barbell Bench Press", [(40.0, 8), (40.0, 8), (40.0, 6)]),
    ])


async def test_prod_case_verdicts_reach_the_model_precomputed(fresh_db, model):
    await _prod_case(fresh_db)
    with i18n.use_lang("en"):
        result = await coach_service.ai_summary(COACH)
    system, data = (m["content"] for m in model.calls[-1]["messages"])

    assert "Barbell Squat: 92.5×5 (2026-09-25) → 70×8 (2026-10-15), e1RM 107.9 → 88.7kg (−18%) — drop" in data
    assert "Barbell Bench Press: 40×8 (2026-09-25) → 40×8 (2026-10-15) — flat" in data
    assert ("Жим в тренажёре: 32×10 (2026-10-02) → 34×8 (2026-10-11), e1RM 42.7 → 43.1kg (+1%) "
            "— flat (change within noise)") in data
    vika, demo = (b for b in data.split("\n\n")[1:] if "Athlete: " in b)
    if "Вика" not in vika:
        vika, demo = demo, vika
    assert "Overall verdict (computed, use as given): flat" in vika
    assert "Overall verdict (computed, use as given): dropping" in demo
    assert "progressing" not in data

    # Правила промпта, о которые сводка спотыкалась.
    for rule in (
        "Never relabel it", "a drop is a drop, flat is flat", "Don't compare sets yourself",
        "no question marks", '"maybe"', "don't translate, explain or comment on them",
        '"her"', '"they"',
    ):
        assert rule in system, rule

    # Сводка из заглушки собрана только из вердиктов — «Progressing» в ней нет.
    assert "Progressing" not in result["text"]
    assert "Demo: Dropping" in result["text"] and "Вика: Flat" in result["text"]


async def test_verdict_lines_in_russian_for_a_russian_coach(fresh_db, model):
    await _prod_case(fresh_db)
    await fresh_db.set_user_lang(COACH, "ru")
    model.create = _capture_only(model)
    with i18n.use_lang("ru"):
        await coach_service.ai_summary(COACH)
    system, data = (m["content"] for m in model.calls[-1]["messages"])
    assert "Barbell Squat: 92.5×5 (2026-09-25) → 70×8 (2026-10-15), e1RM 107.9 → 88.7кг (−18%) — спад" in data
    assert "Общий вывод (посчитан, бери как есть): падает" in data
    assert "Общий вывод (посчитан, бери как есть): ровно" in data
    assert "ровно (разница в пределах шума)" in data
    for rule in ("Не переименовывай", "без вопросительных знаков", "«её»", "безлично", "Сам подходы не сравнивай"):
        assert rule in system, rule


def _capture_only(completions):
    async def create(**kwargs):
        completions.calls.append(kwargs)
        usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2,
                                prompt_tokens_details=None, completion_tokens_details=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))], usage=usage)
    return create


async def test_real_rise_is_progressing(fresh_db, model):
    await _prod_case(fresh_db)
    await _workout(fresh_db, DEMO, "2026-09-20", [("Front Squat", [(60.0, 5)])])
    await _workout(fresh_db, DEMO, "2026-10-14", [("Front Squat", [(70.0, 8)])])
    with i18n.use_lang("en"):
        await coach_service.ai_summary(COACH, DEMO)
    data = model.calls[-1]["messages"][1]["content"]
    assert "Front Squat: 60×5 (2026-09-20) → 70×8 (2026-10-14), e1RM 70 → 88.7kg (+27%) — up" in data
    # Присед упал, фронтальный вырос — вразнобой, а не «растёт».
    assert "Overall verdict (computed, use as given): mixed" in data

