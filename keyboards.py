"""Inline keyboard builders. Callback data uses a short `prefix:arg` scheme."""

import datetime as dt
from typing import Any, Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

import formatting

# Сколько символов названия влезает в кнопку под ответом AI-тренера, не
# растягивая клавиатуру и не обрезаясь самим Telegram.
AI_MENTION_LABEL_LIMIT = 32


def _shorten_label(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

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


def main_menu(has_active_workout: bool, show_quick_log: bool = False) -> InlineKeyboardMarkup:
    """show_quick_log: offered while the diary is still empty. A first-time user
    has nothing to look at and a whole training history behind them — letting
    them type one line of it beats walking the picker for the first record."""
    b = InlineKeyboardBuilder()
    if has_active_workout:
        b.button(text="▶️ ПРОДОЛЖИТЬ ТРЕНИРОВКУ", callback_data="menu:resume_workout")
    else:
        b.button(text="🏋️ НАЧАТЬ ТРЕНИРОВКУ", callback_data="menu:start_workout")
    # Also on the persistent keyboard, but the menu is what a new user reads to
    # find out what the bot does — and "menu:ai" had a handler no keyboard sent.
    if show_quick_log:
        b.button(text="✍️ Записать прошлую тренировку", callback_data="menu:quicklog")
    b.button(text="🤖 AI-тренер", callback_data="menu:ai")
    b.button(text="📈 Прогресс", callback_data="menu:progress")
    b.button(text="📚 История", callback_data="menu:history")
    b.button(text="⚙️ Упражнения", callback_data="menu:exercises")
    b.button(text="🗂 Программы", callback_data="rt:manage")
    b.button(text="⚖️ Дневник веса", callback_data="menu:bodyweight")
    b.button(text="🍽 Дневник питания", callback_data="menu:food")
    b.button(text="🏆 Достижения", callback_data="menu:achievements")
    b.button(text="🔧 Настройки", callback_data="menu:settings")
    # start/resume, quick-log (if shown) and AI-тренер full width, then pairs:
    # Прогресс·История, Упражнения·Программы, Дневник веса·Дневник питания,
    # Достижения·Настройки.
    b.adjust(*([1, 1, 1] if show_quick_log else [1, 1]), 2, 2, 2, 2)
    return b.as_markup()


# Сколько кнопок-упоминаний показываем разом под ответом — дальше листаем
# стрелками, а не удлиняем клавиатуру (см. ai_trainer_keyboard).
AI_MENTION_PAGE_SIZE = 3


def ai_trainer_keyboard(
    has_active_workout: bool = False,
    exercises: Sequence[Any] = (),
    page: int = 0,
    program_name: str | None = None,
) -> InlineKeyboardMarkup:
    """`exercises` — то, что тренер упомянул в ответе (см. exercise_mentions), и
    свои упражнения, и ещё не добавленные из каталога — до
    exercise_mentions.MAX_MENTIONS_TOTAL штук. Своё ведёт прямо на карточку, а
    каталожное сначала добавляет его пользователю и потом открывает ту же
    карточку — прямая ссылка вела бы в никуда, раз упражнения ещё нет.
    Показываем по AI_MENTION_PAGE_SIZE штук за раз с постраничным ⬅️/➡️, если
    упоминаний больше — id всех упоминаний едут прямо в callback_data стрелок
    (см. handlers/ai_trainer.ai_mentions_page), отдельного состояния не нужно.

    `program_name` — название программы, которую тренер собрал в этом ответе
    (см. ai_trainer.propose_program): даёт самую верхнюю кнопку, ведущую на
    превью с составом и кнопкой сохранения. Сам черновик в callback_data не
    влезает и лежит в FSM, поэтому кнопка без параметров."""
    exercises = list(exercises)
    b = InlineKeyboardBuilder()
    b.button(text="🏠 Меню", callback_data="ai:menu")
    if has_active_workout:
        b.button(text="🏋️ К тренировке", callback_data="ai:resume_workout")
        b.adjust(2)
    else:
        b.adjust(1)
    nav = b.as_markup().inline_keyboard

    start = page * AI_MENTION_PAGE_SIZE
    page_exercises = exercises[start : start + AI_MENTION_PAGE_SIZE]
    # Кнопки упражнений — над навигацией и каждая своей строкой: названия
    # длинные, в паре Telegram их обрежет. Разные эмодзи — своё (📌) не спутать
    # с ещё не добавленным из каталога (📋).
    mention_rows = []
    for ex in page_exercises:
        if ex["is_template"]:
            emoji, callback_data = "📋", f"ai:tpladd:{ex['id']}"
        else:
            emoji, callback_data = "📌", f"ai:excard:{ex['id']}"
        mention_rows.append(
            [
                InlineKeyboardButton(
                    text=f"{emoji} {_shorten_label(ex['display_name'], AI_MENTION_LABEL_LIMIT)}",
                    callback_data=callback_data,
                )
            ]
        )

    page_nav = []
    if len(exercises) > AI_MENTION_PAGE_SIZE:
        ids = ",".join(str(ex["id"]) for ex in exercises)
        if page > 0:
            page_nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"ai:mpage:{page - 1}:{ids}"))
        if start + AI_MENTION_PAGE_SIZE < len(exercises):
            page_nav.append(InlineKeyboardButton(text="➡️", callback_data=f"ai:mpage:{page + 1}:{ids}"))
    page_nav_rows = [page_nav] if page_nav else []

    # Программа — над всем остальным: это то, ради чего пользователь и просил
    # ответ, а упоминания упражнений рядом с ней второстепенны.
    program_rows = []
    if program_name:
        program_rows.append(
            [
                InlineKeyboardButton(
                    text=f"📋 {_shorten_label(program_name, AI_MENTION_LABEL_LIMIT)}",
                    callback_data="ai:prog:view",
                )
            ]
        )

    return InlineKeyboardMarkup(
        inline_keyboard=program_rows + mention_rows + page_nav_rows + nav
    )


