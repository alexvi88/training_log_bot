"""Inline keyboard builders. Callback data uses a short `prefix:arg` scheme."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from formatting import format_weight


def main_menu(has_active_workout: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if has_active_workout:
        b.button(text="▶️ Продолжить тренировку", callback_data="menu:resume_workout")
    else:
        b.button(text="🏋️ Начать тренировку", callback_data="menu:start_workout")
    b.button(text="📈 Прогресс", callback_data="menu:progress")
    b.button(text="📚 История", callback_data="menu:history")
    b.button(text="⚙️ Упражнения", callback_data="menu:exercises")
    b.button(text="🔧 Настройки", callback_data="menu:settings")
    b.adjust(1)
    return b.as_markup()


def groups_keyboard(groups, prefix: str, extra_buttons: list[tuple[str, str]] | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for g in groups:
        label = f"{g['emoji'] or ''} {g['name']}".strip()
        b.button(text=label, callback_data=f"{prefix}:grp:{g['id']}")
    for text, cb in extra_buttons or []:
        b.button(text=text, callback_data=cb)
    b.adjust(2)
    return b.as_markup()


def exercises_keyboard(
    exercises,
    prefix: str,
    show_templates_button: bool = True,
    show_new_button: bool = True,
    back_cb: str = "back",
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for ex in exercises:
        b.button(text=ex["display_name"], callback_data=f"{prefix}:ex:{ex['id']}")
    if show_templates_button:
        b.button(text="📋 Шаблоны", callback_data=f"{prefix}:templates")
    if show_new_button:
        b.button(text="➕ Новое упражнение", callback_data=f"{prefix}:new")
    b.button(text="⬅️ Назад", callback_data=f"{prefix}:{back_cb}")
    b.adjust(1)
    return b.as_markup()


def templates_keyboard(templates, prefix: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for t in templates:
        b.button(text=t["display_name"], callback_data=f"{prefix}:tpl:{t['id']}")
    b.button(text="⬅️ Назад", callback_data=f"{prefix}:back")
    b.adjust(1)
    return b.as_markup()


def yes_no_keyboard(yes_cb: str, no_cb: str, yes_text: str = "Да", no_text: str = "Нет") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=yes_text, callback_data=yes_cb)
    b.button(text=no_text, callback_data=no_cb)
    b.adjust(2)
    return b.as_markup()


def set_input_keyboard(weight: float, step: float, step_big: float, in_superset: bool) -> InlineKeyboardMarkup:
    """Primary set-logging keyboard: weight steppers + a 1-20 reps grid (tap = log set)."""
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text=f"➖{format_weight(step_big)}", callback_data="live:w:bigminus"),
        InlineKeyboardButton(text=f"➖{format_weight(step)}", callback_data="live:w:minus"),
        InlineKeyboardButton(text=f"{format_weight(weight)} кг", callback_data="live:w:exact"),
        InlineKeyboardButton(text=f"➕{format_weight(step)}", callback_data="live:w:plus"),
        InlineKeyboardButton(text=f"➕{format_weight(step_big)}", callback_data="live:w:bigplus"),
    )
    b.row(InlineKeyboardButton(text="✏️ Ввести", callback_data="live:w:exact"))
    for start in range(1, 21, 5):
        b.row(*(
            InlineKeyboardButton(text=str(n), callback_data=f"live:reps:{n}")
            for n in range(start, start + 5)
        ))
    b.row(InlineKeyboardButton(text="Другое", callback_data="live:reps:other"))
    finish_text = "✅ Закончить суперсет" if in_superset else "✅ Закончить упражнение"
    b.row(
        InlineKeyboardButton(text="↩️ Удалить последний сет", callback_data="live:undo"),
        InlineKeyboardButton(text=finish_text, callback_data="live:finish_exercise"),
    )
    return b.as_markup()


def exercise_picker_entry_keyboard(has_planned: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if has_planned:
        b.button(text="▶️ Следующее по шаблону", callback_data="live:next_planned")
    b.button(text="➕ Упражнение", callback_data="live:add_exercise")
    b.button(text="🔗 Суперсет", callback_data="live:add_superset")
    b.button(text="🏁 Завершить тренировку", callback_data="live:finish_workout")
    b.adjust(1, 2, 1) if has_planned else b.adjust(2, 1)
    return b.as_markup()


def superset_picker_keyboard(picked_count: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if picked_count >= 2:
        b.button(text="✅ Готово", callback_data="ss:done")
    b.button(text="❌ Отмена", callback_data="ss:cancel")
    b.adjust(1)
    return b.as_markup()


def new_exercise_attrs_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Одной рукой/ногой", callback_data="attr:unilateral")
    b.button(text="✅ Готово", callback_data="attr:done")
    b.adjust(1)
    return b.as_markup()


def finish_workout_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📝 Добавить заметку", callback_data="finish:note")
    b.button(text="✅ Без заметки", callback_data="finish:skip_note")
    b.adjust(1)
    return b.as_markup()


def progress_charts_keyboard(exercise_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📈 e1RM", callback_data=f"chart:e1rm:{exercise_id}")
    b.button(text="📊 Тоннаж", callback_data=f"chart:tonnage:{exercise_id}")
    b.button(text="🔵 Сеты", callback_data=f"chart:scatter:{exercise_id}")
    b.adjust(3)
    return b.as_markup()


def history_list_keyboard(workouts, page: int, has_next: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for w in workouts:
        b.button(text=w["label"], callback_data=f"hist:item:{w['id']}")
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"hist:page:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"hist:page:{page + 1}"))
    b.adjust(1)
    if nav:
        b.row(*nav)
    return b.as_markup()


def history_item_keyboard(workout_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📋 Дублировать как новую", callback_data=f"hist:dup:{workout_id}")
    b.button(text="⬅️ К списку", callback_data="hist:back")
    b.adjust(1)
    return b.as_markup()


def settings_keyboard(unit: str, default_weight_step: float, hide_warmups: bool, formula: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=f"Единицы: {unit}", callback_data="settings:unit")
    b.button(text=f"Шаг веса по умолчанию: {default_weight_step}", callback_data="settings:step")
    b.button(text="Вес тела", callback_data="settings:bodyweight")
    b.button(text=f"Формула 1ПМ: {formula}", callback_data="settings:formula")
    b.button(
        text=f"Скрывать разминку: {'да' if hide_warmups else 'нет'}",
        callback_data="settings:hide_warmups",
    )
    b.button(text="📤 Экспорт CSV", callback_data="settings:export")
    b.adjust(1)
    return b.as_markup()


def cancel_keyboard(cb: str = "cancel") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data=cb)
    return b.as_markup()
