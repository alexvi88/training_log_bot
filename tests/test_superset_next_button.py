"""Вкладки суперсета в боте: активное первым, остальные по кругу.

Отдельной кнопки «Дальше →» в боте нет (в приложении она есть): её убрали по
просьбе владельца — следующее по кругу и так всегда вторая вкладка.
"""
import keyboards


def _cbs(kb):
    return [[b.callback_data for b in row] for row in kb.inline_keyboard]


def test_tabs_start_with_active_then_cycle():
    kb = keyboards.logging_keyboard([(1, "Bench"), (2, "Row"), (3, "Curl")], active_id=3, has_sets=True)
    rows = _cbs(kb)
    assert rows[:3] == [["live:switch:3"], ["live:switch:1"], ["live:switch:2"]]
    assert kb.inline_keyboard[0][0].text.startswith("▶ ")


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


def test_no_next_button_in_bot():
    for open_items, active in (([(1, "Bench"), (2, "Row")], 1), ([(1, "Bench")], 1)):
        cbs = [c for row in _cbs(keyboards.logging_keyboard(open_items, active_id=active)) for c in row]
        assert not any(c.startswith("live:next:") for c in cbs)


def test_rows_under_tabs_unchanged():
    rows = _cbs(keyboards.logging_keyboard([(1, "Bench"), (2, "Row")], active_id=1, has_sets=True))
    assert rows[2:] == [["live:add_exercise", "live:note:1"], ["live:undo"], ["live:finish_exercise"]]
