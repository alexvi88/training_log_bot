"""Inline keyboard builders. Callback data uses a short `prefix:arg` scheme."""

import datetime as dt

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Persistent reply-keyboard buttons, always visible under the input field.
BTN_WORKOUT = "Тренировка"
BTN_MENU = "Меню"
BTN_AI = "AI-тренер"

# Bump whenever persistent_menu()'s button set changes so every user gets the
# new layout next time cmd_start runs (see users.reply_keyboard_version).
PERSISTENT_MENU_VERSION = 2


def persistent_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_WORKOUT), KeyboardButton(text=BTN_MENU), KeyboardButton(text=BTN_AI)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def main_menu(has_active_workout: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if has_active_workout:
        b.button(text="▶️ ПРОДОЛЖИТЬ ТРЕНИРОВКУ", callback_data="menu:resume_workout")
    else:
        b.button(text="🏋️ НАЧАТЬ ТРЕНИРОВКУ", callback_data="menu:start_workout")
    b.button(text="📈 Прогресс", callback_data="menu:progress")
    b.button(text="📚 История", callback_data="menu:history")
    b.button(text="⚙️ Упражнения", callback_data="menu:exercises")
    b.button(text="💪 Объём/нед", callback_data="menu:volume")
    b.button(text="⚖️ Вес тела", callback_data="menu:bodyweight")
    b.button(text="🔧 Настройки", callback_data="menu:settings")
    b.adjust(1, 2, 2, 2)
    return b.as_markup()


def ai_trainer_keyboard(has_active_workout: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🗑 Новый диалог", callback_data="ai:reset")
    b.button(text="⬅️ Меню", callback_data="ai:menu")
    if has_active_workout:
        b.button(text="🏋️ К тренировке", callback_data="ai:resume_workout")
        b.adjust(1, 2)
    else:
        b.adjust(1, 1)
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


def named_buttons(items: list[tuple[str, str]]) -> list[list[InlineKeyboardButton]]:
    """items: list of (callback_data, name) pairs. One full-name button per row."""
    return [[InlineKeyboardButton(text=name, callback_data=cb)] for cb, name in items]


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
    for row in named_buttons(items):
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
    for row in named_buttons(items):
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


def exercise_picker_entry_keyboard(
    has_planned: bool = False, suggested: tuple[int, str] | None = None
) -> InlineKeyboardMarkup:
    """suggested: (exercise_id, display_name) of what usually follows the just-finished exercise."""
    b = InlineKeyboardBuilder()
    if has_planned:
        b.button(text="▶️ Следующее по шаблону", callback_data="live:next_planned")
    b.button(text="➕ Упражнение", callback_data="live:add_exercise")
    if suggested is not None:
        ex_id, _name = suggested
        b.button(text="⏭ Как в прошлый раз", callback_data=f"live:suggest:{ex_id}")
    b.button(text="🏁 Завершить тренировку", callback_data="live:finish_workout")
    b.adjust(1)
    return b.as_markup()


def stale_workout_keyboard(workout_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Завершить задним числом", callback_data=f"stale:finish:{workout_id}")
    b.button(text="🗑 Удалить", callback_data=f"stale:delete:{workout_id}")
    b.adjust(1)
    return b.as_markup()


def finish_workout_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📝 Добавить заметку", callback_data="finish:note")
    b.button(text="✅ Без заметки", callback_data="finish:skip_note")
    b.button(text="❌ Отмена", callback_data="live:cancel_finish")
    b.adjust(1)
    return b.as_markup()


def _progress_back_cb(exercise_id: int, origin: str) -> str:
    """Where "⬅️ Назад" from a progress screen should go.

    `origin` is either "m" (opened from the exercise-detail card in "⚙️
    Упражнения" — back should return to that same card) or a group token
    ("all" or a muscle-group id, as produced by prog:grp:) — back should
    return to that group's exercise list, not all the way up to the
    muscle-group picker.
    """
    if origin == "m":
        return f"exm:ex:{exercise_id}"
    return f"prog:grp:{origin}"


def progress_back_keyboard(exercise_id: int = 0, origin: str = "all") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data=_progress_back_cb(exercise_id, origin))
    return b.as_markup()


PROGRESS_PERIODS = [(8, "8"), (20, "20"), (9999, "Все")]
DEFAULT_PROGRESS_LIMIT = PROGRESS_PERIODS[0][0]


def progress_chart_keyboard(exercise_id: int, limit: int, origin: str = "all") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for value, label in PROGRESS_PERIODS:
        text = f"• {label} •" if value == limit else label
        b.button(text=text, callback_data=f"prog:per:{exercise_id}:{value}:{origin}")
    b.button(text="⬅️ Назад", callback_data=_progress_back_cb(exercise_id, origin))
    b.adjust(len(PROGRESS_PERIODS), 1)
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
    b.row(InlineKeyboardButton(text="🗓 Добавить прошлые тренировки", callback_data="menu:backfill_workout"))
    b.row(InlineKeyboardButton(text="⬅️ Главное меню", callback_data="hist:menu"))
    return b.as_markup()


def history_item_keyboard(workout_id: int, show_ai_button: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🖼 Поделиться картинкой", callback_data=f"hist:card:{workout_id}")
    if show_ai_button:
        b.button(text="🤖 Комментарий AI-тренера", callback_data=f"ai:comment:{workout_id}")
    b.button(text="✏️ Редактировать", callback_data=f"hist:edit:{workout_id}")
    b.button(text="🗑 Удалить", callback_data=f"hist:del:{workout_id}")
    b.button(text="⬅️ К списку", callback_data="hist:back")
    b.adjust(1)
    return b.as_markup()


def workout_card_keyboard(workout_id: int, show_ai_button: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🖼 Поделиться картинкой", callback_data=f"hist:card:{workout_id}")
    if show_ai_button:
        b.button(text="🤖 Комментарий AI-тренера", callback_data=f"ai:comment:{workout_id}")
    b.adjust(1)
    return b.as_markup()


def admin_users_keyboard(users, page: int, has_next: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for u in users:
        name = f"@{u['username']}" if u["username"] else str(u["telegram_id"])
        b.button(text=f"{name} ({u['workout_count']})", callback_data=f"admin:u:{u['telegram_id']}")
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin:up:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"admin:up:{page + 1}"))
    b.adjust(1)
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text="⬅️ Главное меню", callback_data="admin:menu"))
    return b.as_markup()


def admin_history_list_keyboard(
    workouts, target_user_id: int, page: int, has_next: bool
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for w in workouts:
        b.button(text=w["label"], callback_data=f"admin:hi:{target_user_id}:{w['id']}")
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin:hp:{target_user_id}:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"admin:hp:{target_user_id}:{page + 1}"))
    b.adjust(1)
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text="⬅️ К пользователям", callback_data="admin:back"))
    return b.as_markup()