def ai_program_preview_keyboard() -> InlineKeyboardMarkup:
    """Превью программы, собранной тренером: сохранить или отказаться.

    Сохранение создаёт по программе на каждый её день (см.
    handlers/ai_trainer.ai_program_save), поэтому подпись говорит «добавить»,
    а не «сохранить программу» — в списке появится несколько строк.
    """
    b = InlineKeyboardBuilder()
    b.button(text="✅ Добавить себе", callback_data="ai:prog:save")
    b.button(text="❌ Не надо", callback_data="ai:prog:drop")
    b.adjust(1)
    return b.as_markup()


def ai_program_saved_keyboard() -> InlineKeyboardMarkup:
    """После сохранения программы — прямая дорога в её список, без возврата в меню."""
    b = InlineKeyboardBuilder()
    b.button(text="🗂 К программам", callback_data="rt:manage")
    b.adjust(1)
    return b.as_markup()


def groups_keyboard(
    groups,
    prefix: str,
    extra_buttons: list[tuple[str, str]] | None = None,
    show_all: bool = False,
    partner_buttons: list[tuple[int, str]] | None = None,
) -> InlineKeyboardMarkup:
    """partner_buttons: (exercise_id, display_name) pairs most often logged
    alongside the exercise the "➕ Суперсет" screen was opened from — a
    one-tap shortcut so a habitual pairing skips the group→list picker."""
    b = InlineKeyboardBuilder()
    for g in groups:
        b.button(text=formatting.format_group(g["name"]), callback_data=f"{prefix}:grp:{g['id']}")
    if show_all:
        b.button(text="📋 Все", callback_data=f"{prefix}:grp:all")
    b.adjust(2)
    # The partner shortcuts go on top, one full-width row each — a group name is
    # one short word and pairs up two to a row, but an exercise name squeezed
    # into that half-width column comes out as "triceps block - si…". They can't
    # just be b.row()'d first either: adjust(2) reflows every row already in the
    # builder, partner rows included, which is exactly how they ended up there.
    rows = [
        [InlineKeyboardButton(text=f"⚡ {name}", callback_data=f"{prefix}:partner:{ex_id}")]
        for ex_id, name in partner_buttons or []
    ]
    rows += b.export()
    rows += [[InlineKeyboardButton(text=text, callback_data=cb)] for text, cb in extra_buttons or []]
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
    templates=None,
) -> InlineKeyboardMarkup:
    """templates: catalog exercises (db.search_exercise_templates) to offer
    alongside the user's own matches, marked "📋" and routed through
    `{prefix}:tpladd:{id}` — the same fork-then-open callback the "📋 Выбрать
    из шаблонов" entry already uses, so tapping one behaves identically to
    picking it from the template browser instead of from search.
    """
    b = InlineKeyboardBuilder()
    items = [(f"{prefix}:ex:{ex['id']}", ex["display_name"]) for ex in exercises]
    items += [(f"{prefix}:tpladd:{t['id']}", f"📋 {t['display_name']}") for t in (templates or [])]
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


