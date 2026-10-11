"""«Дальше →» в суперсете: следующее открытое упражнение по кругу.

Кнопка только переключает (тот же обработчик, что вкладки), ничего не пишет и
не трогает остальные кнопки экрана записи.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

import i18n
import keyboards
from handlers import workout


def _cbs(kb):
    return [[b.callback_data for b in row] for row in kb.inline_keyboard]


def test_next_id_cycles_in_open_order():
    ids = [10, 20, 30]
    assert keyboards.superset_next_id(10, ids) == 20
    assert keyboards.superset_next_id(20, ids) == 30
    assert keyboards.superset_next_id(30, ids) == 10


def test_next_id_absent_outside_superset():
    assert keyboards.superset_next_id(10, [10]) is None
    assert keyboards.superset_next_id(None, [10, 20]) is None
    assert keyboards.superset_next_id(99, [10, 20]) is None


def test_button_sits_under_tabs_and_points_to_next():
    kb = keyboards.logging_keyboard([(1, "Bench"), (2, "Row"), (3, "Curl")], active_id=3, has_sets=True)
    rows = _cbs(kb)
    # Активная (3) — первой, дальше по кругу; «Дальше →» ведёт на вторую.
    assert rows[:4] == [["live:switch:3"], ["live:switch:1"], ["live:switch:2"], ["live:next:1"]]
    assert kb.inline_keyboard[0][0].text.startswith("▶ ")
    assert kb.inline_keyboard[3][0].text == "Дальше →"


def test_rotated_puts_active_first_then_cycle():
    items = [(10, "A"), (20, "B"), (30, "C")]
    assert [i for i, _ in keyboards.superset_rotated(items, 10)] == [10, 20, 30]
    assert [i for i, _ in keyboards.superset_rotated(items, 20)] == [20, 30, 10]
    assert [i for i, _ in keyboards.superset_rotated(items, 30)] == [30, 10, 20]


def test_rotated_keeps_open_order_without_active():
    items = [(10, "A"), (20, "B")]
    assert keyboards.superset_rotated(items, None) == items
    assert keyboards.superset_rotated(items, 99) == items
    assert keyboards.superset_rotated([], 10) == []


def test_second_tab_is_where_next_goes():
    items = [(1, "Bench"), (2, "Row"), (3, "Curl")]
    for active in (1, 2, 3):
        rows = _cbs(keyboards.logging_keyboard(items, active_id=active))
        assert rows[0] == [f"live:switch:{active}"]
        assert rows[3] == [rows[1][0].replace("switch", "next")]


def test_button_leaves_other_buttons_alone():
    with_superset = _cbs(keyboards.logging_keyboard([(1, "Bench"), (2, "Row")], active_id=1, has_sets=True))
    # Всё, что ниже вкладок и «Дальше →», — ровно те же ряды, что и были.
    assert with_superset[3:] == [["live:add_exercise", "live:note:1"], ["live:undo"], ["live:finish_exercise"]]


def test_no_button_with_one_open_exercise():
    cbs = [c for row in _cbs(keyboards.logging_keyboard([(1, "Bench")], active_id=1)) for c in row]
    assert not any(c.startswith("live:next:") for c in cbs)


def test_button_in_english():
    with i18n.use_lang("en"):
        kb = keyboards.logging_keyboard([(1, "Bench"), (2, "Row")], active_id=1)
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert "Next →" in texts


@pytest.mark.asyncio
async def test_next_callback_switches_active_exercise(monkeypatch):
    render = AsyncMock()
    monkeypatch.setattr(workout, "_render_logging_screen", render)
    monkeypatch.setattr(workout.db, "get_user", AsyncMock(return_value={"id": 1}))
    state = MagicMock()
    state.get_data = AsyncMock(return_value={"open_exercises": [1, 2], "active_exercise_id": 1})
    state.update_data = AsyncMock()
    callback = MagicMock()
    callback.data = "live:next:2"
    callback.answer = AsyncMock()
    callback.from_user.id = 1

    await workout.live_switch_exercise(callback, state)

    state.update_data.assert_awaited_once_with(active_exercise_id=2)
    render.assert_awaited_once()
