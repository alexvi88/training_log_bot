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
# new layout next time cmd_start runs (see users.reply_keyboard_version). Also
# bump this — even with the layout unchanged — whenever a bug is fixed in how
# the keyboard gets attached: users already marked "up to date" under the old,
# buggy attach never get a re-send otherwise, so a bugfix alone can't reach
# them. That's what forced v3: the carrier-delete bug (see
# handlers/persistent_menu.py) had already wiped the keyboard on Android
# before the fix landed, and those users' version was already current.
PERSISTENT_MENU_VERSION = 3


def persistent_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_WORKOUT), KeyboardButton(text=BTN_MENU), KeyboardButton(text=BTN_AI)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def main_menu(
    has_active_workout: bool,
    show_quick_log: bool = False,
    community_url: str | None = None,
) -> InlineKeyboardMarkup:
    """show_quick_log: offered while the diary is still empty. A first-time user
    has nothing to look at and a whole training history behind them — letting
    them type one line of it beats walking the picker for the first record.

    community_url: адрес общей группы (config.COMMUNITY_CHAT_URL). Кнопка —
    url, а не callback: чат живёт снаружи бота, и промежуточный экран между
    нажатием и группой ничего бы не добавил."""
    b = InlineKeyboardBuilder()
    if has_active_workout:
        b.button(text="▶️ ПРОДОЛЖИТЬ ТРЕНИРОВКУ", callback_data="menu:resume_workout")
    else:
        b.button(text="🏋️ НАЧАТЬ ТРЕНИРОВКУ", callback_data="menu:start_workout")
    # Also on the persistent keyboard, but the menu is what a new user reads to
    # find out what the bot does — and "menu:ai" had a handler no keyboard sent.
    if show_quick_log:
        b.button(text="✍️ Записать прошлую тренировку", callback_data="menu:quicklog")
    b.button(text="📈 Прогресс", callback_data="menu:progress")
    b.button(text="📚 История", callback_data="menu:history")
    b.button(text="⚙️ Упражнения", callback_data="menu:exercises")
    b.button(text="🗂 Программы", callback_data="rt:manage")
    b.button(text="⚖️ Дневник веса", callback_data="menu:bodyweight")
    b.button(text="🍽 Дневник еды", callback_data="menu:food")
    b.button(text="🏆 Достижения", callback_data="menu:achievements")
    b.button(text="🔧 Настройки", callback_data="menu:settings")
    b.button(text="🤖 AI-тренер", callback_data="menu:ai")
    if community_url:
        b.button(text="💬 Чат атлетов", url=community_url)
    # start/resume and quick-log (if shown) full width, then pairs:
    # Прогресс·История, Упражнения·Программы, Дневник веса·Дневник еды,
    # Достижения·Настройки, then AI-тренер full width at the very bottom,
    # и под ним — чат сообщества, если он заведён.
    b.adjust(*([1, 1] if show_quick_log else [1]), 2, 2, 2, 2, 1, *([1] if community_url else []))
    return b.as_markup()


# Сколько кнопок-упоминаний показываем разом под ответом — дальше листаем
# стрелками, а не удлиняем клавиатуру (см. ai_trainer_keyboard).
AI_MENTION_PAGE_SIZE = 3

# Предложенных действий за один ход бывает несколько («почисти лишние
# программы» — это два удаления), но клавиатура на семь строк перекрывает сам
# ответ, ради которого её и показали.
MAX_AI_ACTIONS = 3

# Потолок Telegram на callback_data одной кнопки, в байтах. Превышение — это
# не обрезанная кнопка, а отказ на ВСЁ сообщение: ответ тренера, уже
# оплаченный квотой, просто не доходит.
_CALLBACK_DATA_LIMIT = 64


def ai_trainer_keyboard(
    has_active_workout: bool = False,
    exercises: Sequence[Any] = (),
    page: int = 0,
    program_name: str | None = None,
    draft_id: int | str | None = None,
    programs: Sequence[Any] = (),
    actions: Sequence[Any] = (),
    presets: Sequence[tuple[str, str]] = (),
) -> InlineKeyboardMarkup:
    """`exercises` — то, что тренер упомянул в ответе (см. exercise_mentions), и
    свои упражнения, и ещё не добавленные из каталога — до
    exercise_mentions.MAX_MENTIONS_TOTAL штук. Своё ведёт прямо на карточку, а
    каталожное сначала добавляет его пользователю и потом открывает ту же
    карточку — прямая ссылка вела бы в никуда, раз упражнения ещё нет.

    `program_name` — название программы, которую тренер собрал в этом ответе
    (см. ai_trainer.propose_program): даёт кнопку, ведущую на превью с составом
    и кнопкой сохранения. `draft_id` — короткий id этого черновика (см. 5.2 /
    handlers/ai_trainer._handle_question): едет в callback_data кнопки, а сам
    черновик по-прежнему лежит в FSM (не влезает в callback_data целиком).
    Раньше кнопка была без параметров и ссылалась просто на «черновик,
    который сейчас лежит в FSM» — под старым ответом она молча сохраняла
    более позднюю программу, если пользователь успел попросить тренера
    собрать другую, и это никак не проверялось при тапе.

    Подпись — «Забрать: <имя>», а не голое название: неотличимое от навигации
    название программы читалось как «открыть программу», а не как предложение,
    которое ждёт подтверждения (см. 5.1).

    `programs` — сохранённые программы, которые тренер назвал в ответе (см.
    program_mentions): ответ вроде «две «Вики» — дубликаты друг друга» знает,
    о чём говорит, и кнопка под ним должна открывать ровно это, а не
    отправлять человека искать программу руками в ⚙️ Программы.

    `actions` — то, что тренер предложил сделать в этом ходе, но не сделал:
    удалить программу, объединить две, поделиться (см. ai_trainer.ActionCallback).
    Каждое — {"label", "callback"}; кнопки стоят отдельными строками над
    списком и не листаются: это ответ на прямую просьбу, а не подсказка по
    тексту, и прятать её на второй странице нельзя.

    `presets` — готовые вопросы стартового экрана (label, callback_data):
    стоят первыми строками, выше всего остального — на интро это единственный
    контент, и тап по ним должен быть первым движением, а не поиском под
    навигацией.

    Программа, если есть, идёт первым пунктом общего списка и делит с
    упоминаниями упражнений один и тот же лимит и постраничную навигацию
    (AI_MENTION_PAGE_SIZE штук за раз, ⬅️/➡️ если пунктов больше) — иначе она
    съедала бы место сверх лимита. Ссылки на упомянутое едут прямо в
    callback_data стрелок (см. handlers/ai_trainer.ai_mentions_page), отдельного
    состояния для них не нужно.
    """
    exercises = list(exercises)
    programs = list(programs)
    b = InlineKeyboardBuilder()
    b.button(text="🏠 Меню", callback_data="ai:menu")
    if has_active_workout:
        b.button(text="🏋️ К тренировке", callback_data="ai:resume_workout")
        b.adjust(2)
    else:
        b.adjust(1)
    nav = b.as_markup().inline_keyboard

    # A sentinel up front so the program shares pagination with the mentions
    # instead of always occupying an extra row above the limit.
    _DRAFT = object()
    items = ([_DRAFT] if program_name else []) + [("saved", p) for p in programs] + exercises

    start = page * AI_MENTION_PAGE_SIZE
    page_items = items[start : start + AI_MENTION_PAGE_SIZE]
    # Каждая кнопка своей строкой: названия длинные, в паре Telegram их обрежет.
    # Разные эмодзи — программа (🗂, как в главном меню), своё упражнение (📌),
    # ещё не добавленное из каталога (📋) — не путаются друг с другом.
    item_rows = []
    for item in page_items:
        if item is _DRAFT:
            emoji = "🗂"
            callback_data = f"ai:prog:view:{draft_id}"
            label = f"Забрать: {program_name}"
        elif isinstance(item, tuple):
            emoji, callback_data, label = "🗂", ai_program_open_cb(item[1]), item[1]["name"]
        elif item["is_template"]:
            emoji, callback_data, label = "📋", f"ai:tpladd:{item['id']}", item["display_name"]
        else:
            emoji, callback_data, label = "📌", f"ai:excard:{item['id']}", item["display_name"]
        item_rows.append(
            [
                InlineKeyboardButton(
                    text=f"{emoji} {_shorten_label(label, AI_MENTION_LABEL_LIMIT)}",
                    callback_data=callback_data,
                )
            ]
        )

    page_nav = []
    if len(items) > AI_MENTION_PAGE_SIZE:
        # Программы едут теми же стрелками, что и упражнения, — с префиксом
        # p/r, чтобы обработчик знал, многодневка это или одиночный день, и не
        # ходил за этим в базу лишний раз.
        refs = [ai_mention_ref(p) for p in programs] + [str(ex["id"]) for ex in exercises]
        # Ссылки едут прямо в callback_data стрелок, а Telegram ограничивает её
        # 64 байтами: с длинными (8-значными) id упражнений полный список туда
        # не влезает, и Telegram отверг бы всё сообщение с ответом целиком.
        # Хвостовые ссылки честнее потерять — это дальние страницы листания,
        # а не сам ответ. Бюджет считаем по префиксу «вперёд»: у page+1 цифр
        # не меньше, чем у page-1.
        budget = _CALLBACK_DATA_LIMIT - len(f"ai:mpage:{page + 1}:".encode())
        while refs and len(",".join(refs).encode()) > budget:
            refs.pop()
        joined = ",".join(refs)
        if page > 0:
            page_nav.append(InlineKeyboardButton(
                text=PAGE_PREV_TEXT, callback_data=f"ai:mpage:{page - 1}:{joined}"
            ))
        if start + AI_MENTION_PAGE_SIZE < len(items):
            page_nav.append(InlineKeyboardButton(
                text=PAGE_NEXT_TEXT, callback_data=f"ai:mpage:{page + 1}:{joined}"
            ))
    page_nav_rows = [page_nav] if page_nav else []

    action_rows = [
        [
            InlineKeyboardButton(
                text=_shorten_label(action["label"], AI_MENTION_LABEL_LIMIT),
                callback_data=action["callback"],
            )
        ]
        for action in list(actions)[:MAX_AI_ACTIONS]
    ]

    preset_rows = [
        [InlineKeyboardButton(text=label, callback_data=cb)] for label, cb in presets
    ]

    return InlineKeyboardMarkup(
        inline_keyboard=preset_rows + action_rows + item_rows + page_nav_rows + nav
    )


