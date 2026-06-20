"""Inline keyboard builders. Callback data uses a short `prefix:arg` scheme."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def main_menu(has_active_workout: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if has_active_workout:
        b.button(text="▶️ Продолжить тренировку", callback_data="menu:resume_workout")
    else:
        b.button(text="🏋️ Начать тренировку", callback_data="menu:start_workout")
    b.button(text="➕ Прошлая тренировка", callback_data="menu:backfill_workout")
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


def logging_keyboard(
    open_items: list[tuple[int, str]], active_id: int | None, can_repeat: bool
) -> InlineKeyboardMarkup:
    """Set-logging keyboard: tabs to switch between exercises open in parallel, plus controls.

    Weight/reps are typed as plain text (e.g. "100 8") — this keyboard only holds
    navigation/utility actions, not numeric input, to keep it short.
    """
    b = InlineKeyboardBuilder()
    if len(open_items) > 1:
        tabs = [
            InlineKeyboardButton(
                text=("▶ " if ex_id == active_id else "") + name,
                callback_data=f"live:switch:{ex_id}",
            )
            for ex_id, name in open_items
        ]
        for i in range(0, len(tabs), 3):
            b.row(*tabs[i:i + 3])
    controls = []
    if can_repeat:
        controls.append(InlineKeyboardButton(text="🔁 Повторить", callback_data="live:repeat"))
    controls.append(InlineKeyboardButton(text="➕ Упражнение", callback_data="live:add_exercise"))
    b.row(*controls)
    b.row(
        InlineKeyboardButton(text="↩️ Удалить последний", callback_data="live:undo"),
        InlineKeyboardButton(text="✅ Закончить упражнение", callback_data="live:finish_exercise"),
    )
    return b.as_markup()


def exercise_picker_entry_keyboard(has_planned: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if has_planned:
        b.button(text="▶️ Следующее по шаблону", callback_data="live:next_planned")
    b.button(text="➕ Упражнение", callback_data="live:add_exercise")
    b.button(text="🏁 Завершить тренировку", callback_data="live:finish_workout")
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
    b.button(text="✏️ Редактировать", callback_data=f"hist:edit:{workout_id}")
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
    b.button(text="📥 Импорт CSV", callback_data="settings:import")
    b.adjust(1)
    return b.as_markup()


def cancel_keyboard(cb: str = "cancel") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data=cb)
    return b.as_markup()


def date_quick_keyboard(prefix: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Сегодня", callback_data=f"{prefix}:date:today")
    b.button(text="Вчера", callback_data=f"{prefix}:date:yesterday")
    b.button(text="❌ Отмена", callback_data=f"{prefix}:cancel")
    b.adjust(2, 1)
    return b.as_markup()


def confirm_cancel_keyboard(
    confirm_cb: str, cancel_cb: str, confirm_text: str = "✅ Сохранить", cancel_text: str = "❌ Отмена"
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=confirm_text, callback_data=confirm_cb)
    b.button(text=cancel_text, callback_data=cancel_cb)
    b.adjust(1)
    return b.as_markup()


def exercise_resolve_keyboard(candidates, name: str, prefix: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for ex in candidates[:6]:
        b.button(text=ex["display_name"], callback_data=f"{prefix}:pick:{ex['id']}")
    b.button(text=f"➕ Создать «{name}»", callback_data=f"{prefix}:create")
    b.button(text="❌ Отменить весь ввод", callback_data=f"{prefix}:cancelall")
    b.adjust(1)
    return b.as_markup()


def edit_workout_keyboard(sets_rows, add_buttons) -> InlineKeyboardMarkup:
    """sets_rows: list of (set_id, label). add_buttons: list of (block_id, exercise_id, label)."""
    b = InlineKeyboardBuilder()
    for set_id, label in sets_rows:
        b.button(text=label, callback_data=f"editw:set:{set_id}")
    for block_id, exercise_id, label in add_buttons:
        b.button(text=f"➕ Сет — {label}", callback_data=f"editw:addset:{block_id}:{exercise_id}")
    b.button(text="📅 Изменить дату", callback_data="editw:date")
    b.button(text="✅ Готово", callback_data="editw:done")
    b.adjust(1)
    return b.as_markup()


def set_actions_keyboard(set_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Изменить вес/повторы", callback_data=f"editw:editset:{set_id}")
    b.button(text="🗑 Удалить сет", callback_data=f"editw:delset:{set_id}")
    b.button(text="⬅️ Назад", callback_data="editw:back")
    b.adjust(1)
    return b.as_markup()


def csv_column_options_keyboard(headers: list[str], prefix: str, allow_skip: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for idx, header in enumerate(headers):
        b.button(text=header, callback_data=f"{prefix}:{idx}")
    if allow_skip:
        b.button(text="— нет такой колонки —", callback_data=f"{prefix}:skip")
    b.adjust(1)
    return b.as_markup()