def new_exercise_entry_keyboard(prefix: str, show_templates: bool = True) -> InlineKeyboardMarkup:
    """show_templates=False when no muscle group is selected — the template
    browser lists one group's templates, so there'd be nothing to show."""
    b = InlineKeyboardBuilder()
    if show_templates:
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


def weight_confirm_keyboard() -> InlineKeyboardMarkup:
    """"That weight looks like a typo — record it?" before a suspicious set is
    written (see handlers.workout._weight_confirm_prompt). "Нет" throws the
    input away so the set can simply be retyped."""
    return yes_no_keyboard(
        "live:wconf:yes", "live:wconf:no", yes_text="✅ Да, записать", no_text="✏️ Исправить",
    )


def help_keyboard(expanded: bool) -> InlineKeyboardMarkup:
    """Toggle between the short /help screen and the full input reference
    (handlers.workout.help_toggle)."""
    b = InlineKeyboardBuilder()
    if expanded:
        b.button(text="⬆️ Свернуть", callback_data="help:less")
    else:
        b.button(text="⬇️ Ещё: RPE, заметки, правки", callback_data="help:more")
    return b.as_markup()


# A full-width tab fits roughly this many characters before Telegram clips the
# label itself.
_TAB_NAME_MAX_WIDE = 28

def _tab_label(name: str, limit: int) -> str:
    """A superset tab's label — shows the full exercise name, only cutting it
    down (word-boundary-aware, with an ellipsis) when it doesn't fit the tab."""
    name = name.strip()
    if len(name) <= limit:
        return name
    cut = name[:limit].rstrip()
    # Prefer a word boundary over slicing a word in half, when one is close enough.
    if " " in cut and len(cut.rsplit(" ", 1)[0]) >= limit // 2:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "…"


def logging_keyboard(
    open_items: list[tuple[int, str]],
    active_id: int | None,
    has_sets: bool = True,
) -> InlineKeyboardMarkup:
    """Set-logging keyboard: tabs to switch between exercises open in parallel, plus controls.

    Weight/reps are typed as plain text (e.g. "100 8") — this keyboard only holds
    navigation/utility actions, not numeric input, to keep it short.

    The "➕ Суперсет"/"📝 Заметка" row is always available; once a set is logged,
    "↩️ Удалить последний" appears directly above "✅ Закончить упражнение".
    """
    b = InlineKeyboardBuilder()
    if len(open_items) > 1:
        # One tab per row, whatever the size of the superset. Two half-width tabs
        # per row saved a line but cut every real exercise name down to a stub
        # ("triceps…"), which is the one thing the tab has to say; a full-width
        # row holds about twice the text and names them properly.
        for ex_id, name in open_items:
            # The ▶ marker eats width the label would otherwise get.
            text = (
                "▶ " + _tab_label(name, _TAB_NAME_MAX_WIDE - 2)
                if ex_id == active_id
                else _tab_label(name, _TAB_NAME_MAX_WIDE)
            )
            b.row(InlineKeyboardButton(text=text, callback_data=f"live:switch:{ex_id}"))
    top_row = [InlineKeyboardButton(text="➕ Суперсет", callback_data="live:add_exercise")]
    if active_id is not None:
        top_row.append(InlineKeyboardButton(text="📝 Заметка", callback_data=f"live:note:{active_id}"))
    if has_sets:
        b.row(*top_row)
        b.row(InlineKeyboardButton(text="↩️ Удалить последний", callback_data="live:undo"))
        b.row(InlineKeyboardButton(text="✅ Закончить упражнение", callback_data="live:finish_exercise"))
    else:
        b.row(*top_row)
        b.row(InlineKeyboardButton(text="✅ Закончить упражнение", callback_data="live:finish_exercise"))
    return b.as_markup()


