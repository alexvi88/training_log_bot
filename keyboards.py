"""Inline keyboard builders. Callback data uses a short `prefix:arg` scheme."""

import datetime as dt

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def main_menu(has_active_workout: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if has_active_workout:
        b.button(text="▶️ ПРОДОЛЖИТЬ ТРЕНИРОВКУ", callback_data="menu:resume_workout")
    else:
        b.button(text="🏋️ НАЧАТЬ ТРЕНИРОВКУ", callback_data="menu:start_workout")
    b.button(text="📈 Прогресс", callback_data="menu:progress")
    b.button(text="📚 История", callback_data="menu:history")
    b.button(text="⚙️ Упражнения", callback_data="menu:exercises")
    b.button(text="🔧 Настройки", callback_data="menu:settings")
    b.adjust(1, 2, 2)
    return b.as_markup()


def groups_keyboard(
    groups,
    prefix: str,
    extra_buttons: list[tuple[str, str]] | None = None,
    show_all: bool = False,
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for g in groups:
        b.button(text=g["name"], callback_data=f"{prefix}:grp:{g['id']}")
    if show_all:
        b.button(text="📋 Все", callback_data=f"{prefix}:grp:all")
    for text, cb in extra_buttons or []:
        b.button(text=text, callback_data=cb)
    b.adjust(2)
    return b.as_markup()


def numbered_list(names: list[str]) -> str:
    """Render names as "1. foo\n2. bar..." for use alongside a numbered_buttons row."""
    return "\n".join(f"{i + 1}. {name}" for i, name in enumerate(names))


def numbered_buttons(items: list[tuple[str, str]], per_row: int = 5) -> list[list[InlineKeyboardButton]]:
    """items: list of (callback_data, _) pairs, numbered 1..N and chunked into balanced rows.

    Numbers avoid the Telegram button-text truncation that long exercise/template
    names hit; the names themselves are shown as a numbered list in the message text.
    Rows are sized evenly (e.g. 6 items -> 3+3, not 5+1) so the last row never has
    a single stray button.
    """
    buttons = [
        InlineKeyboardButton(text=str(i + 1), callback_data=cb)
        for i, (cb, _) in enumerate(items)
    ]
    n = len(buttons)
    if n == 0:
        return []
    rows = -(-n // per_row)  # ceil(n / per_row)
    base, extra = divmod(n, rows)
    result = []
    i = 0
    for r in range(rows):
        size = base + 1 if r < extra else base
        result.append(buttons[i:i + size])
        i += size
    return result


def exercises_keyboard(
    exercises,
    prefix: str,
    show_new_button: bool = True,
    back_cb: str = "back",
    page: int = 0,
    has_next: bool = False,
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    items = [(f"{prefix}:ex:{ex['id']}", ex["display_name"]) for ex in exercises]
    for row in numbered_buttons(items):
        b.row(*row)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"{prefix}:page:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"{prefix}:page:{page + 1}"))
    if nav:
        b.row(*nav)
    if show_new_button:
        b.row(InlineKeyboardButton(text="➕ Новое упражнение", callback_data=f"{prefix}:new"))
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"{prefix}:{back_cb}"))
    return b.as_markup()


def new_exercise_entry_keyboard(prefix: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📋 Выбрать из шаблона", callback_data=f"{prefix}:templates")
    b.button(text="❌ Отмена", callback_data=f"{prefix}:cancel")
    b.adjust(1)
    return b.as_markup()


def templates_keyboard(templates, prefix: str, back_cb: str = "back") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    items = [(f"{prefix}:tpl:{t['id']}", t["display_name"]) for t in templates]
    for row in numbered_buttons(items):
        b.row(*row)
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"{prefix}:{back_cb}"))
    return b.as_markup()


def yes_no_keyboard(yes_cb: str, no_cb: str, yes_text: str = "Да", no_text: str = "Нет") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=yes_text, callback_data=yes_cb)
    b.button(text=no_text, callback_data=no_cb)
    b.adjust(2)
    return b.as_markup()