def ai_mention_ref(target: Any) -> str:
    """Программа в callback_data стрелок листания: «p12» — многодневка, «r7» —
    одиночная (у них разные экраны и разные таблицы, id между собой не
    пересекаются только внутри своей)."""
    return ("p" if target["kind"] == "program" else "r") + str(target["id"])


def ai_program_open_cb(target: Any) -> str:
    """Чем открывается сохранённая программа: многодневка — своим экраном,
    одиночная — карточкой единственного дня."""
    if target["kind"] == "program":
        return f"rt:prg:{target['id']}"
    return f"rt:view:{target['id']}"



def ai_program_preview_keyboard(
    replacing: bool = False, draft_id: int | str = 0, can_train_now: bool = False,
) -> InlineKeyboardMarkup:
    """Превью программы, собранной тренером: сохранить или отказаться.

    Сохранение создаёт по одной программе (routines.program_name общий на все
    дни — см. handlers/ai_trainer.ai_program_save), просто из нескольких дней,
    так что «🗂 Программы» покажет одну строку с числом дней, а не несколько —
    отсюда подпись «добавить», а не «сохранить программу».

    `replacing` — это правка уже сохранённой программы, а не новая: тап удалит
    её старые дни, и кнопка обязана говорить «обновить», а не «добавить».

    `draft_id` (5.2) едет в callback_data обеих кнопок: обработчик сверяет его
    с id черновика, лежащего в FSM, и отказывается сохранять/удалять чужой,
    более новый черновик, если это превью открыто под устаревшим ответом.

    `can_train_now` — черновик из одного дня, по которому можно пойти прямо
    сейчас. Раньше единственной дорогой от собранного плана к штанге было
    «Добавить себе»: чтобы потренироваться по сгенерённому, приходилось сначала
    сохранить его навсегда — и разовая «тренька на сегодня» оседала в 🗂
    Программы рядом с настоящими программами. Сохранять её незачем и потом:
    проведённая сессия попадает в историю, а «🔁 Повторить тренировку» умеет
    перезапустить любую прошлую. На правке (`replacing`) кнопки нет: там смысл
    тапа — обновить сохранённое, а не сходить разок.
    """
    b = InlineKeyboardBuilder()
    if can_train_now and not replacing:
        b.button(text="▶️ Начать по ней", callback_data=f"ai:prog:train:{draft_id}")
    b.button(
        text="✅ Обновить программу" if replacing else "✅ Добавить себе",
        callback_data=f"ai:prog:save:{draft_id}",
    )
    # «❌ Не надо» тут больше нет. Она стирала черновик — и кнопка «🗂 Забрать:
    # <программа>» под самим ответом тренера становилась мёртвой: тап по ней
    # отвечал «это предложение уже неактуально», хотя человек всего лишь
    # посмотрел превью и передумал сохранять ПРЯМО СЕЙЧАС. Отказ и не нужен
    # отдельной кнопкой: не сохранил — значит не сохранил, а уйти есть куда.
    b.button(text="⬅️ К тренеру", callback_data="menu:ai")
    b.adjust(1)
    return b.as_markup()