# The suggestion button is full-width and carries nothing but the name, so it
# holds a real exercise name ("bench press - flat - machine") whole — only the
# genuinely long ones get cut.
_SUGGEST_NAME_MAX = 32


def suggest_button_label(name: str) -> str:
    """The "как в прошлый раз" button's label, and whether it still names the
    exercise in full — see exercise_picker_entry_keyboard.

    Shows the full name, only cutting it down (word-boundary-aware, with an
    ellipsis) when it doesn't fit the button.
    """
    return _tab_label(name, _SUGGEST_NAME_MAX)


def exercise_picker_entry_keyboard(
    has_planned: bool = False,
    suggested: tuple[int, str] | None = None,
    is_empty: bool = False,
    recent: list[tuple[int, str]] | None = None,
) -> InlineKeyboardMarkup:
    """suggested: (exercise_id, display_name) of what usually follows the just-finished exercise.

    Its button names the exercise ("leg press") so the
    choice can be made without reading the hint line above the keyboard; the
    hint is only kept (see handlers.workout._idle_view) when the name had to be
    shortened to fit.

    recent: up to a couple of (exercise_id, display_name) most-recently-logged
    exercises (never including `suggested`), offered as a one-tap shortcut so
    re-opening something from earlier in the session skips the group→list picker.

    is_empty: nothing logged in this workout yet — "finish" would just discard it
    (see live_finish_workout), so the button reads as an exit rather than a finish.
    """
    b = InlineKeyboardBuilder()
    if has_planned:
        b.button(text="▶️ Следующее по шаблону", callback_data="live:next_planned")
    b.button(text="➕ Упражнение", callback_data="live:add_exercise")
    if suggested is not None:
        ex_id, name = suggested
        # Naming the exercise on the button itself is what makes it a one-tap
        # decision — "как в прошлый раз" alone forces a look up at the hint line
        # to find out what would be opened.
        # No emoji prefix: these buttons are a column of exercise names, and the
        # icons only ate the width the names needed.
        b.button(text=suggest_button_label(name), callback_data=f"live:suggest:{ex_id}")
    b.adjust(1)
    for ex_id, name in recent or []:
        b.row(InlineKeyboardButton(text=name, callback_data=f"live:suggest:{ex_id}"))
    if is_empty:
        b.row(InlineKeyboardButton(text="🏠 Меню", callback_data="live:finish_workout"))
    else:
        b.row(InlineKeyboardButton(text="🏁 Завершить тренировку", callback_data="live:finish_workout"))
    return b.as_markup()


