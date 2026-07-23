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


def main_menu(has_active_workout: bool, can_repeat_last: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    top = 1  # buttons on the first (full-width) row(s)
    if has_active_workout:
        b.button(text="▶️ ПРОДОЛЖИТЬ ТРЕНИРОВКУ", callback_data="menu:resume_workout")
    else:
        b.button(text="🏋️ НАЧАТЬ ТРЕНИРОВКУ", callback_data="menu:start_workout")
        # A one-tap re-run of the last session — the most common pattern for
        # people who train A/B without keeping a saved program.
        if can_repeat_last:
            b.button(text="🔁 Повторить прошлую", callback_data="menu:repeat_last")
            top = 2
    b.button(text="📈 Прогресс", callback_data="menu:progress")
    b.button(text="📚 История", callback_data="menu:history")
    b.button(text="🏆 Зал славы", callback_data="menu:hall")
    b.button(text="⚙️ Упражнения", callback_data="menu:exercises")
    b.button(text="🗂 Программы", callback_data="rt:manage")
    b.button(text="⚖️ Дневник веса", callback_data="menu:bodyweight")
    b.button(text="🔧 Настройки", callback_data="menu:settings")
    # first row(s): start/repeat; then Прогресс·История, Зал славы·Упражнения,
    # Программы·Дневник, Настройки.
    b.adjust(*([1] * top), 2, 2, 2, 1)
    return b.as_markup()


def hall_of_fame_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🏅 Достижения", callback_data="menu:achievements")
    b.button(text="⬅️ Главное меню", callback_data="hist:menu")
    b.adjust(1)
    return b.as_markup()


def ai_trainer_keyboard(has_active_workout: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Меню", callback_data="ai:menu")
    if has_active_workout:
        b.button(text="🏋️ К тренировке", callback_data="ai:resume_workout")
        b.adjust(2)
    else:
        b.adjust(1)
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
    b.adjust(2)
    for text, cb in extra_buttons or []:
        b.row(InlineKeyboardButton(text=text, callback_data=cb))
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
    b.button(text="📋 Выбрать из шаблонов", callback_data=f"{prefix}:templates")
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


def template_preview_keyboard(template_id: int, prefix: str = "exm", back_cb: str | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="➕ Добавить", callback_data=f"{prefix}:tpladd:{template_id}"))
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb or f"{prefix}:templates"))
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

    The keyboard is deliberately kept short: the repeat/undo pair share one row,
    and the per-exercise note button (📝) rides along on an existing row rather
    than adding a new one, so logging many sets never grows a wall of buttons.
    """
    b = InlineKeyboardBuilder()
    if len(open_items) > 1:
        for ex_id, name in open_items:
            text = ("▶ " if ex_id == active_id else "") + name
            b.row(InlineKeyboardButton(text=text, callback_data=f"live:switch:{ex_id}"))
    note_btn = (
        InlineKeyboardButton(text="📝", callback_data=f"live:note:{active_id}")
        if active_id is not None
        else None
    )
    if has_sets:
        row = [
            InlineKeyboardButton(text="🔁 Повторить", callback_data="live:repeat"),
            InlineKeyboardButton(text="↩️ Удалить", callback_data="live:undo"),
        ]
        if note_btn is not None:
            row.append(note_btn)
        b.row(*row)
    elif active_id is not None:
        row = [InlineKeyboardButton(text="ℹ️ Карточка упражнения", callback_data=f"live:card:{active_id}")]
        if note_btn is not None:
            row.append(note_btn)
        b.row(*row)
    b.row(InlineKeyboardButton(text="✅ Закончить упражнение", callback_data="live:finish_exercise"))
    b.row(InlineKeyboardButton(text="➕ Суперсет", callback_data="live:add_exercise"))
    return b.as_markup()


def exercise_card_back_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="◀️ Назад к тренировке", callback_data="live:card_back"))
    return b.as_markup()


def exercise_picker_entry_keyboard(
    has_planned: bool = False, suggested: tuple[int, str] | None = None, is_empty: bool = False
) -> InlineKeyboardMarkup:
    """suggested: (exercise_id, display_name) of what usually follows the just-finished exercise.

    is_empty: nothing logged in this workout yet — "finish" would just discard it
    (see live_finish_workout), so the button reads as an exit rather than a finish.
    """
    b = InlineKeyboardBuilder()
    if has_planned:
        b.button(text="▶️ Следующее по шаблону", callback_data="live:next_planned")
    b.button(text="➕ Упражнение", callback_data="live:add_exercise")
    if suggested is not None:
        ex_id, _name = suggested
        b.button(text="⏭ Как в прошлый раз", callback_data=f"live:suggest:{ex_id}")
    if is_empty:
        b.button(text="⬅️ В меню", callback_data="live:finish_workout")
    else:
        b.button(text="🏁 Завершить тренировку", callback_data="live:finish_workout")
    b.adjust(1)
    return b.as_markup()


def routines_manage_keyboard(routines, has_workouts: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for r in routines:
        b.button(text=r["name"], callback_data=f"rt:view:{r['id']}")
    if has_workouts:
        b.button(text="➕ Из тренировки", callback_data="rt:pickw:page:0")
    b.button(text="✨ Готовые программы", callback_data="rt:programs")
    b.button(text="⬅️ Главное меню", callback_data="rt:menu")
    b.adjust(1)
    return b.as_markup()


def routine_source_picker_keyboard(workouts, page: int, has_next: bool) -> InlineKeyboardMarkup:
    """Pick a past finished workout to snapshot into a new routine."""
    b = InlineKeyboardBuilder()
    for w in workouts:
        b.button(text=w["label"], callback_data=f"rt:pickw:item:{w['id']}")
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"rt:pickw:page:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"rt:pickw:page:{page + 1}"))
    b.adjust(1)
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="rt:manage"))
    return b.as_markup()


def programs_catalog_keyboard(programs) -> InlineKeyboardMarkup:
    """List of ready-made programs; picking one opens its detail screen."""
    b = InlineKeyboardBuilder()
    for p in programs:
        b.button(text=p["name"], callback_data=f"rt:prog:{p['key']}")
    b.button(text="⬅️ К программам", callback_data="rt:manage")
    b.adjust(1)
    return b.as_markup()


def program_detail_keyboard(program_key: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="➕ Добавить себе", callback_data=f"rt:progadd:{program_key}")
    b.button(text="⬅️ К каталогу", callback_data="rt:programs")
    b.adjust(1)
    return b.as_markup()


def routine_detail_keyboard(routine_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="▶️ Начать тренировку", callback_data=f"rt:start:{routine_id}")
    b.button(text="✏️ Переименовать", callback_data=f"rt:rename:{routine_id}")
    b.button(text="🗑 Удалить", callback_data=f"rt:delask:{routine_id}")
    b.button(text="⬅️ К списку", callback_data="rt:manage")
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


def finish_date_mismatch_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Да, всё верно", callback_data="finconfirm:keep")
    b.button(text="📅 Изменить дату", callback_data="finconfirm:changedate")
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
    b.row(InlineKeyboardButton(text="🗂 Карточка упражнения", callback_data=f"prog:card:{exercise_id}"))
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=_progress_back_cb(exercise_id, origin)))
    return b.as_markup()


PROGRESS_PERIODS = [(8, "8"), (20, "20"), (9999, "Все")]
DEFAULT_PROGRESS_LIMIT = PROGRESS_PERIODS[0][0]


def progress_chart_keyboard(exercise_id: int, limit: int, origin: str = "all") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for value, label in PROGRESS_PERIODS:
        text = f"• {label} •" if value == limit else label
        b.button(text=text, callback_data=f"prog:per:{exercise_id}:{value}:{origin}")
    b.adjust(len(PROGRESS_PERIODS))
    b.row(InlineKeyboardButton(text="🗂 Карточка упражнения", callback_data=f"prog:card:{exercise_id}"))
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=_progress_back_cb(exercise_id, origin)))
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


def admin_ai_users_keyboard(users, page: int, has_next: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for u in users:
        name = f"@{u['username']}" if u["username"] else str(u["telegram_id"])
        b.button(text=f"{name} ({u['ai_message_count']})", callback_data=f"admin:aiu:{u['telegram_id']}")
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin:aip:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"admin:aip:{page + 1}"))
    b.adjust(1)
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text="⬅️ Главное меню", callback_data="admin:menu"))
    return b.as_markup()


def admin_ai_dialogs_back_keyboard(page: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ К списку", callback_data=f"admin:aib:{page}")
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
    unit: str, formula: str, pushes_enabled: bool, ai_comments_enabled: bool, progression_enabled: bool
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=f"Единицы: {unit}", callback_data="settings:unit")
    b.button(text=f"Формула 1ПМ: {formula}", callback_data="settings:formula")
    progression_label = (
        "🎯 Подсказки прогрессии: вкл" if progression_enabled else "🎯 Подсказки прогрессии: выкл"
    )
    b.button(text=progression_label, callback_data="settings:progression")
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


# Chart window options for the weight diary (weeks; 0 = all history).
BODYWEIGHT_PERIODS = [(8, "8 нед"), (26, "26 нед"), (0, "Всё")]
DEFAULT_BODYWEIGHT_WEEKS = 0


def bodyweight_keyboard(has_logs: bool, weeks: int = 0, show_periods: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if has_logs:
        b.row(InlineKeyboardButton(text="↩️ Удалить последнюю", callback_data="bw:undo"))
    if show_periods:
        period_buttons = [
            InlineKeyboardButton(
                text=f"• {label} •" if value == weeks else label, callback_data=f"bw:period:{value}"
            )
            for value, label in BODYWEIGHT_PERIODS
        ]
        b.row(*period_buttons)
    b.row(InlineKeyboardButton(text="⬅️ Главное меню", callback_data="bw:menu"))
    return b.as_markup()


def cancel_keyboard(cb: str = "cancel") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data=cb)
    return b.as_markup()


def feedback_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Готово", callback_data="feedback:done")
    return b.as_markup()


def push_cta_keyboard() -> InlineKeyboardMarkup:
    """Attached to daily-rotation push notifications: routes straight into starting a workout."""
    b = InlineKeyboardBuilder()
    b.button(text="▶ Начать тренировку", callback_data="menu:start_workout")
    return b.as_markup()


_CAL_WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_MONTHS_RU = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]


def calendar_keyboard(prefix: str, year: int, month: int, today: dt.date | None = None) -> InlineKeyboardMarkup:
    """Month grid for picking a past date without typing дд.мм.гггг.

    Day taps emit ``{prefix}:date:{iso}`` — the same callback the quick buttons
    already use, so existing per-flow date handlers catch calendar picks for
    free. Month arrows emit ``{prefix}:cal:{year}-{month}`` (re-render only),
    blanks and labels emit ``{prefix}:noop``. Future days and future months are
    not selectable — a past workout can't be dated ahead of today.
    """
    today = today or dt.date.today()
    b = InlineKeyboardBuilder()

    first = dt.date(year, month, 1)
    prev_last = first - dt.timedelta(days=1)
    next_first = dt.date(year + 1, 1, 1) if month == 12 else dt.date(year, month + 1, 1)
    can_next = next_first <= dt.date(today.year, today.month, 1)
    b.row(
        InlineKeyboardButton(text="‹", callback_data=f"{prefix}:cal:{prev_last.year}-{prev_last.month}"),
        InlineKeyboardButton(text=f"{_MONTHS_RU[month - 1]} {year}", callback_data=f"{prefix}:noop"),
        InlineKeyboardButton(
            text="›" if can_next else " ",
            callback_data=f"{prefix}:cal:{next_first.year}-{next_first.month}" if can_next else f"{prefix}:noop",
        ),
    )
    b.row(*[InlineKeyboardButton(text=w, callback_data=f"{prefix}:noop") for w in _CAL_WEEKDAYS])

    cells = [InlineKeyboardButton(text=" ", callback_data=f"{prefix}:noop") for _ in range(first.weekday())]
    days_in_month = (next_first - first).days
    for d in range(1, days_in_month + 1):
        date = dt.date(year, month, d)
        if date > today:
            cells.append(InlineKeyboardButton(text="·", callback_data=f"{prefix}:noop"))
        else:
            label = f"·{d}·" if date == today else str(d)
            cells.append(InlineKeyboardButton(text=label, callback_data=f"{prefix}:date:{date.isoformat()}"))
    while len(cells) % 7:
        cells.append(InlineKeyboardButton(text=" ", callback_data=f"{prefix}:noop"))
    for i in range(0, len(cells), 7):
        b.row(*cells[i : i + 7])

    yesterday = today - dt.timedelta(days=1)
    b.row(
        InlineKeyboardButton(text="Сегодня", callback_data=f"{prefix}:date:{today.isoformat()}"),
        InlineKeyboardButton(text="Вчера", callback_data=f"{prefix}:date:{yesterday.isoformat()}"),
    )
    b.row(InlineKeyboardButton(text="❌ Отмена", callback_data=f"{prefix}:cancel"))
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