def admin_history_item_keyboard(target_user_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ К списку", callback_data=f"admin:hb:{target_user_id}")
    b.adjust(1)
    return b.as_markup()


def admin_pushes_keyboard(page: int, has_next: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin:pp:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"admin:pp:{page + 1}"))
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text="⬅️ Главное меню", callback_data="admin:menu"))
    return b.as_markup()


def settings_keyboard(
    unit: str, formula: str, pushes_enabled: bool, ai_comments_enabled: bool
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=f"Единицы: {unit}", callback_data="settings:unit")
    b.button(text=f"Формула 1ПМ: {formula}", callback_data="settings:formula")
    pushes_label = "🔔 Пуши: включены" if pushes_enabled else "🔕 Пуши: выключены"
    b.button(text=pushes_label, callback_data="settings:pushes")
    ai_label = (
        "🤖 Комментарии AI-тренера: включены"
        if ai_comments_enabled
        else "🤖 Комментарии AI-тренера: выключены"
    )
    b.button(text=ai_label, callback_data="settings:ai_comments")
    b.button(text="📤 Экспорт CSV", callback_data="settings:export")
    b.button(text="📥 Импорт CSV", callback_data="settings:import")
    b.button(text="⬅️ Назад", callback_data="settings:back")
    b.adjust(1)
    return b.as_markup()


def weekly_volume_keyboard(week_offset: int) -> InlineKeyboardMarkup:
    """week_offset: 0 = current week, 1 = last week, … (older to the left)."""
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Прошлая неделя", callback_data=f"vol:wk:{week_offset + 1}")
    if week_offset > 0:
        b.button(text="Следующая ➡️", callback_data=f"vol:wk:{week_offset - 1}")
    b.button(text="⬅️ Главное меню", callback_data="vol:menu")
    b.adjust(2, 1) if week_offset > 0 else b.adjust(1, 1)
    return b.as_markup()


def bodyweight_keyboard(has_logs: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="➕ Записать вес", callback_data="bw:add")
    if has_logs:
        b.button(text="↩️ Удалить последнюю", callback_data="bw:undo")
    b.button(text="⬅️ Главное меню", callback_data="bw:menu")
    b.adjust(1)
    return b.as_markup()


def cancel_keyboard(cb: str = "cancel") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data=cb)
    return b.as_markup()


def push_cta_keyboard() -> InlineKeyboardMarkup:
    """Attached to daily-rotation push notifications: routes straight into starting a workout."""
    b = InlineKeyboardBuilder()
    b.button(text="▶ Начать тренировку", callback_data="menu:start_workout")
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
    for row in named_buttons(items):
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


def csv_column_options_keyboard(headers: list[str], prefix: str, allow_skip: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for idx, header in enumerate(headers):
        b.button(text=header, callback_data=f"{prefix}:{idx}")
    if allow_skip:
        b.button(text="— нет такой колонки —", callback_data=f"{prefix}:skip")
    b.adjust(1)
    return b.as_markup()