def routines_manage_keyboard(routines, has_workouts: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for r in routines:
        b.button(text=r["name"], callback_data=f"rt:view:{r['id']}")
    if has_workouts:
        b.button(text="➕ Из тренировки", callback_data="rt:pickw:page:0")
    b.button(text="✨ Готовые программы", callback_data="rt:programs")
    b.button(text="🏠 Меню", callback_data="rt:menu")
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


def routine_source_preview_keyboard(workout_id: int) -> InlineKeyboardMarkup:
    """Confirm using this past workout as the routine source, or go back to the list."""
    b = InlineKeyboardBuilder()
    b.button(text="✅ Создать из этой", callback_data=f"rt:pickw:use:{workout_id}")
    b.button(text="⬅️ К списку", callback_data="rt:pickw:back")
    b.adjust(1)
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
    """The program's own screen — start it, or go edit it.

    The per-exercise "🗑 {name}" rows used to sit directly under "▶️ Начать
    тренировку": one row's mistap on the way to starting a session silently
    dropped an exercise, and putting it back appends it to the end, losing the
    program's order. They live behind "✏️ Изменить состав" now.
    """
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="▶️ Начать тренировку", callback_data=f"rt:start:{routine_id}"))
    b.row(InlineKeyboardButton(text="✏️ Изменить состав", callback_data=f"rt:edit:{routine_id}"))
    b.row(InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"rt:rename:{routine_id}"))
    b.row(InlineKeyboardButton(text="📤 Поделиться", callback_data=f"share:rt:{routine_id}"))
    b.row(InlineKeyboardButton(text="🗑 Удалить программу", callback_data=f"rt:delask:{routine_id}"))
    b.row(InlineKeyboardButton(text="⬅️ К списку", callback_data="rt:manage"))
    return b.as_markup()


def routine_edit_keyboard(routine_id: int, exercises=()) -> InlineKeyboardMarkup:
    """The program's composition editor: exercises: (routine_exercise_id,
    display_name) rows in program order, each with a remove button. Reached
    deliberately, so removal stays one tap here without a confirmation."""
    b = InlineKeyboardBuilder()
    for re_id, name in exercises:
        b.row(InlineKeyboardButton(text=f"🗑 {name}", callback_data=f"rt:rmex:{routine_id}:{re_id}"))
    b.row(InlineKeyboardButton(text="➕ Добавить упражнение", callback_data=f"rt:addex:{routine_id}"))
    b.row(InlineKeyboardButton(text="⬅️ Готово", callback_data=f"rt:view:{routine_id}"))
    return b.as_markup()


def stale_workout_keyboard(workout_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Завершить задним числом", callback_data=f"stale:finish:{workout_id}")
    b.button(text="🗑 Удалить", callback_data=f"stale:delete:{workout_id}")
    b.adjust(1)
    return b.as_markup()


def confirm_finish_workout_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Да, завершить", callback_data="live:finish_confirmed")
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


PROGRESS_PERIODS = [(10, "10"), (20, "20"), (9999, "Все")]
DEFAULT_PROGRESS_LIMIT = 20


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
    """Dates only, two per row — what each session contained is spelled out in the
    message body (formatting.build_history_list), so these are just tap targets
    and don't need the full width."""
    b = InlineKeyboardBuilder()
    for w in workouts:
        b.button(text=w["label"], callback_data=f"hist:item:{w['id']}")
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"hist:page:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"hist:page:{page + 1}"))
    b.adjust(2)
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text="🗓 Добавить прошлые тренировки", callback_data="menu:backfill_workout"))
    b.row(InlineKeyboardButton(text="🏠 Меню", callback_data="hist:menu"))
    return b.as_markup()


def repeat_list_keyboard(workouts, page: int, has_next: bool) -> InlineKeyboardMarkup:
    """Pick which past workout to repeat: one button per session, plus paging and
    a way back to the fresh-workout picker."""
    b = InlineKeyboardBuilder()
    for w in workouts:
        b.button(text=w["label"], callback_data=f"pick:rep:show:{w['id']}")
    b.adjust(1)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"pick:rep:page:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"pick:rep:page:{page + 1}"))
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="pick:rep:cancel"))
    return b.as_markup()


def repeat_preview_keyboard(workout_id: int) -> InlineKeyboardMarkup:
    """On the preview of a past workout: repeat it, or go back to the list."""
    b = InlineKeyboardBuilder()
    b.button(text="✅ Повторить эту", callback_data=f"pick:rep:use:{workout_id}")
    b.button(text="⬅️ К списку", callback_data="pick:rep:list")
    b.adjust(1)
    return b.as_markup()