def logging_keyboard(
    open_items: list[tuple[int, str]], active_id: int | None, has_sets: bool = True
) -> InlineKeyboardMarkup:
    """Set-logging keyboard: tabs to switch between exercises open in parallel, plus controls.

    Weight/reps are typed as plain text (e.g. "100 8") — this keyboard only holds
    navigation/utility actions, not numeric input, to keep it short.
    """
    b = InlineKeyboardBuilder()
    if len(open_items) > 1:
        for ex_id, name in open_items:
            text = ("▶ " if ex_id == active_id else "") + name
            b.row(InlineKeyboardButton(text=text, callback_data=f"live:switch:{ex_id}"))
    if has_sets:
        b.row(InlineKeyboardButton(text="↩️ Удалить последний", callback_data="live:undo"))
    b.row(InlineKeyboardButton(text="✅ Закончить упражнение", callback_data="live:finish_exercise"))
    b.row(InlineKeyboardButton(text="➕ Суперсет", callback_data="live:add_exercise"))
    return b.as_markup()


def exercise_picker_entry_keyboard(has_planned: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if has_planned:
        b.button(text="▶️ Следующее по шаблону", callback_data="live:next_planned")
    b.button(text="➕ Упражнение", callback_data="live:add_exercise")
    b.button(text="🏁 Завершить тренировку", callback_data="live:finish_workout")
    b.adjust(1)
    return b.as_markup()


def finish_workout_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📝 Добавить заметку", callback_data="finish:note")
    b.button(text="✅ Без заметки", callback_data="finish:skip_note")
    b.button(text="❌ Отмена", callback_data="live:cancel_finish")
    b.adjust(1)
    return b.as_markup()


def progress_back_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data="prog:groups")
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
    b.row(InlineKeyboardButton(text="⬅️ Главное меню", callback_data="hist:menu"))
    return b.as_markup()


def history_item_keyboard(workout_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Редактировать", callback_data=f"hist:edit:{workout_id}")
    b.button(text="🗑 Удалить", callback_data=f"hist:del:{workout_id}")
    b.button(text="⬅️ К списку", callback_data="hist:back")
    b.adjust(1)
    return b.as_markup()


def settings_keyboard(unit: str, formula: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=f"Единицы: {unit}", callback_data="settings:unit")
    b.button(text=f"Формула 1ПМ: {formula}", callback_data="settings:formula")
    b.button(text="📤 Экспорт CSV", callback_data="settings:export")
    b.button(text="📥 Импорт CSV", callback_data="settings:import")
    b.button(text="🗓 Добавить прошлые тренировки", callback_data="menu:backfill_workout")
    b.button(text="⬅️ Назад", callback_data="settings:back")
    b.adjust(1)
    return b.as_markup()


def cancel_keyboard(cb: str = "cancel") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data=cb)
    return b.as_markup()


def date_quick_keyboard(prefix: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    today = dt.date.today()
    quick_dates = [
        ("Сегодня", today),
        ("Вчера", today - dt.timedelta(days=1)),
        ("Позавчера", today - dt.timedelta(days=2)),
    ]
    for label, d in quick_dates:
        b.button(text=label, callback_data=f"{prefix}:date:{d.isoformat()}")
    b.button(text="❌ Отмена", callback_data=f"{prefix}:cancel")
    b.adjust(3, 1)
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
    items = [(f"{prefix}:pick:{ex['id']}", ex["display_name"]) for ex in candidates[:6]]
    for row in numbered_buttons(items):
        b.row(*row)
    b.row(InlineKeyboardButton(text=f"➕ Создать «{name}»", callback_data=f"{prefix}:create"))
    b.row(InlineKeyboardButton(text="❌ Отменить весь ввод", callback_data=f"{prefix}:cancelall"))
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


def bodyweight_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="➕ Записать вес", callback_data="bw:add")
    b.button(text="📈 Динамика", callback_data="bw:chart")
    b.button(text="⬅️ Назад", callback_data="bw:back")
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