def ai_setup_question_keyboard(
    question_index: int, choices: Sequence[str] = ()
) -> InlineKeyboardMarkup:
    """Кнопки под одним вопросом опросника перед сборкой программы.

    Индекс вопроса едет в callback_data каждого варианта не для красоты: кнопки
    в чате живут вечно, и без него тап по варианту под ПРОШЛЫМ вопросом (человек
    проскроллил вверх, передумал) записался бы ответом на текущий — молча и не
    туда. Обработчик сверяет индекс с текущим и на несовпадении не трогает
    ничего (см. handlers/ai_trainer.ai_setup_choice).

    Каждый вариант своей строкой: ответы вроде «час-полтора» в паре Telegram
    обрежет, и два варианта станут неотличимы.

    «⏭ Пропустить вопрос» стоит всегда, даже когда вариантов нет: отмахнуться от
    уточнений — законный ответ («да просто дай что-нибудь»), и без этой кнопки
    единственным выходом из опросника было бы ответить на все вопросы.
    """
    b = InlineKeyboardBuilder()
    for choice_index, choice in enumerate(choices):
        b.button(
            text=_shorten_label(choice, AI_MENTION_LABEL_LIMIT),
            callback_data=f"ai:qa:{question_index}:{choice_index}",
        )
    b.button(text="⏭ Пропустить вопрос", callback_data="ai:qskip")
    b.adjust(1)
    return b.as_markup()


def ai_program_saved_keyboard(program_id: int) -> InlineKeyboardMarkup:
    """После сохранения программы — прямая дорога в неё саму и в общий список.

    «Открыть программу» первой: текст над клавиатурой говорит «ищи в
    «🗂 Программы»», но искать там нечего — бот и так знает, что только что
    сохранил. rt:prg: живёт без StateFilter, так что срабатывает и из
    состояния чата с тренером.
    """
    b = InlineKeyboardBuilder()
    b.button(text="🗂 Открыть программу", callback_data=f"rt:prg:{program_id}")
    b.button(text="🗂 К программам", callback_data="rt:manage")
    b.adjust(1)
    return b.as_markup()


def groups_keyboard(
    groups,
    prefix: str,
    extra_buttons: list[tuple[str, str]] | None = None,
    show_all: bool = False,
    partner_buttons: list[tuple[int, str]] | None = None,
    top_buttons: list[tuple[str, str]] | None = None,
) -> InlineKeyboardMarkup:
    """partner_buttons: (exercise_id, display_name) pairs most often logged
    alongside the exercise the "➕ Суперсет" screen was opened from — a
    one-tap shortcut so a habitual pairing skips the group→list picker.

    top_buttons: (text, callback_data) rows placed above everything else — the
    programs the user is actually training by right now (see
    handlers.workout._picker_screen_groups). They go first because picking the
    day of a running split is the likelier intent than picking a muscle group."""
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
    rows = [[InlineKeyboardButton(text=text, callback_data=cb)] for text, cb in top_buttons or []]
    rows += [
        [InlineKeyboardButton(text=f"⚡ {name}", callback_data=f"{prefix}:partner:{ex_id}")]
        for ex_id, name in partner_buttons or []
    ]
    rows += b.export()
    rows += [[InlineKeyboardButton(text=text, callback_data=cb)] for text, cb in extra_buttons or []]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Голая «➡️» в ряду с названиями читалась как оборванная кнопка, а не как
# «дальше»: непонятно, листает она список или уводит с экрана. Подписываем — и
# одинаково везде, где есть страницы: пикер, упоминания тренера, история,
# дневник еды, выбор тренировки для программы, повтор, админские списки.
PAGE_PREV_TEXT = "⬅️ Предыдущие"
PAGE_NEXT_TEXT = "Ещё ➡️"


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
    show_catalog_button: bool = False,
    new_text: str | None = None,
    new_cb: str = "new",
) -> InlineKeyboardMarkup:
    """templates: catalog exercises (db.search_exercise_templates) to offer
    alongside the user's own matches, marked "📋" and routed through
    `{prefix}:tpladd:{id}` — the same fork-then-open callback the "📋 Выбрать
    из шаблонов" entry already uses, so tapping one behaves identically to
    picking it from the template browser instead of from search.

    show_catalog_button: a standing "📋 Каталог упражнений" row into
    `{prefix}:catalog` — for screens (see handlers/routines.py's rtadd flow)
    that browse a group's own exercises but have no "➕ Новое упражнение" entry
    point of their own to reach the template browser through.

    new_text/new_cb: override the "➕ Новое упражнение" label and callback. An
    empty search result uses them to offer "➕ Создать «жим сидя»" straight from
    the query, so a name the user has already typed doesn't have to be typed
    again.
    """
    b = InlineKeyboardBuilder()
    items = [(f"{prefix}:ex:{ex['id']}", ex["display_name"]) for ex in exercises]
    items += [(f"{prefix}:tpladd:{t['id']}", f"📋 {t['display_name']}") for t in (templates or [])]
    for row in named_buttons(items):
        b.row(*row)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text=PAGE_PREV_TEXT, callback_data=f"{prefix}:page:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text=PAGE_NEXT_TEXT, callback_data=f"{prefix}:page:{page + 1}"))
    if nav:
        b.row(*nav)
    if show_new_button:
        b.row(InlineKeyboardButton(
            text=new_text or "➕ Новое упражнение", callback_data=f"{prefix}:{new_cb}"
        ))
    if show_catalog_button:
        b.row(InlineKeyboardButton(text="📋 Каталог упражнений", callback_data=f"{prefix}:catalog"))
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