def history_item_keyboard(workout_id: int, show_ai_button: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if show_ai_button:
        b.row(InlineKeyboardButton(text="🤖 Комментарий AI-тренера", callback_data=f"ai:comment:{workout_id}"))
    b.row(
        InlineKeyboardButton(text="🖼 Картинка", callback_data=f"hist:card:{workout_id}"),
        InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"hist:edit:{workout_id}"),
    )
    b.row(
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"hist:del:{workout_id}"),
        InlineKeyboardButton(text="⬅️ К списку", callback_data="hist:back"),
    )
    return b.as_markup()


def workout_card_keyboard(
    workout_id: int, show_ai_button: bool = False, show_achievements: bool = False
) -> InlineKeyboardMarkup:
    """show_achievements: only when this workout actually unlocked a badge. The
    card announces the new badge in its text, and until now there was nowhere to
    go and look at it — the grid lives behind Прогресс → выбор группы."""
    b = InlineKeyboardBuilder()
    if show_ai_button:
        b.row(InlineKeyboardButton(text="🤖 Комментарий AI-тренера", callback_data=f"ai:comment:{workout_id}"))
    if show_achievements:
        b.row(InlineKeyboardButton(text="🏆 Достижения", callback_data="menu:achievements"))
    b.row(
        InlineKeyboardButton(text="🖼 Картинка", callback_data=f"hist:card:{workout_id}"),
        InlineKeyboardButton(text="📝 Заметка", callback_data=f"live:addnote:{workout_id}"),
    )
    # A typo caught right here (still fresh in memory) would otherwise mean
    # Меню → История → find this workout → редактировать — same hist:edit
    # callback the history screen's card already uses.
    b.row(
        InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"hist:edit:{workout_id}"),
        InlineKeyboardButton(text="🏠 Меню", callback_data="live:back_to_menu"),
    )
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
    b.row(InlineKeyboardButton(text="🏠 Меню", callback_data="admin:menu"))
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
    b.row(InlineKeyboardButton(text="🏠 Меню", callback_data="admin:menu"))
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
    b.row(InlineKeyboardButton(text="🏠 Меню", callback_data="admin:menu"))
    return b.as_markup()


def format_utc_offset(tz_offset: int) -> str:
    return "UTC" if tz_offset == 0 else f"UTC{tz_offset:+d}"


def timezone_picker_keyboard(current: int) -> InlineKeyboardMarkup:
    """Grid of whole-hour UTC offsets covering the RU/CIS + Europe range."""
    b = InlineKeyboardBuilder()
    for off in range(-1, 13):  # UTC-1 … UTC+12
        label = format_utc_offset(off)
        b.button(text=f"• {label} •" if off == current else label, callback_data=f"settings:tzset:{off}")
    b.button(text="⬅️ Назад", callback_data="settings:tzback")
    b.adjust(4, 4, 4, 2, 1)
    return b.as_markup()


def settings_keyboard(
    unit: str,
    formula: str,
    pushes_enabled: bool,
    ai_comments_enabled: bool,
    progression_enabled: bool,
    tz_offset: int = 0,
    stickers_enabled: bool = True,
    show_stickers_toggle: bool = False,
    food_macros_enabled: bool = True,
    show_extra_stats: bool = True,
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=f"Единицы: {unit}", callback_data="settings:unit")
    # "e1RM", not "1ПМ": every card, chart and record in the bot is labelled
    # e1RM, and a setting that names the metric differently reads as a setting
    # for something else entirely.
    b.button(text=f"Формула e1RM: {formula}", callback_data="settings:formula")
    b.button(text=f"🕒 Часовой пояс: {format_utc_offset(tz_offset)}", callback_data="settings:tz")
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
    # Hidden entirely when no sticker pack is configured — a toggle for something
    # the bot physically can't do would just be a dead switch.
    if show_stickers_toggle:
        stickers_label = "😎 Стикеры: включены" if stickers_enabled else "😶 Стикеры: выключены"
        b.button(text=stickers_label, callback_data="settings:stickers")
    macros_label = (
        "🔢 КБЖУ в дневнике питания: считаю"
        if food_macros_enabled
        else "📝 КБЖУ в дневнике питания: не считаю"
    )
    b.button(text=macros_label, callback_data="settings:food_macros")
    # users.show_extra_stats has always gated the e1RM line on the finish card;
    # it just had no switch, so nobody could ever turn it off.
    card_label = (
        "📊 Карточка тренировки: подробно"
        if show_extra_stats
        else "📋 Карточка тренировки: компактно"
    )
    b.button(text=card_label, callback_data="settings:card_detail")
    b.button(text="📤 Экспорт CSV", callback_data="settings:export")
    b.button(text="📥 Импорт CSV", callback_data="settings:import")
    b.button(text="🏠 Меню", callback_data="settings:back")
    b.adjust(1)
    return b.as_markup()


# Chart window options for the weight diary (weeks; 0 = all history). The 10/20
# split mirrors the progress chart's periods so both screens offer the same shape.
BODYWEIGHT_PERIODS = [(10, "10 нед"), (20, "20 нед"), (0, "Всё")]
# A bounded window, not all history: the screen lists every entry in it, and on a
# daily weigh-in "Всё" grows without bound. "Всё" stays one tap away.
DEFAULT_BODYWEIGHT_WEEKS = 20


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
    b.row(InlineKeyboardButton(text="🏠 Меню", callback_data="bw:menu"))
    return b.as_markup()


# Сколько дней истории питания на страницу — как в истории тренировок.
FOOD_HISTORY_PAGE_SIZE = 8


def food_day_keyboard(date: dt.date, entry_ids: Sequence[int], today: dt.date) -> InlineKeyboardMarkup:
    """Экран одного дня дневника питания: удаление записей, шаг по дням, история.

    Кнопка удаления на запись — по номеру («🗑 2»), потому что сами названия
    («Куриная грудка с рисом и салатом») в лейбл не влезают, а нумерация уже
    есть в тексте экрана.
    """
    b = InlineKeyboardBuilder()
    if entry_ids:
        b.row(
            *[
                InlineKeyboardButton(text=f"🗑 {i}", callback_data=f"fd:delask:{entry_id}")
                for i, entry_id in enumerate(entry_ids, start=1)
            ],
            width=4,
        )
    prev_day = date - dt.timedelta(days=1)
    nav = [InlineKeyboardButton(text=f"⬅️ {formatting.format_day_month_ru(prev_day)}",
                                callback_data=f"fd:day:{prev_day.isoformat()}")]
    if date < today:
        next_day = date + dt.timedelta(days=1)
        nav.append(
            InlineKeyboardButton(
                text=f"{formatting.format_day_month_ru(next_day)} ➡️",
                callback_data=f"fd:day:{next_day.isoformat()}",
            )
        )
    b.row(*nav)
    if date != today:
        b.row(InlineKeyboardButton(text="📅 Сегодня", callback_data=f"fd:day:{today.isoformat()}"))
    b.row(
        InlineKeyboardButton(text="📚 История", callback_data="fd:history:0"),
        InlineKeyboardButton(text="🏠 Меню", callback_data="fd:menu"),
    )
    return b.as_markup()


def food_delete_confirm_keyboard(entry_id: int, back_date: dt.date) -> InlineKeyboardMarkup:
    """"Удалить запись?" — back_date routes "Нет" back to that day's screen
    (reuses fd:day:, the same callback the day-nav buttons already use)."""
    return yes_no_keyboard(
        yes_cb=f"fd:del:{entry_id}", no_cb=f"fd:day:{back_date.isoformat()}",
        yes_text="🗑 Удалить", no_text="❌ Отмена",
    )


def food_confirm_keyboard() -> InlineKeyboardMarkup:
    """Под догадкой модели: подтвердить, поправить словами или выкинуть."""
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="✅ Всё верно", callback_data="fd:ok"))
    b.row(
        InlineKeyboardButton(text="✏️ Поправить", callback_data="fd:fix"),
        InlineKeyboardButton(text="❌ Отменить", callback_data="fd:cancel"),
    )
    return b.as_markup()