def bodyweight_confirm_keyboard() -> InlineKeyboardMarkup:
    """Same "looks like a typo — record it?" nudge as weight_confirm_keyboard,
    for a bodyweight entry outside the plausible range (see
    parser.bodyweight_warning, handlers.bodyweight.bw_weight_entered)."""
    return yes_no_keyboard(
        "bw:wconf:yes", "bw:wconf:no", yes_text="✅ Да, записать", no_text="✏️ Исправить",
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

    Кнопки повтора здесь нет намеренно (см. live:repeat в handlers/workout —
    обработчик подключён, но кнопку к нему убрали в #164). Пробовали вернуть:
    в пару к «Удалить последний» она не встаёт — та занимает двадцать символов
    и в половинной колонке обрежется многоточием, — а своей строкой удлиняет и
    без того высокий экран с вкладками. Повтор остаётся на «=» текстом.
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
    planned_next_name: str | None = None,
    planned_left: int = 0,
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

    planned_next_name / planned_left: what the program has queued up. The first
    button names the exercise that's next in the program's order, and — while
    more than one is left — "📋 Программа" opens the rest (see
    planned_plan_keyboard), because the next one in line is regularly the one
    whose machine is taken.
    """
    b = InlineKeyboardBuilder()
    if has_planned:
        label = (
            f"▶️ {suggest_button_label(planned_next_name)}"
            if planned_next_name
            else "▶️ Следующее по программе"
        )
        b.button(text=label, callback_data="live:next_planned")
        if planned_left > 1:
            # «Другое», а не «осталось»: порядок в программе — подсказка, а не
            # рельсы (внутри экрана так и написано), но снаружи первая кнопка
            # выглядела единственным путём, и про свободу выбора человек узнавал,
            # только заглянув сюда.
            b.button(text=f"📋 Другое из плана · ещё {planned_left}", callback_data="live:plan")
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


# Full-width buttons carrying "название — 3x8-12"; Telegram's own limit is 64,
# but names past this start wrapping to a second line on a phone.
_PLAN_LABEL_MAX = 44


def planned_plan_keyboard(items: list[tuple[int, str]], *, removing: bool = False) -> InlineKeyboardMarkup:
    """What's left of the program, any of it startable right now.

    `items` — (position in planned_blocks, label) in program order. The program's
    order is a suggestion, not a queue: a taken machine shouldn't force the whole
    session to wait, so every remaining exercise is one tap away and the rest keep
    their order after it.

    Каждая строка — на всю ширину. «Убрать из плана» (для тренажёра, который
    реально сломан, а не просто занят) раньше висело крестиком в той же строке,
    но Telegram делит строку из двух кнопок пополам: половину экрана занимали
    ✕, а названия упражнений обрезались до «Сгибание но…ре». Посреди
    тренировки нужен список того, что делать, — убирание из плана ушло под
    отдельную кнопку внизу (`removing=True` перерисовывает тот же список, где
    тап убирает строку).
    """
    b = InlineKeyboardBuilder()
    action = "skip" if removing else "pick"
    for index, label in items:
        b.row(
            InlineKeyboardButton(
                text=_tab_label(label, _PLAN_LABEL_MAX), callback_data=f"live:plan:{action}:{index}",
            ),
        )
    if removing:
        b.row(InlineKeyboardButton(text="✅ Готово", callback_data="live:plan"))
    else:
        b.row(InlineKeyboardButton(text="✕ Убрать из плана", callback_data="live:plan:rm"))
        b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="live:plan:back"))
    return b.as_markup()


def routines_manage_keyboard(
    programs, routines, has_workouts: bool, back_to_picker: bool = False
) -> InlineKeyboardMarkup:
    """`programs` — многодневки одной строкой каждая (см. db.list_programs): их
    дни лежат за вторым экраном, иначе трёхдневный сплит занимает три кнопки и
    список перестаёт читаться. `routines` — одиночные программы, у них второго
    уровня нет и они ведут сразу в карточку.

    `back_to_picker` — сюда попали с экрана выбора группы мышц уже начатой
    тренировки («🗂 Выбрать программу»): последняя кнопка ведёт назад туда же,
    а не в главное меню мимо тренировки, которая всё это время ждёт."""
    b = InlineKeyboardBuilder()
    for p in programs:
        word = formatting.plural_ru(p["day_count"], ("день", "дня", "дней"))
        b.button(
            text=f"🗂 {p['name']} · {p['day_count']} {word}",
            callback_data=f"rt:prg:{p['id']}",
        )
    for r in routines:
        b.button(text=r["name"], callback_data=f"rt:view:{r['id']}")
    # Каталог первым и с эмодзи-акцентом: у человека без единой тренировки
    # верный ответ почти всегда «возьми готовую» — это мгновенно и бесплатно,
    # тогда как AI-путь требует переписки. Три равнозначные кнопки заставляли
    # выбирать способ раньше, чем он понял, что вообще выбирает.
    b.button(text="✨ Готовые программы", callback_data="rt:programs")
    b.button(text="🤖 Составить с AI-тренером", callback_data="ai:buildprog")
    if has_workouts:
        b.button(text="➕ Из тренировки", callback_data="rt:pickw:page:0")
    b.button(text="⬅️ Назад" if back_to_picker else "🏠 Меню", callback_data="rt:menu")
    b.adjust(1)
    return b.as_markup()


def program_days_keyboard(
    days, program_id: int, next_day_id: int | None = None, trained_before: bool = False
) -> InlineKeyboardMarkup:
    """Экран программы: до какого дня дошла очередь, остальные ниже, и одна
    кнопка правок.

    `next_day_id` — день, до которого дошла очередь (db.next_program_day). Он
    поднят наверх отдельной кнопкой, потому что раньше три дня сплита выглядели
    тремя одинаковыми кнопками и вспоминать, что вчера был «Толкай», приходилось
    самому — при том, что бот это знал (workouts.routine_id).

    И только при `trained_before` — то есть когда по программе уже ходили.
    Раньше кнопка стояла всегда и на новой программе называла первый день
    «сегодняшним»: очереди ещё нет, бот просто берёт первый по списку, а слово
    обещало расписание, которого у программ нет вовсе. Выделять первый день на
    новой программе не нужно и без вранья — он и так первый в списке. Текст
    сообщения про очередь говорит по тому же условию (см.
    handlers.routines.show_program).

    Всё, что меняет программу, уехало за «⚙️ Изменить программу» (см.
    program_edit_keyboard). Шесть кнопок редактирования стояли ровно на пути
    «пойти потренироваться» — а между тренировками программу правят примерно
    никогда, зато открывают её каждый раз. На двухдневной программе экран был из
    девяти кнопок, стал из четырёх.
    """
    b = InlineKeyboardBuilder()
    hoisted = None
    if trained_before and next_day_id is not None:
        hoisted = next((d for d in days if d["id"] == next_day_id), None)
    if hoisted is not None:
        b.button(text=f"▶️ Дальше: {hoisted['name']}", callback_data=f"rt:view:{hoisted['id']}")
    for d in days:
        if hoisted is not None and d["id"] == hoisted["id"]:
            continue
        b.button(text=d["name"], callback_data=f"rt:view:{d['id']}")
    b.button(text="⚙️ Изменить программу", callback_data=f"rt:pgmedit:{program_id}")
    b.button(text="⬅️ Назад", callback_data="rt:manage")
    b.adjust(1)
    return b.as_markup()


def program_edit_keyboard(days, program_id: int) -> InlineKeyboardMarkup:
    """«⚙️ Изменить программу»: всё, что меняет саму программу, одним экраном.

    Подписи без слова «программу» — заголовок экрана её и так называет, а
    короткие подписи встают по две в ряд, так что четыре действия занимают две
    строки вместо четырёх.
    """
    b = InlineKeyboardBuilder()
    # Состав дней — первым делом: «изменить программу» человек жмёт прежде всего
    # чтобы поменять упражнения, а этого пункта тут не было вовсе. Добраться до
    # состава можно было только вернувшись на экран программы и ткнув в день —
    # догадаться неоткуда, экран правок про день не говорил ни слова.
    for d in days:
        b.button(text=f"✏️ {d['name']}", callback_data=f"rt:edit:{d['id']}")
    b.button(text="➕ Добавить день", callback_data=f"rt:dayadd:{program_id}")
    if len(days) > 1:
        b.button(text="🔀 Порядок дней", callback_data=f"rt:dayorder:{program_id}")
    # Копия целиком — база для «хочу вторую версию с правками»: без неё
    # единственный способ получить вариант программы был собрать её заново.
    b.button(text="📄 Дублировать", callback_data=f"rt:pgmcopy:{program_id}")
    b.button(text="✏️ Переименовать", callback_data=f"rt:pgmrename:{program_id}")
    # Программа целиком, а не день — тот же токен-визитка, но со всеми днями
    # разом: делиться по одному дню значило собирать программу получателю
    # вручную из нескольких пересланных сообщений.
    #
    # Префикс share:prg: (а не share:pgm:) — id здесь программы, а старый
    # префикс остался за кнопками, которые адресовались днём-якорем; см.
    # handlers.sharing.share_program_legacy и ту же пару rt:prg:/rt:pgm:.
    b.button(text="📤 Поделиться", callback_data=f"share:prg:{program_id}")
    b.button(text="🗑 Удалить", callback_data=f"rt:pgmdelask:{program_id}")
    b.button(text="⬅️ Назад", callback_data=f"rt:prg:{program_id}")
    # Работа с днями — полной шириной: она про состав программы, а не про
    # программу целиком, и ставить её в пару с «Удалить» значило бы посадить
    # рядом безобидное и необратимое.
    b.adjust(*((1, 1) if len(days) > 1 else (1,)), 2, 2, 1)
    return b.as_markup()


def program_day_order_keyboard(days, program_id: int) -> InlineKeyboardMarkup:
    """Перестановка дней: номер с именем первым, за ним одна стрелка.

    Ровно та же раскладка, что у упражнений внутри дня (routine_edit_keyboard),
    и по тем же причинам. Было наоборот: две колонки стрелок, а название —
    третьим, да ещё с пустышками «·» на краях списка, где стрелка не работала.
    Читалось это как «⬆️ ⬇️ День 2», то есть сначала непонятно что, потом уже
    про что.

    Стрелка одна и ходит по кругу (db.reorder_program_day): поднять второй день
    — то же самое, что опустить первый, а у первого «выше» отправляет его в
    конец. Так пустышки не нужны вовсе.
    """
    b = InlineKeyboardBuilder()
    for position, d in enumerate(days, start=1):
        b.row(
            InlineKeyboardButton(
                text=f"{position}. {_shorten_label(d['name'], 24)}",
                callback_data=f"rt:view:{d['id']}",
            ),
            InlineKeyboardButton(text="⬆️", callback_data=f"rt:daymv:{d['id']}:up"),
        )
    # Назад — на шаг, откуда пришли («⚙️ Изменить программу»), а не сразу на
    # экран программы: порядок дней меняют в связке с остальными правками, и
    # выбрасывать человека из редактора после каждой значит заставлять его
    # заходить туда заново.
    b.row(InlineKeyboardButton(text="⬅️ Готово", callback_data=f"rt:pgmedit:{program_id}"))
    return b.as_markup()


def program_day_source_keyboard(program_id: int, days) -> InlineKeyboardMarkup:
    """«➕ Добавить день»: пустой день, копия существующего или снимок прошлой
    тренировки. Программа была неизменяемого размера навсегда — взял из каталога
    трёхдневку, захотел четвёртый день на руки, и пересобирать надо было всё."""
    b = InlineKeyboardBuilder()
    b.button(text="📄 Пустой день", callback_data=f"rt:dayblank:{program_id}")
    # «Из тренировки», а не «из прошлой»: за кнопкой список всех проведённых
    # тренировок с листалкой, а «прошлая» читается как «последняя» — будто выбора
    # нет. Тем же словом эта кнопка названа и в списке программ («➕ Из
    # тренировки»), и ведут они в один и тот же экран.
    b.button(text="🏋️ Из тренировки", callback_data=f"rt:daypickw:{program_id}:0")
    for d in days:
        b.button(text=f"⧉ Копия «{d['name']}»", callback_data=f"rt:daycopy:{d['id']}")
    b.button(text="⬅️ Назад", callback_data=f"rt:prg:{program_id}")
    b.adjust(1)
    return b.as_markup()


def routine_source_picker_keyboard(workouts, page: int, has_next: bool) -> InlineKeyboardMarkup:
    """Pick a past finished workout to snapshot into a new routine."""
    b = InlineKeyboardBuilder()
    for w in workouts:
        b.button(text=w["label"], callback_data=f"rt:pickw:item:{w['id']}")
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text=PAGE_PREV_TEXT, callback_data=f"rt:pickw:page:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text=PAGE_NEXT_TEXT, callback_data=f"rt:pickw:page:{page + 1}"))
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


def routine_detail_keyboard(routine_id: int, program_id: int | None = None) -> InlineKeyboardMarkup:
    """The program's own screen — start it, or go edit it.

    The per-exercise "🗑 {name}" rows used to sit directly under "▶️ Начать
    тренировку": one row's mistap on the way to starting a session silently
    dropped an exercise, and putting it back appends it to the end, losing the
    program's order. They live behind "✏️ Изменить состав" now.

    `program_id` — этот экран обслуживает и одиночную программу, и день
    многодневки, и раньше подписи этого не различали: на дне «🗑 Удалить
    программу» удаляла один день, а точно такая же кнопка этажом выше — всю
    программу, и по тексту подтверждения различить их было нельзя. Плюс «назад»
    у дня ведёт к списку дней, а не на самый верх.

    Отсюда же и голое «🗑 Удалить» у одиночной: кнопка стоит второй в ряду, и
    Telegram резал «Удалить программу» до «🗑 Удал…ограмму». Слово «программу»
    тут ничего не уточняет — заголовок сообщения её и называет, а спутать не с
    чем: на дне подпись своя, и она короткая. Экран подтверждения по-прежнему
    говорит полностью, что именно сносим.
    """
    is_day = program_id is not None
    back = f"rt:prg:{program_id}" if is_day else "rt:manage"
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="▶️ Начать тренировку", callback_data=f"rt:start:{routine_id}"))
    b.row(
        InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"rt:editmenu:{routine_id}"),
        InlineKeyboardButton(
            text="🗑 Удалить день" if is_day else "🗑 Удалить",
            callback_data=f"rt:delask:{routine_id}",
        ),
    )
    b.row(
        InlineKeyboardButton(text="📤 Поделиться", callback_data=f"share:rt:{routine_id}"),
        InlineKeyboardButton(text="⬅️ К списку", callback_data=back),
    )
    return b.as_markup()


def routine_edit_menu_keyboard(routine_id: int, is_day: bool = False) -> InlineKeyboardMarkup:
    """Sub-menu behind "✏️ Редактировать": состав и название редко трогают
    вместе, но обе — правки, а не отдельные действия верхнего уровня."""
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="✏️ Изменить состав", callback_data=f"rt:edit:{routine_id}"))
    b.row(
        InlineKeyboardButton(
            text="✏️ Переименовать день" if is_day else "✏️ Переименовать",
            callback_data=f"rt:rename:{routine_id}",
        )
    )
    if is_day:
        # Вынести день наружу отдельной программой — обратная операция к
        # «➕ Добавить день»; без неё день, попавший в программу, оставался в
        # ней навсегда.
        b.row(
            InlineKeyboardButton(text="📤 Вынести из программы", callback_data=f"rt:dayout:{routine_id}")
        )
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"rt:view:{routine_id}"))
    return b.as_markup()


def routine_edit_keyboard(routine_id: int, exercises=()) -> InlineKeyboardMarkup:
    """Редактор состава дня: номер с именем первым, за ним ⬆️ и 🗑.

    Стрелка одна и работает по кругу: поднять второе — то же самое, что опустить
    первое, а у первого «выше» отправляет его в конец. Вторая колонка только
    отбирала место у названия.

    Карандаша нет по той же причине: он вёл ровно туда же, куда тап по имени, а
    забирал четверть ряда. Ряд Telegram делит поровну, так что каждая лишняя
    колонка режет подпись — с четырьмя было «Жим гантелей лё…» и «Жим гантелей
    си…» рядом, с тремя влезает заметно больше.

    Номер в подписи — чтобы обрезка перестала быть фатальной: «Тяга гантели в
    наклоне» и «Тяга гантелей лёжа на наклонной скамье» обрезаются в одно и то
    же, и без номера по кнопке не понять, какую из них удаляешь. Полный состав с
    номерами и схемами стоит в тексте сообщения — там и читают.
    """
    b = InlineKeyboardBuilder()
    for position, entry in enumerate(exercises, start=1):
        re_id, name = entry[0], entry[1]
        b.row(
            InlineKeyboardButton(
                text=f"{position}. {_shorten_label(name, 22)}",
                callback_data=f"rt:extarget:{routine_id}:{re_id}",
            ),
            InlineKeyboardButton(text="⬆️", callback_data=f"rt:mvex:{routine_id}:{re_id}:up"),
            InlineKeyboardButton(text="🗑", callback_data=f"rt:rmex:{routine_id}:{re_id}"),
        )
    b.row(InlineKeyboardButton(text="➕ Добавить упражнение", callback_data=f"rt:addex:{routine_id}"))
    b.row(InlineKeyboardButton(text="⬅️ Готово", callback_data=f"rt:view:{routine_id}"))
    return b.as_markup()


def program_name_taken_keyboard(existing_id: int, back_cb: str, add_cb: str) -> InlineKeyboardMarkup:
    """Имя занято — три честных выхода вместо молчаливого слияния, которое
    происходило раньше."""
    b = InlineKeyboardBuilder()
    b.button(text="🗂 Открыть существующую", callback_data=f"rt:prg:{existing_id}")
    b.button(text="➕ Добавить второй копией", callback_data=add_cb)
    b.button(text="❌ Отмена", callback_data=back_cb)
    b.adjust(1)
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
        nav.append(InlineKeyboardButton(text=PAGE_PREV_TEXT, callback_data=f"hist:page:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text=PAGE_NEXT_TEXT, callback_data=f"hist:page:{page + 1}"))
    b.adjust(2)
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text="🗓 Добавить прошлые тренировки", callback_data="menu:backfill_workout"))
    b.row(InlineKeyboardButton(text="🏠 Меню", callback_data="hist:menu"))
    return b.as_markup()


def history_search_keyboard(workouts, page: int, has_next: bool) -> InlineKeyboardMarkup:
    """Same tap targets as history_list_keyboard, but paging keeps the search
    query alive (hist:spage:N vs hist:page:N) — a frequent exercise's old
    workouts used to be unreachable past the first 20 matches."""
    b = InlineKeyboardBuilder()
    for w in workouts:
        b.button(text=w["label"], callback_data=f"hist:item:{w['id']}")
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text=PAGE_PREV_TEXT, callback_data=f"hist:spage:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text=PAGE_NEXT_TEXT, callback_data=f"hist:spage:{page + 1}"))
    b.adjust(2)
    if nav:
        b.row(*nav)
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
        nav.append(InlineKeyboardButton(text=PAGE_PREV_TEXT, callback_data=f"pick:rep:page:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text=PAGE_NEXT_TEXT, callback_data=f"pick:rep:page:{page + 1}"))
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
        # Суффикс :card — «пришли с карточки законченной тренировки», по образцу
        # существующего :prog. Нужен, чтобы экран достижений НЕ съел карточку:
        # из главного меню и из прогресса удалять экран под собой правильно, а
        # карточка это запись в ленте, и второй раз её взять неоткуда.
        b.row(InlineKeyboardButton(
            text="🏆 Достижения", callback_data="menu:achievements:card"
        ))
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
        nav.append(InlineKeyboardButton(text=PAGE_PREV_TEXT, callback_data=f"admin:up:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text=PAGE_NEXT_TEXT, callback_data=f"admin:up:{page + 1}"))
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
        nav.append(InlineKeyboardButton(text=PAGE_PREV_TEXT, callback_data=f"admin:hp:{target_user_id}:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text=PAGE_NEXT_TEXT, callback_data=f"admin:hp:{target_user_id}:{page + 1}"))
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
        nav.append(InlineKeyboardButton(text=PAGE_PREV_TEXT, callback_data=f"admin:aip:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text=PAGE_NEXT_TEXT, callback_data=f"admin:aip:{page + 1}"))
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


def admin_activity_users_keyboard(users, page: int, has_next: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for u in users:
        name = f"@{u['username']}" if u["username"] else str(u["telegram_id"])
        b.button(text=f"{name} ({u['event_count']})", callback_data=f"admin:acu:{u['telegram_id']}")
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text=PAGE_PREV_TEXT, callback_data=f"admin:acp:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text=PAGE_NEXT_TEXT, callback_data=f"admin:acp:{page + 1}"))
    b.adjust(1)
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text="🌐 Все пользователи", callback_data="admin:aca:0"))
    b.row(InlineKeyboardButton(text="🏠 Меню", callback_data="admin:menu"))
    return b.as_markup()


def admin_activity_all_keyboard(page: int, has_next: bool) -> InlineKeyboardMarkup:
    """Общая лента всех пользователей: та же логика «⬅️ раньше / позже ➡️», что и в ленте одного юзера."""
    b = InlineKeyboardBuilder()
    nav = []
    if has_next:
        nav.append(InlineKeyboardButton(text="⬅️ раньше", callback_data=f"admin:aca:{page + 1}"))
    if page > 0:
        nav.append(InlineKeyboardButton(text="позже ➡️", callback_data=f"admin:aca:{page - 1}"))
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text="⬅️ К пользователям", callback_data="admin:acb"))
    return b.as_markup()


def admin_activity_feed_keyboard(target_user_id: int, page: int, has_next: bool) -> InlineKeyboardMarkup:
    """Лента одного пользователя: страницы — вглубь истории, «⬅️» — назад к списку.

    Стрелки читаются как время, а не как номер страницы: следующая страница —
    это события постарше, поэтому «⬅️ раньше» слева, «позже ➡️» справа.
    """
    b = InlineKeyboardBuilder()
    nav = []
    if has_next:
        nav.append(InlineKeyboardButton(text="⬅️ раньше", callback_data=f"admin:acf:{target_user_id}:{page + 1}"))
    if page > 0:
        nav.append(InlineKeyboardButton(text="позже ➡️", callback_data=f"admin:acf:{target_user_id}:{page - 1}"))
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text="⬅️ К пользователям", callback_data="admin:acb"))
    return b.as_markup()


def admin_pushes_keyboard(page: int, has_next: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text=PAGE_PREV_TEXT, callback_data=f"admin:pp:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text=PAGE_NEXT_TEXT, callback_data=f"admin:pp:{page + 1}"))
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text="🏠 Меню", callback_data="admin:menu"))
    return b.as_markup()


def format_utc_offset(tz_offset: int) -> str:
    return "UTC" if tz_offset == 0 else f"UTC{tz_offset:+d}"


def timezone_picker_keyboard(current: int) -> InlineKeyboardMarkup:
    """Сетка целочасовых смещений от UTC — весь обитаемый диапазон.

    Раньше сетка шла от UTC−1: продукт русскоязычный, и охват «СНГ и Европа»
    выглядел достаточным. Но русскоязычные живут и в Америке, а без своего
    пояса у них уезжает «сегодня» — а с ним границы суток, стрики и
    напоминания — на три-восемь часов, и поправить это было нечем.
    """
    b = InlineKeyboardBuilder()
    for off in range(-11, 15):  # UTC-11 … UTC+14
        label = format_utc_offset(off)
        b.button(text=f"• {label} •" if off == current else label, callback_data=f"settings:tzset:{off}")
    b.button(text="⬅️ Назад", callback_data="settings:tzback")
    # 26 смещений по четыре в ряд, остаток и «назад» — своими рядами.
    b.adjust(4, 4, 4, 4, 4, 4, 2, 1)
    return b.as_markup()


def settings_keyboard(
    unit: str,
    formula: str,
    pushes_enabled: bool,
    ai_comments_enabled: bool,
    progression_enabled: bool,
    tz_offset: int = 0,
    food_macros_enabled: bool = True,
    show_extra_stats: bool = True,
    show_mcp: bool = False,
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
    # Профиль тренирующегося пишет AI-тренер (ai_trainer.save_athlete_profile),
    # и до появления этого экрана посмотреть, что он там про тебя записал, было
    # нельзя нигде — при том что от этих полей зависит, какую программу он
    # соберёт.
    b.button(text="🤖 Что тренер про тебя знает", callback_data="settings:profile")
    b.button(text="📤 Экспорт CSV", callback_data="settings:export")
    b.button(text="📥 Импорт CSV", callback_data="settings:import")
    # Скрыт, когда бот развёрнут без публичного адреса для MCP: подключать
    # тогда физически не к чему (см. config.mcp_available).
    if show_mcp:
        b.button(text="🔌 Подключить к Claude и ChatGPT", callback_data="settings:mcp")
    b.button(text="🏠 Меню", callback_data="settings:back")
    b.adjust(1)
    return b.as_markup()


def profile_keyboard() -> InlineKeyboardMarkup:
    """Экран «🧬 Обо мне»: посмотреть и, если надо, стереть.

    Правится профиль репликой тренеру, а не отсюда: поля свободнотекстовые, и
    городить под каждое свой ввод значило бы дублировать разговор, который для
    этого и существует. А вот стереть всё разом словами не попросишь — модель
    save_athlete_profile пустые поля игнорирует, — поэтому кнопка одна.
    """
    b = InlineKeyboardBuilder()
    b.button(text="🗑 Очистить", callback_data="settings:profileclear")
    b.button(text="⬅️ Назад", callback_data="settings:menu")
    b.adjust(1)
    return b.as_markup()


# Клиенты, которые подключаются коннектором по OAuth: адрес и код из бота, ни
# одного файла конфигурации. Их инструкции показываются всегда — токена они не
# требуют вовсе.
MCP_OAUTH_CLIENTS = [
    ("claude", "☁️ Claude"),
    ("claude_code", "🖥 Claude Code"),
]

# Второй ряд. Два Claude стоят рядом в первом — они и называются похоже, и
# ищутся вместе; ChatGPT уезжает ниже, чтобы ряды не читались как «Claude против
# всех остальных».
#
# Список нарочно короткий: экран нужен, чтобы человек подключился, а не чтобы
# перечислить всё, что умеет MCP. Cursor и VS Code подключаются тем же
# коннектором по тому же адресу.
MCP_SECOND_ROW_CLIENTS = [
    ("chatgpt", "🤖 ChatGPT"),
]

# Все клиенты сразу: ключи совпадают с handlers.mcp_access.GUIDES.
MCP_CLIENTS = MCP_OAUTH_CLIENTS + MCP_SECOND_ROW_CLIENTS


def mcp_keyboard(has_token: bool, has_connections: bool = False) -> InlineKeyboardMarkup:
    """Экран /mcp: сверху простой путь, ниже — токен для терминала.

    Порядок ровно такой, потому что подключение коннектором доступно всем и
    сразу, а токен нужен меньшинству: ставить его первым значит отправлять
    человека копировать секрет в конфиг там, где хватило бы шести цифр.
    Кнопка «Подключённые приложения» появляется, только когда есть что
    отключать, — иначе это тап в пустой список.

    Кнопки разложены по рядам, а не столбиком: девять штук в одну колонку — это
    простыня, в которой глазу не за что зацепиться, хотя группы очевидны
    (клиенты / код и приложения / токен). Парами они читаются как три раздела.
    """
    b = InlineKeyboardBuilder()
    # Claude один на браузер и приложение — путь там ровно один и тот же, и второй
    # экран с тем же текстом только сбивает. Claude Code рядом с ним: тоже Claude,
    # тоже коннектором, отличается только тем, что живёт в терминале.
    b.row(
        *(
            InlineKeyboardButton(text=label, callback_data=f"mcp:how:{kind}")
            for kind, label in MCP_OAUTH_CLIENTS
        )
    )
    b.row(
        *(
            InlineKeyboardButton(text=label, callback_data=f"mcp:how:{kind}")
            for kind, label in MCP_SECOND_ROW_CLIENTS
        )
    )
    # Код — после инструкций, а не первым: он живёт минуты и нужен на середине
    # пути, когда приложение уже открыло страницу подтверждения. Взятый заранее
    # успевает истечь, пока человек ищет в приложении раздел коннекторов, — та же
    # кнопка есть на каждом экране инструкции, где она и к месту.
    b.row(InlineKeyboardButton(text="🔗 Код для подключения", callback_data="mcp:code"))
    if has_connections:
        b.row(
            InlineKeyboardButton(
                text="🔌 Подключённые приложения", callback_data="mcp:apps"
            )
        )
    if has_token:
        b.row(
            InlineKeyboardButton(text="♻️ Перевыпустить", callback_data="mcp:issue"),
            InlineKeyboardButton(text="🗑 Отозвать", callback_data="mcp:revoke"),
        )
    else:
        b.row(
            InlineKeyboardButton(
                text="🔑 Выдать токен для терминала", callback_data="mcp:issue"
            )
        )
    b.row(InlineKeyboardButton(text="🔧 Настройки", callback_data="menu:settings"))
    return b.as_markup()


def mcp_guide_keyboard(code_kind: str | None = None) -> InlineKeyboardMarkup:
    """Экран инструкции: назад к списку клиентов.

    `code_kind` — вид инструкции, на которой показан код связывания. Тогда сверху
    появляется «🔄 Новый код», и он перерисовывает эту же инструкцию: истёкший код
    — обычный исход, и лечиться он должен на месте, а не походом на другой экран.
    """
    b = InlineKeyboardBuilder()
    if code_kind:
        # Суффикс :new — чтобы просто вернуться на этот экран можно было, не
        # трогая код: человек мог его уже скопировать и зайти перечитать шаг.
        b.button(text="🔄 Новый код", callback_data=f"mcp:how:{code_kind}:new")
    b.button(text="⬅️ К подключению", callback_data="mcp:open")
    b.adjust(1)
    return b.as_markup()


def mcp_code_keyboard() -> InlineKeyboardMarkup:
    """Экран кода без инструкции — для тех, у кого страница подтверждения уже
    открыта. «Новый код» тут же: код живёт минуты."""
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Новый код", callback_data="mcp:code")
    b.button(text="⬅️ К подключению", callback_data="mcp:open")
    b.adjust(1)
    return b.as_markup()


def mcp_apps_keyboard(connections: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """Подключённые приложения: на каждое — своя кнопка «Отключить».

    Отзыв именно по приложению, а не «всё сразу»: у человека может быть
    подключён и браузерный Claude, и ChatGPT, и убить оба, чтобы закрыть один,
    — это не отзыв доступа, а поломка настроенного.
    """
    b = InlineKeyboardBuilder()
    for client_id, name in connections:
        b.button(text=f"🚫 Отключить {name}", callback_data=f"mcp:off:{client_id}")
    b.button(text="⬅️ К подключению", callback_data="mcp:open")
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


def limit_ack_keyboard(kind: str) -> InlineKeyboardMarkup:
    """Под предупреждением о лимите на своём аккаунте (см. ai_limits.preview_text).

    Одна кнопка: нажал — и до конца суток этот вид лимита тебя пропускает.
    """
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="👌 Понятно, пропускай", callback_data=f"ail:ack:{kind}"))
    return b.as_markup()


def food_history_keyboard(days: Sequence[dt.date], page: int, has_next: bool) -> InlineKeyboardMarkup:
    """Дни с записями, по два в ряд — что в них было, расписано в тексте экрана."""
    b = InlineKeyboardBuilder()
    for d in days:
        b.button(text=d.strftime("%d.%m.%Y"), callback_data=f"fd:day:{d.isoformat()}")
    b.adjust(2)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text=PAGE_PREV_TEXT, callback_data=f"fd:history:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text=PAGE_NEXT_TEXT, callback_data=f"fd:history:{page + 1}"))
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


def routine_exercise_target_keyboard(cb: str) -> InlineKeyboardMarkup:
    """Prompt after picking an exercise to add to a routine: type a target
    ("3x8-12") or skip and leave it blank."""
    b = InlineKeyboardBuilder()
    b.button(text="➡️ Пропустить", callback_data=cb)
    b.adjust(1)
    return b.as_markup()


def feedback_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Готово", callback_data="feedback:done")
    return b.as_markup()


def push_cta_keyboard(text: str = "▶ Начать тренировку") -> InlineKeyboardMarkup:
    """Attached to daily-rotation push notifications: routes straight into starting a workout.

    `text` меняется по категории пуша (см. engagement.PUSH_CTA_BY_CATEGORY):
    кнопка — последняя строка пуша, и «▶ Начать тренировку» под «серия на кону»
    звучит как реклама, а «▶ Спасти серию» — как продолжение реплики тренера.
    """
    b = InlineKeyboardBuilder()
    b.button(text=text, callback_data="menu:start_workout")
    return b.as_markup()


def announcement_keyboard(buttons: Sequence[tuple[str, str]]) -> InlineKeyboardMarkup:
    """Кнопки под разовой релизной рассылкой (см. announcements.py).

    По одной в строку: в анонсе их обычно две, и каждая — вход в свою фичу, а
    не «да/нет». Две кнопки в ряд читаются как выбор из одного, а тут можно и
    то, и другое.
    """
    b = InlineKeyboardBuilder()
    for text, callback_data in buttons:
        b.button(text=text, callback_data=callback_data)
    b.adjust(1)
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
    candidates, name: str, prefix: str, remaining: int = 0, templates=()
) -> InlineKeyboardMarkup:
    """remaining: how many unmatched names are still queued after this one. With
    a foreign CSV that's dozens of names, each needing a pick plus a muscle-group
    choice — "создать все остальные" is the escape hatch that isn't throwing the
    whole import away.

    templates: каталожные шаблоны, подошедшие по имени. Импорт — самый массовый
    вход новых упражнений, и без этого ряда имя, буквально совпадающее с
    каталогом, заводилось голым: без техники, без фото и без группы."""
    b = InlineKeyboardBuilder()
    items = [(f"{prefix}:pick:{ex['id']}", ex["display_name"]) for ex in candidates[:6]]
    for row in named_buttons(items):
        b.row(*row)
    for tpl in list(templates)[:4]:
        b.row(
            InlineKeyboardButton(
                text=f"📋 {tpl['display_name']}", callback_data=f"{prefix}:tpl:{tpl['id']}"
            )
        )
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
    b.button(text="➕ Добавить подход", callback_data=f"editw:addset:{block_id}:{exercise_id}")
    b.button(text="🗑 Убрать упражнение целиком", callback_data=f"editw:rmexask:{block_id}")
    b.button(text="⬅️ К упражнениям", callback_data="editw:top")
    b.adjust(1)
    return b.as_markup()


def set_actions_keyboard(set_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Изменить вес/повторы", callback_data=f"editw:editset:{set_id}")
    b.button(text="🗑 Удалить подход", callback_data=f"editw:delset:{set_id}")
    b.button(text="⬅️ Назад", callback_data="editw:back")
    b.adjust(1)
    return b.as_markup()


def csv_import_confirm_keyboard(new_count: int, dup_count: int) -> InlineKeyboardMarkup:
    """Кнопки подтверждения импорта CSV.

    Дубли по датам ни пропускаются молча, ни грузятся молча: обычная кнопка
    берёт только новые даты (и говорит, сколько их), а вторая появляется лишь
    при дублях — для тех, кто действительно тренировался в тот день дважды.
    Если новых дат нет, кнопки «загрузить N» нет вообще: одна и та же история
    дважды — это тот самый баг, из-за которого её потом удаляли по одной.
    """
    b = InlineKeyboardBuilder()
    if new_count:
        b.button(text=f"✅ Загрузить {new_count}", callback_data="imp:save")
    if dup_count:
        total = new_count + dup_count
        b.button(text=f"⚠️ Загрузить всё ({total}), включая дубли", callback_data="imp:saveall")
    b.button(text="❌ Отмена", callback_data="imp:cancel")
    b.adjust(1)
    return b.as_markup()


def csv_import_page_keyboard(
    page: int, total_pages: int, new_count: int, dup_count: int
) -> InlineKeyboardMarkup:
    """Экран подтверждения импорта — несколько тренировок на странице, как
    список в истории: переключалки влево/вправо между страницами файла, а
    решение «загрузить» общее для всех и не зависит от того, на какой
    странице стоишь.
    """
    b = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text=PAGE_PREV_TEXT, callback_data=f"imp:page:{page - 1}"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(text=PAGE_NEXT_TEXT, callback_data=f"imp:page:{page + 1}"))
    if nav:
        b.row(*nav)
    if new_count:
        # Без дублей это и есть весь файл — «Загрузить N» тогда просто
        # повторяет число из заголовка, а «всё» короче и не требует сверки.
        text = "✅ Загрузить всё" if not dup_count else f"✅ Загрузить новые ({new_count})"
        b.row(InlineKeyboardButton(text=text, callback_data="imp:save"))
    if dup_count:
        total = new_count + dup_count
        b.row(InlineKeyboardButton(
            text=f"⚠️ Загрузить всё ({total}), включая дубли", callback_data="imp:saveall"
        ))
    b.row(InlineKeyboardButton(text="❌ Отмена", callback_data="imp:cancel"))
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