def food_history_keyboard(days: Sequence[dt.date], page: int, has_next: bool) -> InlineKeyboardMarkup:
    """Дни с записями, по два в ряд — что в них было, расписано в тексте экрана."""
    b = InlineKeyboardBuilder()
    for d in days:
        b.button(text=d.strftime("%d.%m.%Y"), callback_data=f"fd:day:{d.isoformat()}")
    b.adjust(2)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"fd:history:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"fd:history:{page + 1}"))
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text="⬅️ К сегодняшнему дню", callback_data="fd:day:today"))
    b.row(InlineKeyboardButton(text="🏠 Меню", callback_data="fd:menu"))
    return b.as_markup()


def back_keyboard(cb: str) -> InlineKeyboardMarkup:
    """Одна кнопка «⬅️ Назад» — для экранов, где больше делать нечего."""
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data=cb)
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


def date_quick_keyboard(prefix: str, today: dt.date | None = None) -> InlineKeyboardMarkup:
    """today: the user's own date — "Сегодня" must mean their today, not the
    server's, or it silently logs to the wrong day near midnight."""
    b = InlineKeyboardBuilder()
    today = today or dt.date.today()
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


def exercise_resolve_keyboard(
    candidates, name: str, prefix: str, remaining: int = 0
) -> InlineKeyboardMarkup:
    """remaining: how many unmatched names are still queued after this one. With
    a foreign CSV that's dozens of names, each needing a pick plus a muscle-group
    choice — "создать все остальные" is the escape hatch that isn't throwing the
    whole import away."""
    b = InlineKeyboardBuilder()
    items = [(f"{prefix}:pick:{ex['id']}", ex["display_name"]) for ex in candidates[:6]]
    for row in named_buttons(items):
        b.row(*row)
    b.row(InlineKeyboardButton(text=f"➕ Создать «{name}»", callback_data=f"{prefix}:create"))
    if remaining > 0:
        b.row(
            InlineKeyboardButton(
                text=f"➕ Создать все остальные ({remaining + 1})",
                callback_data=f"{prefix}:createall",
            )
        )
    b.row(InlineKeyboardButton(text="❌ Отменить весь ввод", callback_data=f"{prefix}:cancelall"))
    return b.as_markup()


def edit_workout_keyboard(exercises) -> InlineKeyboardMarkup:
    """Top level of editing a past workout: one button per exercise.

    exercises: ordered (block_id, exercise_id, label) rows.
    A button per *set* here (with the exercise name repeated in every label, plus
    a "➕ Добавить сет" and a "🗑 Убрать" row each) ran to 30+ single-column rows on an
    ordinary 5-exercise session — the sets live one level down instead, where the
    exercise is already named by the screen's own header.
    """
    b = InlineKeyboardBuilder()
    for block_id, exercise_id, label in exercises:
        b.button(text=label, callback_data=f"editw:ex:{block_id}:{exercise_id}")
    b.button(text="➕ Новое упражнение", callback_data="editw:newex")
    b.button(text="📅 Изменить дату", callback_data="editw:date")
    b.button(text="✅ Готово", callback_data="editw:done")
    b.adjust(1)
    return b.as_markup()


def edit_exercise_keyboard(block_id: int, exercise_id: int, sets) -> InlineKeyboardMarkup:
    """One exercise's sets inside the edit flow. sets: (set_id, label) pairs —
    labels carry no exercise name, the screen header already does."""
    b = InlineKeyboardBuilder()
    for set_id, label in sets:
        b.button(text=label, callback_data=f"editw:set:{set_id}")
    b.button(text="➕ Добавить сет", callback_data=f"editw:addset:{block_id}:{exercise_id}")
    b.button(text="🗑 Убрать упражнение целиком", callback_data=f"editw:rmexask:{block_id}")
    b.button(text="⬅️ К упражнениям", callback_data="editw:top")
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
    # Without these, a mistapped column can't be undone and the only way out of
    # the import is /start.
    b.row(
        InlineKeyboardButton(text="⬅️ Назад", callback_data="imp:mapback"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="imp:cancel"),
    )
    return b.as_markup()
