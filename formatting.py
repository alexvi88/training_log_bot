"""Pure text-formatting helpers — build user-facing messages from plain data.

Kept independent of the DB layer so it can be unit tested directly: handlers
are responsible for turning DB rows into the small view dataclasses below.
"""

import datetime as dt
import re
from collections import Counter
from dataclasses import dataclass, field
from html import escape
from typing import Callable, Literal, Optional

from aiogram.types import MessageEntity

import config
import i18n
import seed_data
from analytics import (
    RANK_FREQUENCY_WEEKS,
    VOLUME_WINDOW_DAYS,
    classify_weekly_volume,
    e1rm,
)

# Ключи ICU-select date.weekday_short (locales/*.json) — порядок как у
# datetime.weekday() (0 = понедельник).
_WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

def unit_label(unit: str) -> str:
    """Единица веса с учётом языка: «кг»/«kg» по каталогу, lb — как есть (то
    же сокращение в обоих языках).

    Раньше здесь был приватный `_unit_label` рядом с публичным словарём
    `UNIT_LABELS = {"kg": "кг", "lb": "lb"}` — тот словарь читали напрямую
    handlers/workout.py и handlers/bodyweight.py через `.get(unit, "кг")` с
    русским фолбэком в вызове, и англоязычный с килограммами видел «кг» (в
    bodyweight.py это ещё и уезжало в подпись графика). Словарь убран, все
    вызовы переведены на эту функцию.
    """
    return "lb" if unit == "lb" else i18n.t("unit.kg")


DIVIDER = "─" * 10

_TAG_RE = re.compile(r"<[^>]+>")

# A photo caption tops out at 1024 characters where a plain message gets 4096,
# and screens that ride along with a chart (the progress screen) live under the
# smaller cap. Overflowing it isn't a soft failure: ui.safe_edit_photo deletes
# the old screen before re-sending, so a too-long caption leaves the user with
# no screen at all.
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096

# Название приёма пищи в дневнике: при выключенном КБЖУ туда попадает текст
# пользователя как есть, а он ограничен только лимитом сообщения Telegram —
# одна такая запись способна распереть экран дня за 4096 и сделать день
# неоткрываемым (кнопки удаления живут на этом же экране).
FOOD_DESC_LIMIT = 200

# Worth folding only above this much content — see collapsible_if_long. Six lines
# is twice what the collapsed box occupies; the character threshold catches prose,
# which wraps into many screen lines without containing a single newline.
FOLD_MIN_LINES = 6
FOLD_MIN_CHARS = 300

# e1RM is an abbreviation for a concept most people have never met, and the
# number itself gives no hint that it's a calculation rather than a lift that
# happened. The footnote is permanent on the progress screen: that's the screen
# you open to interpret the metric, so a standing line there is reference
# material. Under the completion card it shows only for the first few workouts
# (handlers.workout._should_explain_e1rm) — that card is where a newcomer meets
# the term, but it's also read after every session, and a permanent line there
# would be something to scroll past instead of read.
def _e1rm_hint() -> str:
    return i18n.t("progress.e1rm_hint")


_MODULE_ATTR_KEYS = {
    "UNGROUPED_LABEL": "dashboard.ungrouped_label",
    "MENU_LIFTS_NOTE": "dashboard.lifts_note",
}


def __getattr__(name: str) -> str:
    """PEP 562: `formatting.E1RM_HINT`/`UNGROUPED_LABEL`/`MENU_LIFTS_NOTE`
    остаются рабочими module-level атрибутами для handlers/workout.py и
    тестов, но значение теперь читается из каталога по текущему языку при
    каждом обращении, а не застывает один раз при импорте модуля (что
    случилось бы с обычной константой)."""
    if name == "E1RM_HINT":
        return _e1rm_hint()
    if name in _MODULE_ATTR_KEYS:
        return i18n.t(_MODULE_ATTR_KEYS[name])
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def local_time_entity(moment: dt.datetime, fallback: str) -> tuple[str, MessageEntity]:
    """Момент времени, который клиент покажет в поясе смотрящего (Bot API 9.5).

    Возвращает (текст, entity): текст — обычный фолбэк, который увидят старые
    клиенты, entity накрывает его целиком и заставляет новые нарисовать то же
    время по своим часам. Выигрыш не в том, чтобы не считать пояс самим, — а в
    том, что для большинства он посчитан неверно: пояс в настройках бота никто
    не выставляет, и вся сдвижка идёт от нуля.

    Хранится по-прежнему серверное время (см. UX_IMPROVEMENTS_PART2.md) —
    entity лечит отображение, а не корень.
    """
    return fallback, MessageEntity(
        type="date_time",
        offset=0,
        length=len(fallback.encode("utf-16-le")) // 2,
        unix_time=int(moment.replace(tzinfo=dt.timezone.utc).timestamp()),
    )


def entities_at(text: str, marker: str, entity: MessageEntity) -> list[MessageEntity] | None:
    """Сдвинуть entity на позицию `marker` внутри `text`.

    Смещения Telegram считает в UTF-16, как и telegram_length, — кириллица и
    эмодзи до маркера иначе сдвинут метку на чужие символы.
    """
    index = text.find(marker)
    if index < 0:
        return None
    prefix_len = len(text[:index].encode("utf-16-le")) // 2
    return [entity.model_copy(update={"offset": prefix_len})]


def format_group(name: str) -> str:
    """A muscle group's name as it's shown anywhere in the UI: uppercase.

    The group is context around whatever it labels (an exercise name, a card
    heading), never the thing itself — caps read as a tag at a glance and stop
    the group competing with the name next to it. Applied at render time only:
    the stored name keeps whatever case the user typed.

    Язык выбирается здесь же, а не у вызывающих. Группы мышц — глобальные строки
    (`muscle_groups.user_id IS NULL`), их никогда не форкают в аккаунт, поэтому
    в базе имя навсегда остаётся русским пресетом, и перевести его можно только
    на рендере. Единственная точка рендера — вот эта функция, через неё идут и
    клавиатура выбора группы, и панель объёма, и теги рядом с названием
    упражнения (format_group_tag). Локализуй у вызывающих — и каждый новый
    вызов будет молча показывать англоязычному русское слово, что и произошло
    в трёх местах сразу, пока это жило снаружи.

    Своя группа пользователя слага не имеет и возвращается как есть: это его
    данные, а не наш пресет.
    """
    return seed_data.localized_muscle_group_name(name, i18n.get_lang()).upper()


def format_group_lower(name: str) -> str:
    """Группа строчными и на языке атлета — для строк, где она стоит внутри
    фразы, а не тегом («💤 ещё не отдохнули: грудь — 0%»).

    Живой QA поймал ровно то, о чём предупреждает докстринг format_group: строка
    восстановления звала `.lower()` на сырое имя из базы, потому что общий
    рендер отдаёт капс, — и англоязычный атлет читал «грудь». Локализация должна
    оставаться внутри formatting, иначе следующему регистру понадобится своя
    копия и она снова протечёт.
    """
    return seed_data.localized_muscle_group_name(name, i18n.get_lang()).lower()


def format_group_tag(name: str) -> str:
    """Группа в квадратных скобках рядом с названием упражнения — тем же капсом,
    что и везде (см. format_group): «Румынская тяга [НОГИ]», один регистр на
    весь бот, а не два разных в зависимости от того, стоит группа одна или
    в скобках рядом с именем.

    Экранирование остаётся на вызывающем: этим же тегом подписаны и картинки,
    где HTML не при чём.
    """
    return format_group(name.strip())


def clamp_caption(text: str, limit: int = CAPTION_LIMIT) -> str:
    """Подпись к фото, гарантированно влезающая в лимит Telegram.

    Страховка, а не основная проверка: длину описания ограничивает ввод (см.
    config.MAX_EXERCISE_DESCRIPTION_LENGTH). Но в подпись едет не только оно, а
    ещё имя, группа, оснастка и дата, и описание может приехать не из ввода —
    из каталога или импорта. Превышение лимита Telegram отклоняет всё сообщение
    целиком, то есть карточка не показывается вовсе, поэтому обрезанная подпись
    здесь честнее исключения.

    Резать по границе строки: обрыв посреди слова читается как сбой, а
    оборванный HTML-тег вообще не даёт Telegram разобрать разметку.
    """
    if len(text) <= limit:
        return text
    kept: list[str] = []
    used = 0
    for line in text.split("\n"):
        if used + len(line) + 1 > limit - 1:
            break
        kept.append(line)
        used += len(line) + 1
    return ("\n".join(kept) + "…") if kept else text[: limit - 1] + "…"


def strip_tags(text: str) -> str:
    """Текст без HTML-разметки — для мест, где разметку не разбирают
    (rich-блоки, лог)."""
    return _TAG_RE.sub("", text)


def telegram_length(text: str) -> int:
    """Length as Telegram counts it: markup is parsed into entities and doesn't
    count toward the limit, and characters are measured in UTF-16 code units —
    so an emoji costs two, not one."""
    return len(_TAG_RE.sub("", text).encode("utf-16-le")) // 2


def shorten(text: str, limit: int) -> str:
    """Обрезать по границе слова с многоточием, если длиннее лимита."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rstrip()
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,.;:—-") + "…"


def collapsible_if_long(text: str) -> str:
    """Fold, but only when there is enough to hide.

    A collapsed block is itself a quote box about three lines tall, so folding a
    short list costs the room it saves — and worse, the box draws the eye to the
    part that was judged least important. Below the thresholds the text is
    returned untouched.
    """
    if text.count("\n") + 1 >= FOLD_MIN_LINES or telegram_length(text) >= FOLD_MIN_CHARS:
        return collapsible(text)
    return text


def collapsible(text: str) -> str:
    """Fold a block behind Telegram's expandable blockquote (Bot API 7.4+).

    It renders collapsed to three lines with an expand chevron, and unfolding
    happens entirely on the client — no callback, no edit, no screen re-send —
    which is what makes it cheaper than an inline button for hiding bulk text.
    Clients too old to know the entity draw it as a plain quote, so nothing is
    ever unreachable. Folding does not shorten the message: hidden text still
    counts toward the 4096/1024 limits.

    Callers pass text that is already escaped/marked up: the tags are added
    around it, never to it.
    """
    return f"<blockquote expandable>{text}</blockquote>"


def plural_ru(n: int, forms: tuple[str, str, str]) -> str:
    """Russian plural: forms = (1 единица, 2-4 единицы, 5+ единиц)."""
    m = abs(n) % 100
    last = m % 10
    if 11 <= m <= 14:
        return forms[2]
    if last == 1:
        return forms[0]
    if 2 <= last <= 4:
        return forms[1]
    return forms[2]


def format_weight(weight: float) -> str:
    if weight == int(weight):
        return str(int(weight))
    return f"{weight:.1f}".rstrip("0").rstrip(".")


def format_rpe(rpe: float | None) -> str:
    """Trailing "@9" / "@8.5" suffix for a set, or empty string when no RPE was logged."""
    if rpe is None:
        return ""
    return f" @{format_weight(rpe)}"


def format_set(weight: float, reps: int, rpe: float | None = None) -> str:
    return f"{format_weight(weight)}×{reps}{format_rpe(rpe)}"


# «5x3-5», «5 х 3 - 5», «4X8» — как схему пишет человек. Разделитель подходов:
# латинская x, русская х, знак умножения. Диапазон повторов: дефис, тире, длинное
# тире. Всё это должно приезжать к одному виду, а не соседствовать в одном списке.
_TARGET_RE = re.compile(
    r"^\s*(?P<sets>\d{1,2})\s*[xX×хХ]\s*(?P<lo>\d{1,3})\s*(?:[-–—]\s*(?P<hi>\d{1,3}))?\s*$"
)


def parse_routine_target(text: str) -> tuple[int, int, int | None] | None:
    """«4x6-10» → (4, 6, 10). None, если это не схема «подходы × повторы».

    None — обычный ответ, а не ошибка: в каталоге есть «3×30–60 сек», и строго
    отбивать неразобранное значило бы запретить формат, который бот использует
    сам. Разбираем то, что разбирается, остальное оставляем как есть.
    """
    match = _TARGET_RE.match(text or "")
    if match is None:
        return None
    sets = int(match.group("sets"))
    lo = int(match.group("lo"))
    hi = int(match.group("hi")) if match.group("hi") else None
    if not sets or not lo or (hi is not None and hi < lo):
        return None
    return sets, lo, hi


def planned_rep_range(text: str | None) -> tuple[int, int] | None:
    """Диапазон повторов из схемы («3×6–12» → (6, 12)), если он там есть.

    Нужен подсказке прогрессии: «🎯 Цель» стоит на экране прямо под «📋 План», и
    цель выше плановой верхушки — это спор бота с самим собой. Одиночное число
    повторов («3×8») диапазоном не считаем: расти там некуда, и догадка про
    диапазон осталась бы догадкой.
    """
    parsed = parse_routine_target(text or "")
    if parsed is None:
        return None
    _sets, lo, hi = parsed
    return (lo, hi) if hi else None


def normalize_routine_target(text: str) -> str:
    """Привести схему к тому же виду, в котором её пишем мы сами.

    Без этого рядом в одном дне живут «4×6–10» от генератора и «5x3-5» от
    человека — одна и та же вещь двумя разными наборами символов.
    """
    parsed = parse_routine_target(text)
    if parsed is None:
        return (text or "").strip()
    sets, lo, hi = parsed
    return build_routine_target(sets, lo, hi)


def build_routine_target(
    sets: int | None, reps_min: int | None, reps_max: int | None
) -> str:
    """«3×5–8» — routine_exercises.target из подходов и повторов, которые назвал
    AI-тренер (см. ai_trainer.propose_program).

    Та же free-form строка, что у готовых программ в seed_data, и попадает она
    в то же поле — значит и на карточке программы, и подсказкой «📋 План» во
    время тренировки выглядит одинаково, кто бы программу ни собрал.

    Пустая строка, если схемы нет вовсе: тогда у упражнения останется пустой
    target, как у программы, снятой с тренировки.
    """
    reps = ""
    if reps_min and reps_max and reps_max != reps_min:
        reps = f"{reps_min}–{reps_max}"
    elif reps_min or reps_max:
        reps = str(reps_min or reps_max)
    if sets and reps:
        return f"{sets}×{reps}"
    if sets:
        return i18n.t("routine.target_sets_only", sets=sets)
    return reps


def format_date_ru(d: dt.datetime) -> str:
    """«07.08.2026 (чт)» / «08.07.2026 (Thu)» — число всегда цифрами (форма
    даты не зависит от языка), день недели — по каталогу (date.weekday_short)."""
    wd = i18n.t("date.weekday_short", wd=_WEEKDAY_KEYS[d.weekday()])
    return i18n.t("date.full", date=d.strftime("%d.%m.%Y"), wd=wd)


# Ключи ICU-select date.month_gen (locales/*.json) — по номеру месяца
# datetime.month (1 = январь). Родительный падеж — только у русского значения
# ключа («12 января»), у английского там обычное название месяца («January»).
_MONTH_KEYS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def format_day_month_ru(d: dt.date, today: dt.date | None = None) -> str:
    """"20 июля" / "July 20" — for prose and button labels, where dd.mm.yyyy
    reads as a form field. Год добавляется только когда он не текущий
    ("20 июля 2025") — иначе он не несёт новой информации, а на кнопке
    отъедает место у самой даты.

    Порядок «число месяц» намеренно не зеркалится под английский («20 July»
    вместо «July 20»): это не про месяцы как такие, а про естественный
    порядок слов в каждом языке (см. TONE_OF_VOICE.md — «Даты»), и каталог
    хранит оба порядка отдельными ключами (date.day_month/date.day_month_year).
    """
    month = i18n.t("date.month_gen", m=_MONTH_KEYS[d.month - 1])
    today = today or dt.date.today()
    if d.year != today.year:
        return i18n.t("date.day_month_year", day=d.day, month=month, year=d.year)
    return i18n.t("date.day_month", day=d.day, month=month)


def format_duration(seconds: float) -> str:
    total_minutes = round(seconds / 60)
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return i18n.t("date.duration_h_m", h=hours, m=minutes)
    if hours:
        return i18n.t("date.duration_h", h=hours)
    return i18n.t("date.duration_m", m=minutes)


@dataclass
class ExerciseBlockView:
    group_name: str
    exercise_name: str
    sets: list[tuple[float, int]]  # weight, reps
    formula: str = "epley"
    type: Literal["single"] = "single"
    exercise_id: int | None = None
    prev_sets: list[tuple[float, int]] | None = None  # sets from the previous session, if any
    set_rpes: list[float | None] | None = None  # per-set RPE, aligned with `sets`; None = none logged
    prev_set_rpes: list[float | None] | None = None  # per-set RPE for prev_sets
    prev_started_at: dt.datetime | None = None  # date of the previous session, for the delta line
    note: str | None = None  # exercise's own note (technique cue, injury flag)
    # Index into `sets` of the set that is the exercise's new all-time-best
    # e1RM — the live 🥇 mark. None when nothing in this session beats history.
    gold_index: int | None = None
    # Фактическая нагрузка каждого подхода (db.load_of), выровнена с `sets`.
    # Отдельным полем, потому что в `sets` лежит то, что записал человек
    # («0×12» подтягиваний), а вся арифметика — e1RM, рекорды, графики — считает
    # по нагрузке: вес тела плюс добавка. Пока карточка считала e1RM по сырому
    # весу, один и тот же подход показывал 11.7 кг на экране тренировки и 105 кг
    # рекордом в зале славы. None — обычное железо: нагрузка равна записанному.
    set_loads: list[float] | None = None
    # Рекорд, поставленный этим упражнением именно в этой тренировке: на
    # сколько e1RM обошёл прошлый лучший, а для упражнений своим весом —
    # повторы в подходе. Раньше это жило отдельным блоком «🔥 Рекорды и
    # сравнения» под карточкой, где имя упражнения приходилось называть заново,
    # и глаз бегал между двумя списками одного и того же. None — рекорда нет
    # (или это первая сессия упражнения, бить нечего). См. view_builder.
    record_e1rm_delta: float | None = None
    record_reps: int | None = None

    def load_for(self, index: int) -> float:
        """Нагрузка подхода — то, по чему считается e1RM (не то, что показано)."""
        if self.set_loads and index < len(self.set_loads):
            return self.set_loads[index]
        return self.sets[index][0]

    def rpe_for(self, index: int) -> float | None:
        if not self.set_rpes or index >= len(self.set_rpes):
            return None
        return self.set_rpes[index]

    def prev_rpe_for(self, index: int) -> float | None:
        if not self.prev_set_rpes or index >= len(self.prev_set_rpes):
            return None
        return self.prev_set_rpes[index]

    @property
    def tonnage(self) -> float:
        return sum(w * r for w, r in self.sets)

    @property
    def load_tonnage(self) -> float:
        """Тоннаж по фактической нагрузке — то же, что считает
        analytics.SessionStats.tonnage. Отличается от `tonnage` только там, где
        записанный вес не равен нагрузке (подтягивания «0×12» — это не ноль)."""
        return sum(self.load_for(i) * r for i, (_w, r) in enumerate(self.sets))

    @property
    def is_bodyweight(self) -> bool:
        return bool(self.sets) and all(w == 0 for w, _ in self.sets)

    @property
    def top_e1rm(self) -> float:
        # По нагрузке, а не по записанному весу: рекорды, графики и «прошлая»
        # считаются так же (db.LOAD_WEIGHT_SQL / db.load_of), и расходиться с
        # ними на том же самом подходе карточка не имеет права.
        if not self.sets:
            return 0.0
        return max(
            e1rm(self.load_for(i), r, self.formula) for i, (_w, r) in enumerate(self.sets)
        )

    @property
    def prev_top_e1rm(self) -> float:
        # prev_sets приходят из view_builder уже нагрузкой (db.load_of), так что
        # дельта «vs прошлая» сравнима с top_e1rm выше.
        if not self.prev_sets:
            return 0.0
        return max(e1rm(w, r, self.formula) for w, r in self.prev_sets)


# A workout is rendered as a flat list of exercise blocks. (Exercises logged in
# parallel — the "superset" entry mechanic — are stored as independent blocks and
# shown the same as any other exercise; there is no separate superset view type.)
BlockView = ExerciseBlockView


def format_date_short(d: dt.datetime) -> str:
    """Compact dd.mm for the inline e1RM-delta annotation — the year and weekday
    only add width there, since the comparison is always to the most recent prior
    session."""
    return d.strftime("%d.%m")


HISTORY_MAX_NAMES = 3


def _history_bullets(names: list[str]) -> list[str]:
    """Up to the first 3 exercise names as bullet lines; any rest are collapsed
    into a single "+N других" bullet rather than spilling the list further."""
    kept = names[:HISTORY_MAX_NAMES]
    lines = [f"• {escape(name)}" for name in kept]
    rest = len(names) - len(kept)
    if rest:
        lines.append(f"• {i18n.t('history.more_exercises', n=rest)}")
    return lines


def build_history_list(
    entries: list[tuple[dt.datetime, list[str], int]],
    header: str | None = None,
    footer: str = "",
    empty: str | None = None,
) -> str:
    """The history list's body: date, then what was in that session.

    The exercise names live here rather than on the buttons because real ones
    ("conventional deadlift", "abs - pull down block") run 20-30 characters —
    two of them already overflow a button label, while the message body has
    thousands of characters going spare.

    `header`/`empty` default to None, а не готовой русской строке: обычной
    константой в сигнатуре значение застыло бы на языке, который был активен
    в момент импорта модуля, а не вызова функции (handlers.history зовёт эту
    функцию без этих аргументов в самом частом случае — просмотре истории).
    """
    if not entries:
        return empty if empty is not None else i18n.t("history.empty")
    lines = [header if header is not None else i18n.t("history.header")]
    for started, names, _set_count in entries:
        head = format_date_ru(started)
        lines.append("")
        lines.append(f"<b>{head}</b>")
        if names:
            lines.extend(f"<i>{b}</i>" for b in _history_bullets(names))
        else:
            lines.append(f"<i>{i18n.t('history.day_empty')}</i>")
    if footer:
        lines.append("")
        lines.append(footer)
    return "\n".join(lines)


def build_import_confirmation_list(
    entries: list[tuple[dt.date, list[str]]],
    dup_dates: set,
    header: str,
) -> str:
    """Same shape as build_history_list (date, then a few exercise names as
    bullets) — an import confirmation is a preview of history-to-be, and
    showed as a wall of per-set weights it read as a completely different,
    more overwhelming screen instead of just more of the same list. Dates
    already in `dup_dates` are flagged inline, since with several workouts per
    page a single blanket warning can't say which ones are affected."""
    lines = [header]
    for date, names in entries:
        head = f"<b>{format_date_ru(date)}</b>"
        if date.isoformat() in dup_dates:
            head += i18n.t("history.import_duplicate")
        lines.append("")
        lines.append(head)
        if names:
            lines.extend(f"<i>{b}</i>" for b in _history_bullets(names))
        else:
            lines.append(f"<i>{i18n.t('history.day_empty')}</i>")
    return "\n".join(lines)


def _delta_arrow(delta: float) -> str:
    return "↑" if delta > 0 else ("↓" if delta < 0 else "→")


def format_delta(delta: float, unit: str = "kg") -> str:
    """Стрелка и величина изменения БЕЗ знака: «↓38кг», «↑2.5кг», «→0кг».

    Знак несёт стрелка, поэтому «+»/«−» рядом с ней — второй знак об одном и том
    же. На проде это выглядело как «↓-38.0кг vs 11.08» и читалось как опечатка.
    Заодно вес идёт через format_weight: «66кг» вместо «66.0кг» — в остальной
    карточке веса давно так и печатаются.
    """
    u = unit_label(unit)
    return f"{_delta_arrow(delta)}{format_weight(abs(delta))}{u}"


def format_delta_reps(delta: int) -> str:
    """То же для повторов: «↑3», «↓2», «→0» — без второго знака рядом со стрелкой."""
    return f"{_delta_arrow(delta)}{abs(delta)}"


def to_kg(total: float, unit: str = "kg") -> float:
    """Weights are stored in whatever unit the user picked, so anything compared
    against a real-world quantity (a ton, an elephant) has to be normalized."""
    return total / config.LB_PER_KG if unit == "lb" else total


def format_tonnage(total: float, unit: str = "kg") -> str:
    """Session/lifetime tonnage as a full word ("тонны"/"тонн"), never abbreviated.

    `total` is in the user's own unit. A ton is a ton, so the threshold and the
    figure are computed in kilograms — a lb user lifting 20 000 lb has moved
    9 tons, not 20. Below a ton there's nothing to convert: their own number in
    their own unit is what they want to see.

    Russian grammar: a non-whole amount (e.g. "1.5 тонны") always takes the
    2-4 form regardless of the leading digit, so only a whole number of tons
    goes through the normal plural_ru rules.
    """
    u = unit_label(unit)
    total_kg = to_kg(total, unit)
    if total_kg >= 1000:
        tons = round(total_kg / 1000, 1)
        tons_str = format_weight(tons)
        # Дробное количество тонн всегда берёт форму «тонны» независимо от
        # ведущей цифры — форсируем это, подставляя в plural число 2 (форма
        # few по-русски), а не само дробное значение: ICU-плюрализация здесь
        # выбирает только слово, число на экране печатает {tons} отдельно.
        plural_n = int(tons) if tons == int(tons) else 2
        return i18n.t("tonnage.total", tons=tons_str, n=plural_n)
    return f"{total:.0f}{u}"


def format_block_record(
    block: ExerciseBlockView, unit: str = "kg", show_extra: bool = True,
) -> str | None:
    """Строка рекорда внутри блока упражнения — или None, если рекорда нет.

    Стоит рядом с подходами, которыми рекорд и поставлен: отдельный список
    «🔥 Рекорды и сравнения» под карточкой повторял имена упражнений и заставлял
    читать одну тренировку дважды. 🔥 — закреплённое эмодзи рекорда
    (TONE_OF_VOICE.md), и в блоке оно ровно одно.

    Рекорд e1RM молчит при выключенных доп. цифрах: человек, убравший строку
    «↳ e1RM», не должен получать e1RM через заднюю дверь. Рекорд повторов
    показывается всегда — он про повторы, которые и так на экране.
    """
    text = _block_record_text(block, unit, show_extra)
    return None if text is None else f"🔥 {text}"


def _block_record_text(
    block: ExerciseBlockView, unit: str, show_extra: bool = True
) -> str | None:
    """Фраза рекорда без значка — общая у текстовой карточки и картинки,
    которые различаются только им (🔥 против ★).

    Коротко и числом вперёд: «+1.3кг к рекорду», а не «Рекорд e1RM — на 1.3кг
    выше прошлого». На тренировке, где рекорд стоит в каждом из шести блоков,
    длинное предложение шесть раз подряд превращало карточку в полотно, а само
    число — то единственное, что человек тут читает, — стояло в середине.
    """
    if block.record_reps is not None:
        reps = block.record_reps
        return i18n.t("card.record_reps", reps=reps, n=reps)
    if block.record_e1rm_delta is not None and show_extra:
        u = unit_label(unit)
        return i18n.t("card.record_e1rm", delta=format_weight(block.record_e1rm_delta), u=u)
    return None


def _render_single_block(block: ExerciseBlockView, show_extra: bool, unit: str = "kg") -> list[str]:
    u = unit_label(unit)
    label = f"{escape(block.exercise_name)} [{escape(format_group_tag(block.group_name))}]"
    lines = [f"<b>{label}</b>"]
    if block.note:
        lines.append(f"  📝 <i>{escape(block.note)}</i>")
    if block.sets:
        # Одной строкой через запятую — тем же форматом, которым ниже печатается
        # «[прошлая: …]». Столбик буллетов на восемь упражнений разгонял карточку
        # на три экрана, и одна и та же тренировка выглядела в двух разных видах:
        # сегодняшние подходы столбиком, прошлые — строкой.
        #
        # Одинаковые подходы подряд НЕ сворачиваются: «180×4, 180×4» честнее
        # любой приписки. «180×4 ×2» склеивалось глазом в один подход с третьим
        # числом, «180×4 (2 подхода)» в списке через запятую повисало так, будто
        # счёт про весь список. Когда подходы стояли столбиком, свёртка экономила
        # строки — в одну строку экономить уже нечего.
        formatted = [format_set(w, r, block.rpe_for(i)) for i, (w, r) in enumerate(block.sets)]
        lines.append(f"  {', '.join(formatted)}")
    else:
        lines.append(f"  <i>{i18n.t('card.no_sets')}</i>")
    # Прошлый рекорд стоял с прошлой же тренировки — тогда «↑+5.7 vs 03.08» и
    # «на 5.7 выше прошлого» это одно и то же число дважды. Строка e1RM в этом
    # случае остаётся голой — сравнение уже сказано строкой рекорда.
    prev_holds_the_record = (
        block.record_e1rm_delta is not None
        and block.prev_sets is not None
        and block.prev_started_at is not None
        and abs((block.top_e1rm - block.prev_top_e1rm) - block.record_e1rm_delta) < 0.05
    )
    # Порядок строк: подходы → рекорд → e1RM → прошлая. Рекорд стоит сразу под
    # подходами, которыми он и поставлен, — это главное, что человек в блоке
    # ищет, и оно не должно ждать своей очереди за расчётным максимумом.
    record = format_block_record(block, unit, show_extra)
    if record:
        lines.append(f"  {record}")
    if show_extra and block.sets and not block.is_bodyweight:
        vs_prev = ""
        if block.prev_sets and block.prev_started_at is not None and not prev_holds_the_record:
            when = format_date_short(block.prev_started_at)
            delta = block.top_e1rm - block.prev_top_e1rm
            vs_prev = f" ({format_delta(delta, unit)} vs {when})"
        lines.append(f"  ↳ e1RM {format_weight(block.top_e1rm)}{u}{vs_prev}")
    if block.prev_sets:
        formatted_prev = [format_set(w, r, block.prev_rpe_for(i)) for i, (w, r) in enumerate(block.prev_sets)]
        prev_str = ", ".join(formatted_prev)
        lines.append(f"<i>{i18n.t('card.previous_sets', sets=prev_str)}</i>")
    return lines


def build_workout_summary(
    started_at: dt.datetime,
    blocks: list[BlockView],
    note: str | None = None,
    show_extra_stats: bool = True,
    duration_seconds: float | None = None,
    unit: str = "kg",
    max_chars: int | None = None,
) -> str:
    """max_chars: if the rendered text would exceed this, oldest exercises are
    dropped from the tail (with a "показано N из M" marker) until it fits —
    see fit_workout_text for why this can't be a simple truncate.
    """
    header = f"<b>{format_date_ru(started_at)}</b>"
    if duration_seconds is not None:
        header += f" · {format_duration(duration_seconds)}"
    head_lines = [header]
    if note:
        head_lines.append(f"📝 {escape(note)}")
    head_lines.append("")

    def assemble(keep: list[BlockView]) -> str:
        lines = list(head_lines)
        for i, block in enumerate(keep):
            if i > 0:
                lines.append("")
            lines.extend(_render_single_block(block, show_extra_stats, unit))
        text = "\n".join(lines)
        if len(keep) < len(blocks):
            text += i18n.t("card.truncated", kept=len(keep), total=len(blocks))
        return text

    kept = blocks
    text = assemble(kept)
    while max_chars is not None and len(kept) > 1 and telegram_length(text) > max_chars:
        kept = kept[:-1]
        text = assemble(kept)
    return text


def workout_composition(blocks: list[BlockView], unit: str = "kg") -> str:
    """Состав тренировки — упражнения, группы и подходы, без даты/заголовка,
    без тоннажа и рекордов. Для мест, где дата и так названа отдельной фразой
    (предупреждение о незакрытой тренировке): полный build_workout_summary
    поверх неё дал бы вторую дату и заголовок над тем же самым списком.
    """
    lines: list[str] = []
    for i, block in enumerate(blocks):
        if i > 0:
            lines.append("")
        lines.extend(_render_single_block(block, show_extra=False, unit=unit))
    return "\n".join(lines)


def fit_workout_text(build_summary, suffix: str, limit: int = MESSAGE_LIMIT) -> str:
    """Guards a workout card against Telegram's 4096-char message cap.

    `build_summary` is a callable(max_chars) -> str producing the header+sets
    portion (see build_workout_summary's max_chars). `suffix` is everything
    else already assembled (tonnage, PR highlights, achievements, AI comment)
    — it doesn't depend on the summary, so its length is known up front and
    becomes the summary's budget. This can't be a length estimate on the
    combined text either: ui.safe_edit deletes the old screen before sending
    the new one, so overflowing silently would leave the user with nothing.
    """
    text = build_summary(None)
    full = text + suffix
    if telegram_length(full) <= limit:
        return full
    reserve = telegram_length(full) - telegram_length(text)
    budget = max(limit - reserve - 20, 200)
    return build_summary(budget) + suffix


_MD_HEADING_RE = re.compile(r"^#{1,6}[ \t]+(.*)$", re.MULTILINE)
_MD_DIVIDER_RE = re.compile(r"^[ \t]*-{3,}[ \t]*$", re.MULTILINE)
# Ячейку от ячейки отделяет неэкранированная палка: «\|» внутри текста ячейки
# сама по себе разделителем не является.
_MD_CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")

# Вся строчная разметка одним проходом. Порядок веток значим: «**» проверяется
# раньше «*», иначе жирный разобрался бы как курсив вокруг пустоты.
#
# Ни одна ветка, кроме блока кода, не переходит на другую строку (`[^\n]`,
# точка без DOTALL): непарная звёздочка в одном абзаце иначе съела бы полтекста
# до следующей такой же. Курсиву дополнительно запрещено начинаться с пробела —
# так пункт списка «* тяга 260» не превращается в курсив до следующей звёздочки,
# — и стоять вплотную к букве или цифре, чтобы snake_case и «100_000» остались
# собой.
_MD_MARKUP_RE = re.compile(
    r"(?s:```[^\n]*\n(?P<pre>.*?)```)"
    r"|`(?P<code>[^`\n]+)`"
    r"|\*\*(?P<bold>[^\n]+?)\*\*"
    r"|__(?P<bold_alt>[^\n]+?)__"
    r"|~~(?P<strike>[^\n]+?)~~"
    r"|\[(?P<link>[^\]\n]+)\]\((?P<href>[^)\s]+)\)"
    r"|(?<![\w*])\*(?P<italic>[^*\s][^*\n]*)\*(?![\w*])"
    r"|(?<![\w_])_(?P<italic_alt>[^_\s][^_\n]*)_(?![\w_])"
)


def _render_markup(m: re.Match) -> str:
    """Одно совпадение _MD_MARKUP_RE → тег Telegram. Содержимое экранируется
    здесь же: наружу из этой функции неэкранированный текст не выходит."""
    if m.group("pre") is not None:
        return f"<pre>{escape(m.group('pre'))}</pre>"
    if m.group("code") is not None:
        return f"<code>{escape(m.group('code'))}</code>"
    if m.group("link") is not None:
        return f'<a href="{escape(m.group("href"), quote=True)}">{escape(m.group("link"))}</a>'
    for group, tag in (("bold", "b"), ("bold_alt", "b"), ("strike", "s"),
                       ("italic", "i"), ("italic_alt", "i")):
        if m.group(group) is not None:
            return f"<{tag}>{escape(m.group(group))}</{tag}>"
    return escape(m.group(0))


def _is_table_delimiter(line: str) -> bool:
    """Строка-разделитель шапки markdown-таблицы («|---|---|», «|:--|--:|»).

    Именно она отличает таблицу от обычного текста, в котором просто попалась
    палка, — поэтому таблицей считается только пара «строка + разделитель под
    ней», а не всё подряд с «|».
    """
    stripped = line.strip()
    return "|" in stripped and "-" in stripped and not stripped.strip("|:- \t")


def _split_cells(row: str) -> list[str]:
    stripped = row.strip().strip("|")
    return [cell.strip().replace("\\|", "|") for cell in _MD_CELL_SPLIT_RE.split(stripped)]


def _table_as_lines(header: list[str], rows: list[list[str]]) -> list[str]:
    """Таблица построчно: «первая ячейка — Шапка2: ячейка2 · Шапка3: ячейка3».

    Разворот в строки, а не выравнивание пробелами: на телефоне таблица шире
    двух колонок всё равно не помещается по ширине, а моноширинный блок с
    выравниванием на узком экране рвётся переносами ещё некрасивее, чем
    исходные палки. Имя из шапки уезжает к каждому значению — иначе, потеряв
    выравнивание, значения теряют и смысл.
    """
    lines: list[str] = []
    for row in rows:
        label = row[0] if row else ""
        rest = [
            f"{header[i]}: {cell}" if i < len(header) and header[i] else cell
            for i, cell in enumerate(row[1:], start=1)
            if cell
        ]
        if label and rest:
            lines.append(f"{label} — {' · '.join(rest)}")
        elif label or rest:
            lines.append(label or " · ".join(rest))
    return lines


# Ячейка длиннее этого — уже не ячейка, а предложение. Порог примерно по
# ширине телефонного экрана в половину таблицы: «3×5–10», «210×3», название
# упражнения и короткая пометка укладываются с запасом, а фраза вроде
# «тяжёлый присед + тяжёлая тяга в одной неделе = надо разводить» — нет.
MAX_TABLE_CELL_CHARS = 50


def _iter_tables(text: str):
    """Все markdown-таблицы текста как (шапка, строки, ячеек в разделителе)."""
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        if "|" in lines[i] and i + 1 < len(lines) and _is_table_delimiter(lines[i + 1]):
            header = _split_cells(lines[i])
            delimiter_cells = len(_split_cells(lines[i + 1]))
            i += 2
            rows = []
            while i < len(lines) and "|" in lines[i]:
                rows.append(_split_cells(lines[i]))
                i += 1
            yield header, rows, delimiter_cells
            continue
        i += 1


def has_markdown_table(text: str) -> bool:
    """Есть ли в тексте таблица, которую стоит рисовать таблицей.

    Таблица — единственное, чего обычное сообщение не умеет вовсе (см.
    markdown_tables_to_lines: там она разбирается на строки, потому что другого
    выхода нет). Всё прочее — заголовки, списки, жирный — обычным сообщением
    передаётся не хуже, поэтому именно по этому признаку и решается, слать ли
    ответ rich-сообщением (см. handlers.ai_trainer._handle_question).

    Но таблица таблице рознь, и признать её таблицей нужно ровно тогда, когда её
    признает Telegram — иначе он покажет разметку текстом.

    Во-первых, число ячеек в строке-разделителе обязано совпадать с шапкой: это
    требование GFM, и при расхождении таблицы нет вовсе. Модель промахивается
    этим регулярно — кладёт «|---|---|---|» под шапку из двух колонок, — и тогда
    в rich-сообщении весь блок склеивался в один абзац с палками и дефисами
    внутри, потому что по правилам markdown одиночные переводы строк внутри
    абзаца съедаются. Наш собственный разворот в строки (markdown_tables_to_lines)
    к числу колонок терпим и разбирает такую таблицу правильно, поэтому кривую
    таблицу выгоднее увести на обычный путь, чем отдать Telegram.

    Во-вторых, модели регулярно кладут в ячейку целое предложение, хотя промпт
    просит короткие; на телефоне такая ячейка переносится на четыре строки,
    высота строки равняется по самой высокой, и таблица превращается в решётку из
    пустоты. Развёрнутая в строки, та же самая пара читается лучше — поэтому
    «таблица из предложений» здесь тоже честно считается не-таблицей.
    """
    return any(
        delimiter_cells == len(header)
        and all(len(cell) <= MAX_TABLE_CELL_CHARS for row in [header, *rows] for cell in row)
        for header, rows, delimiter_cells in _iter_tables(text)
    )


def markdown_tables_to_lines(text: str) -> str:
    """Разворачивает markdown-таблицы в обычные строки, остальной текст не трогает.

    Нужно там, где ответ модели уходит обычным сообщением, а не rich-сообщением
    (см. handlers.ai_trainer): в обычном сообщении разметки таблиц нет вовсе, и
    «| Движение | Факт |» доезжает до пользователя ровно палками и дефисами.
    """
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        # Блок строк с палками, но БЕЗ строки-разделителя. Markdown-таблицей это не
        # является, поэтому мимо ветки ниже он проходил насквозь — и до человека
        # доезжал ровно палками («Этап | Ориентир | Фокус»). Модель забывает
        # разделитель регулярно, и просьбы в промпте это не гарантируют, поэтому
        # разворачиваем такой блок теми же строками, что и настоящую таблицу.
        # Вход по ШАПКЕ: две палки и больше, то есть от трёх колонок. Одиночная
        # палка слишком часто встречается в обычном тексте, чтобы считать её
        # таблицей. А вот продолжение блока терпимее — одной палки достаточно:
        # у кривой «таблицы» строки бывают короче шапки, и на такой строке блок
        # обрывался, оставляя хвост палками на экране.
        if (
            lines[i].count("|") >= 2
            and not _is_table_delimiter(lines[i])
            and i + 1 < len(lines)
            and "|" in lines[i + 1]
            and not _is_table_delimiter(lines[i + 1])
        ):
            block: list[list[str]] = []
            while i < len(lines) and "|" in lines[i] and not _is_table_delimiter(lines[i]):
                block.append(_split_cells(lines[i]))
                i += 1
            header, *body_rows = block
            for row in body_rows:
                pairs = [
                    f"{head}: {cell}"
                    # strict=False намеренно: у кривой «таблицы» строки бывают
                    # разной длины, и падать на этом нельзя — лишние ячейки просто
                    # отбрасываются вместе с непарной шапкой.
                    for head, cell in zip(header, row, strict=False)
                    if cell and head
                ]
                out.append("• " + ", ".join(pairs) if pairs else "• " + " ".join(row))
            continue
        if "|" in lines[i] and i + 1 < len(lines) and _is_table_delimiter(lines[i + 1]):
            header = _split_cells(lines[i])
            i += 2
            body: list[list[str]] = []
            while i < len(lines) and "|" in lines[i]:
                body.append(_split_cells(lines[i]))
                i += 1
            out.extend(_table_as_lines(header, body))
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def markdown_headings_to_bold(text: str) -> str:
    """«## Заголовок» → жирная строка отдельным абзацем.

    Настоящий заголовок Telegram рисует по-статейному — крупным кеглем и с
    широкими полями сверху и снизу, — и строка «Твои цифры» посреди ответа в
    чате читается как разрыв, а не как подзаголовок. Rich-сообщение мы шлём
    ради таблиц, а не ради вёрстки статьи.

    Пустая строка вокруг — не косметика, а условие: «## Заголовок» отбивается
    сам, потому что это блок, а «**Заголовок**» без пустой строки — просто
    жирный кусок соседнего абзаца, и заголовок слипается с текстом намертво.
    Добавляем ровно там, где её нет: где модель отбила заголовок сама, второй
    пустой строки не появится.
    """
    out: list[str] = []
    blank_before_next = False
    for line in text.split("\n"):
        heading = _MD_HEADING_RE.match(line)
        if heading:
            if out and out[-1].strip():
                out.append("")
            # Внутренние «**» из текста заголовка убираем: строка и так станет
            # жирной целиком, а лишняя пара разрывает разметку. Модель пишет
            # «### Разбор **становая тяга**» — обёртка давала
            # «**Разбор **становая тяга****», парсер склеивал первую пару
            # «**Разбор **» в жирное, и хвост «****» уезжал пользователю
            # текстом. Ровно эти четыре звёздочки и торчали в ответе.
            out.append(f"**{heading.group(1).strip().replace('**', '')}**")
            blank_before_next = True
            continue
        if blank_before_next:
            blank_before_next = False
            if line.strip():
                out.append("")
        out.append(line)
    return "\n".join(out)


def ai_markdown_to_html(text: str) -> str:
    """Ответ модели (markdown) → HTML для обычного сообщения Telegram.

    Фолбэк-путь: там, где сервер и клиент умеют rich-сообщения, тот же ответ
    уходит markdown'ом целиком и разбирается самим Telegram (см.
    handlers.ai_trainer._send_rich_answer) — тогда и таблицы, и заголовки
    остаются настоящими. Здесь же вся разметка сводится к тому немногому, что
    понимает обычное сообщение:

    - **жирный** → <b>, *курсив* → <i>, ~~зачёркнутый~~ → <s>, `код` и блоки
      кода → <code>/<pre>, [текст](ссылка) → <a>. Всё, что разметкой не
      является, экранируется как обычный текст, поэтому шальная звёздочка или
      «<» в ответе не сломают сообщение. Пара **, разорванная на границе кусков
      (см. split_for_telegram), в обоих кусках останется буквальными
      звёздочками, а не открытым тегом.
    - «### Заголовок» → жирная строка, «---» → линия: ни заголовков, ни
      разделителей у обычного сообщения нет, и то и другое доехало бы до
      пользователя решётками и дефисами.
    - таблицы → строки (см. markdown_tables_to_lines).

    Списки не трогаем: «- тяга 260» и «1. присед» читаются как список и без
    всякой разметки — а вот всё остальное выше без разбора доезжало бы сырым.
    """
    text = markdown_tables_to_lines(text)
    text = _MD_DIVIDER_RE.sub(DIVIDER, text)
    text = markdown_headings_to_bold(text)
    parts = []
    pos = 0
    for m in _MD_MARKUP_RE.finditer(text):
        parts.append(escape(text[pos : m.start()]))
        parts.append(_render_markup(m))
        pos = m.end()
    parts.append(escape(text[pos:]))
    return "".join(parts)


def split_for_telegram(text: str, limit: int) -> list[str]:
    """Режет длинный ответ на сообщения по границам строк.

    Резать вслепую по счётчику символов нельзя: разрыв посреди строки таблицы
    оставляет в одном сообщении шапку, а в другом — хвост строки без единой
    палки, и ни один из кусков уже не разберётся ни как таблица, ни как текст.
    По строкам же куски остаются самостоятельными — и в rich-разметке, и в
    HTML-фолбэке. Строку длиннее лимита целиком сохранить всё равно нельзя,
    её режем как есть.
    """
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [text]


def format_milestone_line(total_finished: int) -> str:
    """Celebratory one-liner for a round finished-workout count (see analytics.is_workout_milestone)."""
    if total_finished == 1:
        return i18n.t("card.milestone_first")
    return i18n.t("card.milestone_n", n=total_finished)


def build_ai_comment_block(comment: str) -> str:
    """Rendered as a card section prefixed by DIVIDER — same convention as highlights.

    The comment itself is folded: it's a paragraph of prose sitting under the
    workout card, where the numbers are what the user came for. Collapsed it
    costs three lines and still reads as an opening sentence, and unfolding is
    a tap on the client (see collapsible).
    """
    return f"{DIVIDER}\n{i18n.t('card.ai_comment_header')}\n{collapsible_if_long(ai_markdown_to_html(comment))}"


def build_ai_comment_placeholder() -> str:
    """Пока комментарий генерируется в фоне (handlers.workout._attach_ai_comment) —
    карточка уходит без него и правится позже, и без этой строки человек видит
    ровно те же цифры, что и без AI-комментариев вовсе, и не понимает, ждать ли
    ответ или функция просто выключена."""
    return f"{DIVIDER}\n{i18n.t('card.ai_comment_placeholder')}"




def _day_key(name: str) -> str:
    return name.strip().lower()


def format_progression_rule(progression: Optional[dict], unit: Optional[str] = None) -> str:
    """Короткая человекочитаемая строка правила прогрессии (5.6/3.2 —
    ai_trainer._clean_progression, db.set_routine_exercise_progression): без неё
    превью показывало бы схему подходов, но не то, как её менять дальше, а это
    ровно то, что раньше терялось вместе с чатом.

    Модульного уровня, потому что нужна в двух местах: и в составе программы, и
    в блоке «что меняется» — правило, которое правка молча переписала, ничем не
    хуже изменившейся схемы и должно читаться так же.

    `unit` — "kg"/"lb" пользователя: «прибавь 2.5» без единицы читалось как
    «прибавь чего?». Параметр необязательный: не у всех вызовов единица под
    рукой, и тогда пишем нейтральное «к весу», а не выдуманные килограммы —
    у lb-пользователя они были бы враньём в 2.2 раза.
    """
    if not progression:
        return ""
    rule = progression.get("rule")
    step = progression.get("step")
    step_text = None
    if step:
        # Вплотную к числу, как везде в проекте («20кг», не «20 кг»).
        step_text = (
            f"{step:g}{unit_label(unit)}" if unit else i18n.t("program.step_neutral", step=f"{step:g}")
        )
    if rule == "double_progression":
        top = progression.get("reps_top")
        if top and step_text:
            return i18n.t("program.progression_double_at_top", top=top, step=step_text)
        return i18n.t("program.progression_double_generic")
    if rule == "linear_load":
        if step_text:
            return i18n.t("program.progression_linear_step", step=step_text)
        return i18n.t("program.progression_linear_generic")
    return ""


# Правило прогрессии общими словами, без шага: шаг у каждого упражнения свой
# (2.5 на тяге, 5 на жиме ногами, 1 на разведениях), и печатать его построчно
# значило семнадцать почти одинаковых строк подряд — состав программы в них
# тонул. Сам шаг человек всё равно увидит в тренировке, когда бот предложит вес.
_PROGRESSION_KIND_KEYS = {
    "double_progression": "program.kind_double",
    "linear_load": "program.kind_linear",
}


def progression_summary(days: list[dict]) -> tuple[str, Optional[str]]:
    """(общая строка про прогрессию, тип правила большинства) — или ("", None).

    Прогрессия — общий принцип программы, а не свойство каждой строки. Шага в
    превью нет вовсе: «прибавь 2.5кг» под каждым из восьми упражнений — это
    восемь почти одинаковых строк, в которых тонет сам состав, а конкретный шаг
    человек всё равно увидит в тренировке, на карточке упражнения.

    Раньше общая строка появлялась только при ПОЛНОМ совпадении типов правил, и
    одно упражнение с другим типом рушило всё: в живой программе на восемь
    упражнений семь двойных прогрессий и одна линейная давали семь повторов
    одной и той же фразы. Теперь берём самый частый тип, пишем его один раз
    наверху, а под упражнением строка остаётся только у исключений — тех, у кого
    тип ДРУГОЙ.

    Пусто, когда правило всего одно (повторять нечего) или когда самый частый
    тип встречается один раз — общая строка тогда не экономит ничего.
    """
    kinds = [
        (item.get("progression") or {}).get("rule") for day in days for item in day["items"]
    ]
    present = [kind for kind in kinds if kind in _PROGRESSION_KIND_KEYS]
    if len(present) < 2:
        return "", None
    top_kind, top_count = Counter(present).most_common(1)[0]
    if top_count < 2:
        return "", None
    kind_text = i18n.t(_PROGRESSION_KIND_KEYS[top_kind])
    # «Везде» — только когда это правда везде: у всех до единого упражнения тот
    # же тип. Иначе формулировка честная, её перебивает строка под упражнением.
    key = (
        "program.shared_kind_everywhere"
        if top_count == len(kinds)
        else "program.progression_shared_default"
    )
    return i18n.t(key, kind=kind_text), top_kind


def build_program_changes(
    old_days: list[dict], new_days: list[dict], unit: Optional[str] = None
) -> list[str]:
    """Построчно: что правка делает со старой программой.

    Дни и упражнения сопоставляются по имени (регистр и пробелы не в счёт) —
    id у предложения нет, да и человек узнаёт свой день именно по названию.
    Переименованный день читается как «убрал один, добавил другой»: угадывать,
    что «Ноги» стали «Низом», а не заменили их, неоткуда.

    Перестановки внутри дня намеренно игнорируются: порядок упражнений правка
    меняет постоянно, и строчка про это утопила бы то, ради чего блок и нужен —
    что реально пришло, ушло и с какой схемой.
    """
    old_by = {_day_key(d["name"]): d for d in old_days}
    new_by = {_day_key(d["name"]): d for d in new_days}

    lines: list[str] = []
    for day in new_days:
        if _day_key(day["name"]) not in old_by:
            lines.append(i18n.t("program.new_day", name=escape(day["name"])))
    for day in old_days:
        if _day_key(day["name"]) not in new_by:
            lines.append(i18n.t("program.removed_day", name=escape(day["name"])))

    for day in new_days:
        old_day = old_by.get(_day_key(day["name"]))
        if old_day is None:
            continue
        old_items = {item["name"].strip().lower(): item for item in old_day["items"]}
        new_items = {item["name"].strip().lower(): item for item in day["items"]}
        changes: list[str] = []
        for item in day["items"]:
            was = old_items.get(item["name"].strip().lower())
            target = item.get("target")
            if was is None:
                suffix = f" — {escape(target)}" if target else ""
                changes.append(f"  ➕ {escape(item['name'])}{suffix}")
                continue
            if (was.get("target") or "") != (target or ""):
                no_scheme = i18n.t("program.no_scheme")
                changes.append(
                    f"  ✏️ {escape(item['name'])}: "
                    f"{escape(was.get('target') or no_scheme)} → {escape(target or no_scheme)}"
                )
            # Правило прогрессии — такая же часть упражнения, как схема, и
            # переписанное молча оно тем неприятнее, что именно по нему потом
            # считается подсказка веса.
            was_rule = format_progression_rule(was.get("progression"), unit)
            now_rule = format_progression_rule(item.get("progression"), unit)
            if was_rule != now_rule:
                no_progression = i18n.t("program.no_progression")
                changes.append(
                    f"  ⤴️ {escape(item['name'])}: "
                    f"{escape(was_rule or no_progression)} → {escape(now_rule or no_progression)}"
                )
        for item in old_day["items"]:
            if item["name"].strip().lower() not in new_items:
                changes.append(f"  ➖ {escape(item['name'])}")
        if changes:
            lines.append(f"<b>{escape(day['name'])}</b>")
            lines += changes
    return lines


# Запас, который блок «что меняется» обязан оставить после себя для состава
# ниже (см. build_ai_program_preview, _truncate_block): состав обрезается уже
# ПОСЛЕ изменений и делит с ними один и тот же общий лимит Telegram — если бы
# изменения расходовали его весь без остатка, составу не хватило бы места даже
# на собственную строку «…и ещё N упражнений», и итог всё равно перевалил бы
# за лимит. С запасом в разы больше самой длинной такой строки — с учётом
# 3-значных чисел и русской словоформы — этого не случится.
_COMPOSITION_NOTE_RESERVE = 140

# Потолок одной строки блока «На заметку» (см. build_ai_program_preview).
# Строку блок режет целиком, а не по буквам, так что без этого один пункт
# «"День 3": не нашёл в боте — ...» с дюжиной выдуманных моделью названий по
# паре сотен символов съедал бы весь блок, и остальные заметки схлопывались
# бы в «…и ещё N» вместо того, чтобы поместиться рядом.
_NOTE_LIMIT = 180


def _truncate_block(
    candidate_lines: list[str],
    already: list[str],
    tail: list[str],
    is_item: Callable[[str], bool],
    note: Callable[[int], str],
    reserve: int = 0,
) -> list[str]:
    """Добавить candidate_lines к already построчно, пока итог (already + kept
    + tail) укладывается в MESSAGE_LIMIT - reserve; остаток сворачивается в
    одну строку от note().

    Общий механизм для обоих переменных по размеру блоков превью программы
    (см. build_ai_program_preview) — блока «что меняется» и состава: раньше
    обрезался только состав, а блок изменений на большой правке (много дней,
    имена не менялись — см. build_program_changes) сам по себе перевешивал
    лимит, и тогда не отправлялось вообще ничего.

    `reserve` — сколько символов заведомо оставить свободными после этого
    блока для того, что будет обрезано следующим (см.
    _COMPOSITION_NOTE_RESERVE): без этого первый же обрезанный блок мог занять
    буквально весь лимит под свою note(), не оставив второму места даже на
    собственную «…и ещё N» — раньше оба блока резервировали один и тот же
    _TRUNCATION_RESERVE независимо друг от друга, и на двух одновременно
    обрезанных блоках итог всё равно перевешивал MESSAGE_LIMIT.

    Проверка на каждом шаге — это полная длина сообщения, а не абстрактный
    «бюджет в символах»: после того, как основной цикл остановился, note ещё
    раз проверяется на месте и, если впритык не влезла, забирает место у уже
    добавленных строк, а не сообщения целиком.
    """
    budget = MESSAGE_LIMIT - reserve

    def fits(extra: list[str]) -> bool:
        return telegram_length("\n".join(already + extra + tail)) <= budget

    kept: list[str] = []
    cut_at = len(candidate_lines)
    for index, line in enumerate(candidate_lines):
        if not fits(kept + [line]):
            cut_at = index
            break
        kept.append(line)
    else:
        return kept  # все строки уместились — резать было нечего

    hidden = sum(1 for rest in candidate_lines[cut_at:] if is_item(rest))
    if not hidden:
        return kept

    note_lines = ["", note(hidden)]
    while kept and not fits(kept + note_lines):
        kept.pop()
        hidden = sum(1 for rest in candidate_lines[len(kept):] if is_item(rest))
        note_lines = ["", note(hidden)]
    return kept + note_lines


def _editable_later() -> str:
    """Программа не приговор: состав, порядок и схемы правятся руками в любой
    момент. Без этой строчки человек считал предложение тренера единственным
    вариантом и либо соглашался целиком, либо отказывался — хотя поменять
    одно упражнение дешевле, чем пересобирать всё заново.

    Функция, а не модульная константа: значение обязано читать текущий язык
    при каждом вызове, а не застывать один раз при импорте.
    """
    return i18n.t("program.editable_later")


def build_ai_program_preview(
    name: str, days: list[dict], replaces: Optional[dict] = None, notes: Optional[list[str]] = None,
    unit: Optional[str] = None,
) -> str:
    """Превью программы, которую собрал AI-тренер, до её сохранения.

    `days` — черновик из ai_trainer.propose_program: [{"name", "items": [{"name",
    "target", "source", "progression"?}]}]. Дни складываются в одну программу с
    общим именем (routine_exercises.program_name — см.
    db.create_routine_from_program), в списке пользователя это одна строка с
    числом дней, а не несколько.

    `replaces` — сохранённая программа, которую эта правка заменит: {"name",
    "days"} со старым составом. Замена стирает старые дни, поэтому превью и
    называется правкой, и показывает разницу со старой версией: без этого
    человеку пришлось бы сличать два списка по десятку упражнений глазами,
    чтобы понять, что тренер вообще поменял.

    `notes` — 5.3: человеческие фразы о том, что молча срезалось или не
    нашлось (дней/упражнений было больше лимита, имя обрезано, упражнение не
    из каталога) — раньше это уходило только модели полем в JSON, и если она
    забывала упомянуть об этом в ответе, пользователь просто не узнавал, что
    попросил семь дней, а получил шесть.

    `unit` — "kg"/"lb" пользователя для строк прогрессии («прибавь 2.5кг»,
    см. format_progression_rule); без него — нейтральное «к весу».
    """
    total = sum(len(day["items"]) for day in days)
    new_names = sorted(
        {item["name"] for day in days for item in day["items"] if item.get("source") == "template"}
    )

    lines = [
        i18n.t("program.header_edit") if replaces else i18n.t("program.header_new"),
        "",
        f"<b>{escape(name)}</b>",
        i18n.t("program.days_exercises", days=len(days), d=len(days), total=total, e=total),
    ]

    # Прогрессия — общий принцип программы, а не свойство каждой строки: одна
    # фраза сверху вместо восьми почти одинаковых под упражнениями.
    shared_progression, shared_kind = progression_summary(days)
    if shared_progression:
        lines.append(escape(shared_progression))

    composition: list[str] = []
    for day in days:
        composition += ["", f"<b>{escape(day['name'])}</b>"]
        for i, item in enumerate(day["items"], start=1):
            target = item.get("target")
            suffix = f" — {escape(target)}" if target else ""
            composition.append(f"{i}. {escape(item['name'])}{suffix}")
            kind = (item.get("progression") or {}).get("rule")
            if shared_kind is None:
                # Общей фразы нет — правил в программе одно-два, повторов не
                # будет, и точная строка со своим шагом полезнее общих слов.
                prog_note = format_progression_rule(item.get("progression"), unit)
                if prog_note:
                    composition.append(f"   ⤴️ {escape(prog_note)}")
            elif kind != shared_kind and kind in _PROGRESSION_KIND_KEYS:
                # Исключение из общей фразы — тип правила, без шага: шаг человек
                # увидит в тренировке, а здесь он превращает состав в простыню.
                composition.append(f"   ⤴️ {escape(i18n.t(_PROGRESSION_KIND_KEYS[kind]))}")

    tail = ["", DIVIDER]
    if replaces:
        tail.append(i18n.t("program.replaces_tail", name=escape(replaces["name"])))
    elif len(days) == 1:
        tail.append(i18n.t("program.add_single_tail"))
        tail.append(_editable_later())
    else:
        tail.append(i18n.t("program.add_multi_tail", name=escape(name), days=len(days), d=len(days)))
        tail.append(_editable_later())
    if new_names:
        # Числительное здесь фиксированное «Новых для тебя» — ключ каталога
        # сам решает согласование формы по числу (см. program.new_exercises_for_you).
        tail.append(i18n.t("program.new_exercises_for_you", n=len(new_names)))

    # 5.3: клампы и потери, которые раньше уходили только модели JSON-полями
    # (truncated_days/truncated_exercises/unresolved/name_truncated в
    # ai_trainer._propose_program) — здесь тем же текстом, что видит модель, но
    # адресовано пользователю напрямую: он не должен зависеть от того, упомянет
    # ли модель в ответе, что просил семь дней, а получил шесть.
    #
    # Режется наравне с остальными блоками переменного размера. Раньше блок
    # приклеивался к шапке целиком и в обрезке не участвовал вовсе, хотя растёт
    # он от того, что нафантазировала модель: шесть дней по дюжине ненайденных
    # упражнений с длинными выдуманными названиями давали под шесть тысяч
    # символов только заметками — превью переваливало за лимит Telegram, и тап
    # по кнопке «📋 Программа» не показывал вообще ничего (сообщение не
    # отправлялось, а ai_program_view его отправку не страхует).
    if notes:
        notes_header = ["", i18n.t("program.notes_header")]
        lines += notes_header + _truncate_block(
            [f"• {escape(shorten(n, _NOTE_LIMIT))}" for n in notes],
            already=lines + notes_header,
            tail=tail,
            is_item=lambda line: line.startswith("•"),
            note=lambda n: i18n.t("program.notes_hidden", n=n),
            # Заметки режутся первыми из трёх блоков, поэтому оставляют место
            # под «…и ещё N» обоих следующих: состава и, если это правка,
            # блока изменений. Иначе на предложении, где обрезаны все три,
            # их note() конкурировали бы за один и тот же хвост лимита.
            reserve=_COMPOSITION_NOTE_RESERVE * (2 if replaces else 1),
        )

    # Что меняется — выше состава: решение принимают по разнице, а полный
    # список ниже нужен уже для «ок, и как это теперь выглядит целиком».
    # Блок сам по себе переменного размера (шесть дней с сохранёнными именами
    # и полностью новым составом дают под четверть сотни строк на день — см.
    # test_preview_of_an_edit_that_keeps_day_names_still_fits) и режется по
    # тому же принципу, что и состав ниже.
    changes_block: list[str] = []
    if replaces:
        changes_header = ["", i18n.t("program.changes_header")]
        changes_body = build_program_changes(replaces.get("days") or [], days, unit) or [
            i18n.t("program.changes_same")
        ]
        kept_changes = _truncate_block(
            changes_body,
            already=lines + changes_header,
            tail=tail,
            is_item=lambda line: any(sym in line for sym in ("➕", "➖", "✏️")),
            note=lambda n: i18n.t("program.changes_hidden", n=n),
            # Оставляем составу ниже гарантированное место хотя бы на
            # собственную «…и ещё N упражнений» (см. _COMPOSITION_NOTE_RESERVE) —
            # иначе на программе, где обрезаны оба блока разом, их note()
            # конкурировали бы за один и тот же хвост лимита.
            reserve=_COMPOSITION_NOTE_RESERVE,
        )
        changes_block = changes_header + kept_changes
    lines += changes_block

    # Шесть дней по двенадцать упражнений (потолки propose_program) сами по
    # себе переваливают за лимит Telegram — и тогда не отправится вообще
    # ничего. Состав ниже всего по важности (итог всё равно окажется в «🗂
    # Программы»), поэтому от него отрезается то, что не влезло уже после
    # шапки, блока изменений (если он был) и хвоста.
    kept_composition = _truncate_block(
        composition,
        already=lines,
        tail=tail,
        is_item=lambda line: bool(line) and line[0].isdigit(),
        note=lambda n: i18n.t("program.composition_hidden", n=n),
    )
    return "\n".join(lines + kept_composition + tail)


# Fun, shareable size comparisons for a tonnage total — (emoji, kg each, catalog
# object id), light→heavy. Каждый id — суффикс ключа tonnage.object.<id>
# (locales/*.json), где живёт ICU-плюрализация названия на обоих языках —
# английское слово тут не калька русского ("гружёная «Газель»" стала общим
# "loaded delivery van", ровно как задумано TONE_OF_VOICE.md).
_TONNAGE_OBJECTS = [
    ("🐺", 80, "saint_bernard"),
    ("🏍", 200, "motorcycle"),
    ("🐻", 350, "bear"),
    ("🎹", 480, "piano"),
    ("🐴", 550, "horse"),
    ("🐮", 750, "cow"),
    ("🚗", 1400, "car"),
    ("🚚", 3500, "van"),
    ("🐘", 5000, "elephant"),
    ("🦈", 5500, "orca"),
    ("🚌", 12000, "bus"),
]


def format_tonnage_equivalent(total: float, seed: int = 0, unit: str = "kg") -> str | None:
    """A playful "это как N слонов 🐘" comparison clause, without restating the tonnage
    itself — callers fold it into whatever sentence already states the total.

    Picks whichever object gives a believable count (2..40); `seed` (e.g. the
    workout id) rotates the choice so it isn't always the same object. Returns
    None for a tonnage too small to compare (bodyweight-only or very light days).

    `total` comes in the user's own unit and is converted first: the objects
    below weigh what they weigh, so counting pounds against them inflated every
    comparison by 2.2× for lb users.
    """
    total_kg = to_kg(total, unit)
    if total_kg < 150:
        return None
    candidates = [
        (emoji, obj_id, round(total_kg / w))
        for emoji, w, obj_id in _TONNAGE_OBJECTS
        if 2 <= round(total_kg / w) <= 40
    ]
    if not candidates:
        # Above the heaviest bracket (or in a gap): fall back to the biggest object that fits.
        fitting = [
            (emoji, obj_id, max(1, round(total_kg / w)))
            for emoji, w, obj_id in _TONNAGE_OBJECTS
            if w <= total_kg
        ]
        if not fitting:
            return None
        candidates = [fitting[-1]]
    emoji, obj_id, count = candidates[seed % len(candidates)]
    noun = i18n.t(f"tonnage.object.{obj_id}", n=count)
    return i18n.t("tonnage.equivalent", count=count, noun=noun, emoji=emoji)


# Подпись группы, под которую бот не смог определить мышцу — читается через
# module __getattr__ выше (formatting.UNGROUPED_LABEL), чтобы отражать
# текущий язык при каждом обращении. Своя строка, а не пропуск: подходы
# сделаны, и прятать их из суммы значило бы показывать неверный итог.

# «Другое» — сборная группа каталога, куда попадает всё, что не легло в шесть
# основных. На сводке она не нужна: у коридора 6–12 нет смысла для мешка, в
# котором может лежать и пресс, и кардио, и одна разминка, а строку и место она
# занимает наравне с грудью. В сумму заголовка тоже не идёт — иначе число не
# сходилось бы с видимыми полосами.
VOLUME_HIDDEN_GROUPS = frozenset({"другое"})


def weekly_volume_panel(
    counts: dict[Optional[int], int], groups: list
) -> tuple[str, list[tuple[str, int, str]]]:
    """(заголовок, строки) для панели недельного объёма на главном экране.

    `counts` — что вернул db.weekly_volume_by_group (ключ — id группы мышц, None
    для упражнений без группы), `groups` — список групп пользователя. Строка:
    (название капсом, подходов, статус из classify_weekly_volume) — то, что
    charts._draw_volume_panel рисует напрямую.

    Группы с нулём остаются в списке: «спину я на этой неделе не трогал» — это
    главное, что панель вообще способна сообщить, и выкинуть такую строку значит
    выкинуть ответ. А вот когда ноль везде, панели нет совсем — вызывающая
    сторона видит пустой список строк.

    Порядок — по убыванию подходов: перебор собирается сверху, провалы внизу, и
    длина полос идёт монотонно, так что дыру видно по силуэту списка, а не
    вычитанием чисел. Ничьи разводятся по названию, иначе картинка бы
    перетасовывалась между открытиями меню на одних и тех же данных.
    """
    rows: list[tuple[str, int, str]] = []
    for group in groups:
        if group["name"].strip().lower() in VOLUME_HIDDEN_GROUPS:
            continue
        sets = counts.get(group["id"], 0)
        rows.append((format_group(group["name"]), sets, classify_weekly_volume(sets)))

    ungrouped = counts.get(None, 0)
    if ungrouped:
        rows.append((i18n.t("dashboard.ungrouped_label"), ungrouped, classify_weekly_volume(ungrouped)))

    total = sum(sets for _, sets, _ in rows)
    if total == 0:
        return "", []

    rows.sort(key=lambda row: (-row[1], row[0]))
    title = i18n.t("volume.title", window=days_window_label(VOLUME_WINDOW_DAYS), total=total, n=total)
    return title, rows


# Подпись блока плиток роста — у него нет своего числа в заголовке, и без
# подписи «101 кг» под именем упражнения читалось бы как поднятый вес, хотя
# это расчётный максимум.
#
# Число недель — параметр, а не часть константы: у истории моложе 8 недель
# окно роста короче (см. handlers.workout._LIFT_WINDOW_WEEKS), и заголовок
# обязан называть то самое окно, которым реально считали, а не соврать
# «8 недель» там, где смотрели только 4.
def menu_lifts_title(weeks: int) -> str:
    return i18n.t("dashboard.lifts_title", weeks=weeks, n=weeks)


# MENU_LIFTS_NOTE — читается через module __getattr__ выше
# (formatting.MENU_LIFTS_NOTE), чтобы отражать текущий язык при каждом
# обращении, а не застывать один раз при импорте модуля.

# Сколько плиток роста рисуется независимо от того, сколько упражнений выросло:
# 2×3 в раскладке _dash_growth_tiles. Больше шести на узкой картинке уже не
# читаются, а меньше — на широких плитках остаётся пусто.
_LIFT_TILE_COUNT = 6


def days_window_label(days: int) -> str:
    """«ЗА 7 ДНЕЙ» — единственная форма записи окна в сводке.

    До этого в одной картинке жили три: «ЗА 30 ДНЕЙ», «ТОННАЖ 7 Д» и «ОБЪЁМ ЗА 7
    ДНЕЙ». Одна и та же мысль, записанная тремя способами, заставляет читателя
    каждый раз заново решать, что перед ним, — а решать тут нечего.
    """
    return i18n.t("dashboard.days_window", days=days, n=days)


def menu_headline(dashboard) -> str:
    """Одна крупная строка вверху сводки.

    Серия — если она есть: это единственное число, которое человек хочет не
    потерять, и потому лучше всех работает заголовком. Без серии заголовок
    говорит про месяц, а не показывает «0 недель подряд» — считать нулевую серию
    достижением незачем.
    """
    if dashboard.week_streak >= 2:
        return i18n.t("dashboard.headline_streak", weeks=dashboard.week_streak, n=dashboard.week_streak)
    return i18n.t("dashboard.headline_month", n=dashboard.last_30_days)


def menu_tiles(dashboard, tonnage: float, records: int, unit: str = "kg") -> list[tuple[str, str]]:
    """Три плитки под заголовком: месяц, работа за неделю и рекорды.

    Рекордов может не быть, и тогда плитка отдаёт место текущей неделе:
    «РЕКОРДОВ 0» — это не факт, а укор, причём за неделю, в которую человек мог
    просто работать в подходах.

    У тоннажа и рекордов окно названо в подписи. Без него «11» рядом с «35.1 т»
    выглядит как счётчик за всё время, и цифра врёт в разы.
    """
    u = unit_label(unit)
    # Тонны — когда их есть чем мерить: «0.4 т» читается хуже, чем «400 кг».
    #
    # Тонна — тонна, поэтому и порог, и сама цифра считаются в килограммах, ровно
    # как в format_tonnage: тоннаж лежит в единицах пользователя, и делить на
    # 1000 фунты значило показать «24.5 т» там, где на всех остальных экранах
    # (зал славы, недельная сводка) у того же человека 11.1 тонны. Ниже тонны
    # конвертировать нечего — там его собственное число в его же единицах.
    total_kg = to_kg(tonnage, unit)
    tonnes = f"{total_kg / 1000:.1f}"
    weight = f"{tonnes} {i18n.t('unit.ton_short')}" if total_kg >= 1000 else f"{tonnage:.0f} {u}"
    tiles = [
        (i18n.t("dashboard.tile_workouts", window=days_window_label(30)), str(dashboard.last_30_days)),
        (i18n.t("dashboard.tile_tonnage", window=days_window_label(VOLUME_WINDOW_DAYS)), weight),
    ]
    if records > 0:
        tiles.append((
            i18n.t("dashboard.tile_records", window=days_window_label(VOLUME_WINDOW_DAYS)),
            str(records),
        ))
    elif dashboard.this_week != dashboard.last_30_days:
        tiles.append((i18n.t("dashboard.tile_workouts_week"), str(dashboard.this_week)))
    # Иначе плитки нет вовсе: у человека, который только начал, вся история
    # лежит внутри этой недели, и «ЗА НЕДЕЛЮ 1» рядом с «ЗА 30 ДНЕЙ 1» — это
    # одно и то же число, поставленное дважды. Две плитки на полосе честнее трёх,
    # из которых одна дублирует соседнюю.
    return tiles


def menu_lift_tiles(
    growth: list[tuple[str, float, float]], unit: str = "kg"
) -> list[tuple[str, str, str]]:
    """(имя, «+NN%», «227кг vs 220кг») — плитки роста e1RM.

    `growth` — тройки (имя, e1RM до окна, e1RM внутри окна) от
    db.exercise_e1rm_growth. Упражнения без роста (нет базы до окна, или
    результат внутри окна её не превысил) в сводку не попадают: плитка «рост
    0%» ничего не сообщает и просто занимает место, которое мог бы занять
    настоящий прогресс по другому движению.

    Сортировка — по проценту роста: самый заметный прогресс всегда в первой
    плитке.

    Имя не обрезается: это выбор, а не карточки со спарклайном, где ширина
    буквально занята линией. Полное название важнее аккуратной колонки.
    """
    u = unit_label(unit)
    rows = [
        (name, before, window)
        for name, before, window in growth
        if before > 0 and window > before
    ]
    rows.sort(key=lambda r: (r[2] - r[1]) / r[1], reverse=True)
    tiles: list[tuple[str, str, str]] = []
    for name, before, window in rows[:_LIFT_TILE_COUNT]:
        pct = (window - before) / before * 100
        abs_str = f"{window:.0f}{u} vs {before:.0f}{u}"
        tiles.append((name.upper(), f"+{pct:.0f}%", abs_str))
    return tiles


def build_workout_card(
    started_at: dt.datetime,
    blocks: list[BlockView],
    note: str | None = None,
    unit: str = "kg",
) -> tuple[str, list[str], str, str | None]:
    """Plain-text (no HTML) breakdown of a workout, for rendering to a shareable image.

    Returns (title, body_lines, footer, note) — charts.render_workout_card draws them.
    """
    u = unit_label(unit)
    title = format_date_ru(started_at)
    body: list[str] = []
    exercise_count = 0
    set_count = 0
    tonnage = 0.0

    for block in blocks:
        body.append(f"{block.exercise_name} [{format_group_tag(block.group_name)}]")
        if block.sets:
            # Same "190×5 ×3" collapsing the text card uses — a straight run of
            # work sets otherwise spells every one of them out, which on the image
            # is what pushes the line past the card's width.
            formatted = [format_set(w, r, block.rpe_for(i)) for i, (w, r) in enumerate(block.sets)]
            body.append("  " + ", ".join(formatted))
        else:
            body.append(f"  {i18n.t('card.no_sets_dash')}")
        # Рекорд едет и на картинку: её уносят в чат с друзьями, и «на 5.7кг
        # выше прошлого» — ровно то, ради чего её уносят. ★ вместо 🔥
        # — картинку рисует matplotlib, эмодзи там выходят пустыми квадратами
        # (см. charts.render_workout_card), а звёздочка есть в шрифте.
        record = _block_record_text(block, unit)
        if record:
            body.append(f"  ★ {record}")
        exercise_count += 1
        set_count += len(block.sets)
        tonnage += block.tonnage

    # TONE_OF_VOICE.md запрещает «сет» в прозе («суперсет» — ок) — словарь
    # хочет «подход» (по-английски же естественное слово для этого — именно
    # «set», запрет на кальку не переносится, см. TONE_OF_VOICE.md English
    # voice). Эта строка уходит и в текстовую карточку, и в PNG-картинку
    # шеринга (charts.render_workout_card), так что видит её каждый
    # пользователь на каждой законченной тренировке.
    footer = i18n.t(
        "card.image_footer",
        ex=exercise_count, e=exercise_count,
        sets=set_count, s=set_count,
        tonnage=format_weight(tonnage), u=u,
    )
    return title, body, footer, note


def workout_pick_block(index: int, date: str, exercises: list[tuple[str, str]]) -> str:
    """Одна тренировка в списке выбора: жирный «номер · дата», под ним все
    упражнения по строке на каждое, группа мышц в скобках — та же запись
    «Название [ГРУППА]», что в живом трекере (см. _render_single_block).

    Номер — потому что в подпись кнопки имена упражнений не влезают: на кнопке
    остаётся «1 - 3 августа», а что это за тренировка, человек читает в тексте
    над кнопками. Через запятую тот же список читался стеной даже усечённым, а
    по строке на упражнение он длиннее, зато сканируется — и ничего не
    приходится обрезать.

    Одна и та же разметка на двух экранах выбора — «повторить тренировку» и
    «создать программу из тренировки»: выбор там один и тот же, и выглядеть он
    должен одинаково.
    """
    header = f"<b>{index} · {date}</b>"
    if not exercises:
        return f"{header}\n{i18n.t('history.no_exercises')}"
    bullets = "\n".join(
        f"• {escape(name)} [{escape(format_group_tag(group))}]" if group else f"• {escape(name)}"
        for name, group in exercises
    )
    return f"{header}\n{bullets}"


def build_workout_preview(
    started_at: dt.datetime, blocks: list[BlockView], note: str | None = None,
    duration_seconds: float | None = None,
) -> str:
    """Compact preview of a past workout before repeating it: one line per
    exercise plus a single comma-joined line of its sets — the same terse style
    the live tracker uses for already-finished exercises, no e1RM/group tag/prev-set
    clutter, since this is about scanning what was done, not analysing it."""
    header = f"<b>{format_date_ru(started_at)}</b>"
    if duration_seconds is not None:
        header += f" · {format_duration(duration_seconds)}"
    lines = [header]
    if note:
        lines.append(f"📝 {escape(note)}")
    lines.append("")
    for block in blocks:
        lines.append(f"<b>{escape(block.exercise_name)}</b>")
        if block.sets:
            lines.append(", ".join(format_set(w, r, block.rpe_for(i)) for i, (w, r) in enumerate(block.sets)))
        else:
            lines.append(f"<i>{i18n.t('card.no_sets')}</i>")
    return "\n".join(lines)


def build_live_session_text(
    blocks: list[BlockView],
    hint: str | None = None,
    active_exercise_id: int | None = None,
    note: str | None = None,
) -> str:
    body_lines = []
    for i, block in enumerate(blocks):
        if i > 0:
            body_lines.append("")
        is_active = active_exercise_id is not None and block.exercise_id == active_exercise_id
        prefix = "▶ " if is_active else ""
        body_lines.append(f"{prefix}<b>{escape(block.exercise_name)}</b>")
        # 🥇 — новый лучший сет за всю историю упражнения. Ставится сразу, даже
        # если тренировка в целом слабая: ран провален — голд твой.
        gold = getattr(block, "gold_index", None)

        def set_str(i: int, w: float, r: int, *, blk=block, gold=gold) -> str:
            marked = format_set(w, r, blk.rpe_for(i))
            return f"{marked} 🥇" if i == gold else marked

        if is_active:
            body_lines.extend(f"  • {set_str(i, w, r)}" for i, (w, r) in enumerate(block.sets))
            if note:
                body_lines.append(f"📝 <i>{escape(note)}</i>")
        elif block.sets:
            body_lines.append(", ".join(set_str(i, w, r) for i, (w, r) in enumerate(block.sets)))
    lines = list(body_lines)
    if not lines and not hint:
        lines = [i18n.t("card.add_exercise_to_start")]
    if hint:
        if lines:
            lines.append(DIVIDER if body_lines else "")
        lines.append(hint)
    return "\n".join(lines)


def build_gold_book_lines(golds, unit: str = "kg", is_bodyweight: bool = False) -> list[str]:
    """"🥇 Золотая книга" — лучшие сеты упражнения за всё время, каждый с датой.

    Две категории — самый тяжёлый сет и сет с лучшим e1RM: пики у них разные
    дни. Рекорд повторов из книги убран вместе с остальными упоминаниями
    рекордов по повторам — остался он только у упражнений своим весом, где
    e1RM тождественно нулю и мерить больше нечем. Дубли схлопываются: если
    тяжёлый сет он же и лучший по e1RM, строка одна.
    """
    # Пустая книга — по числу повторов, а не по e1RM: у упражнений своим весом
    # e1RM тождественно нулю, и проверка по нему прятала книгу целиком.
    if golds is None or golds.max_reps <= 0:
        return []
    u = unit_label(unit)
    header = i18n.t("gold.header")

    def dated(label: str, value: str, day: str) -> str:
        when = f" · {_iso_to_ru(day)}" if day else ""
        return f"   {label} {value}{when}"

    if is_bodyweight:
        # Вес всегда 0 — «самый тяжёлый» и e1RM смысла не имеют, остаются повторы.
        return [header,
                dated(i18n.t("gold.reps_label"), str(golds.max_reps), golds.max_reps_date)]

    rows = [("e1RM", f"{format_weight(golds.best_e1rm)}{u} ({format_set(golds.best_e1rm_weight, golds.best_e1rm_reps)})",
             golds.best_e1rm_date)]
    weight_set = (golds.max_weight, golds.max_weight_reps)
    if weight_set != (golds.best_e1rm_weight, golds.best_e1rm_reps):
        rows.append((i18n.t("gold.weight_label"), format_set(*weight_set), golds.max_weight_date))
    return [header] + [dated(label, value, day) for label, value, day in rows]


def _iso_to_ru(day: str) -> str:
    """'2026-08-02' → '2 августа'; пустая строка, если дата не разбирается."""
    try:
        return format_day_month_ru(dt.date.fromisoformat(day))
    except ValueError:
        return ""


def format_new_achievements(new_codes: list[str]) -> str | None:
    """Celebratory block for badges earned right now, shown on the completion card."""
    import achievements

    earned = [achievements.BY_CODE[c] for c in new_codes if c in achievements.BY_CODE]
    if not earned:
        return None
    header = i18n.t("card.new_achievement_one") if len(earned) == 1 else i18n.t("card.new_achievements_many")
    lines = [header] + [f"{a.emoji} <b>{escape(a.title)}</b> — {escape(a.description)}" for a in earned]
    return "\n".join(lines)


def build_achievements_screen(earned: set[str]) -> str:
    """The full 🏅 badge grid: everything unlocked, then everything still locked.

    Both halves always fold on this screen specifically, short lists included:
    it's reached from Progress purely to check a number or brag about a single
    badge, so the header count (11/23) is the answer most taps are actually
    after — the two lists are supporting detail, not the point of the tap.
    """
    import achievements

    got = [a for a in achievements.CATALOG if a.code in earned]
    locked = [a for a in achievements.CATALOG if a.code not in earned]
    lines = [i18n.t("achievements.screen_header", got=len(got), total=len(achievements.CATALOG))]
    if got:
        lines.append(
            collapsible(
                "\n".join(f"{a.emoji} <b>{escape(a.title)}</b> — {escape(a.description)}" for a in got)
            )
        )
    if locked:
        lines.append("")
        lines.append(i18n.t("achievements.locked_header", n=len(locked)))
        lines.append(
            collapsible(
                "\n".join(f"🔒 {escape(a.title)} — {escape(a.description)}" for a in locked)
            )
        )
    return "\n".join(lines)


def format_duration_hm(seconds: float) -> str:
    """Compact h/m for the Hall of Fame longest-workout line."""
    minutes = int(seconds // 60)
    h, m = divmod(minutes, 60)
    if h:
        return i18n.t("date.duration_h_m", h=h, m=m) if m else i18n.t("date.duration_h", h=h)
    return i18n.t("date.duration_m", m=m)


def _hall_of_fame_lift(name: str, weight: float, reps: int, e1rm_value: float, unit_label: str) -> str:
    """One personal-record line. Bodyweight moves have no load to report, so their
    record is the best set of reps instead of a weight and an e1RM."""
    if weight > 0:
        return f"• {escape(name)} — {format_set(weight, reps)} · e1RM {e1rm_value:.0f}{unit_label}"
    return i18n.t("hall.reps_line", name=escape(name), reps=reps, n=reps)


@dataclass
class WeeklyRow:
    """Одна строка недельной сводки — упражнение за неделю."""
    name: str
    top_weight: float
    tonnage: float
    sets_count: int


# Сколько упражнений показываем в недельной сводке — дальше таблица перестаёт
# читаться с телефона, а хвост из одного подхода ничего не добавляет.
#
# Обрезается ровно вывод и ничего кроме: раньше список резался до вызова
# сводки, и итог недели считался по остатку — у человека с 20 упражнениями
# недельный тоннаж выходил заниженным и не сходился с плиткой на дашборде,
# которая считает по всем подходам. Итог приходит отдельным числом (см.
# total_tonnage), строки — все, что были за неделю.
WEEKLY_ROWS_LIMIT = 12


def _weekly_shown(rows: list[WeeklyRow]) -> list[WeeklyRow]:
    return rows[:WEEKLY_ROWS_LIMIT]


def build_weekly_summary(
    rows: list[WeeklyRow],
    workouts: int,
    total_tonnage: float,
    period: str,
    unit: str = "kg",
    food_line: str | None = None,
) -> str:
    """Недельная сводка обычным текстом — фолбэк для клиентов без rich-таблиц
    и источник тех же чисел для табличной версии (см. build_weekly_table).

    `rows` — все упражнения недели; до читаемого числа строк список режется
    здесь, а `total_tonnage` считается вызывающей стороной по всем подходам.
    """
    u = unit_label(unit)
    lines = [
        i18n.t("weekly.header", period=escape(period)),
        i18n.t("weekly.summary_line", n=workouts, tonnage=format_tonnage(total_tonnage, unit)),
        "",
    ]
    if not rows:
        lines.append(i18n.t("weekly.empty"))
        return "\n".join(lines)
    for row in _weekly_shown(rows):
        lines.append(
            i18n.t(
                "weekly.row",
                name=escape(row.name),
                weight=format_weight(row.top_weight),
                u=u,
                sets=row.sets_count,
                n=row.sets_count,
                tonnage=format_tonnage(row.tonnage, unit),
            )
        )
    if food_line:
        lines += ["", food_line]
    return "\n".join(lines)


def build_weekly_table(rows: list[WeeklyRow], unit: str = "kg"):
    """Те же строки настоящей таблицей (rich messages, Bot API 10.1).

    Возвращает InputRichBlockTable или None, если строк нет. Вызывающая сторона
    обязана уметь и без него: на сервере/клиенте ниже 10.1 отправка упадёт, и
    сводка уходит текстом (см. build_weekly_summary).
    """
    from aiogram.types import InputRichBlockTable, RichBlockTableCell

    if not rows:
        return None
    u = unit_label(unit)
    def cell(text: str, align: str = "left", is_header: bool = False) -> RichBlockTableCell:
        # align/valign у ячейки обязательны — без них pydantic-модель aiogram
        # не собирается вовсе.
        return RichBlockTableCell(text=text, align=align, valign="middle", is_header=is_header)

    header = [
        cell(i18n.t("weekly.table_header_exercise"), is_header=True),
        cell(i18n.t("weekly.table_header_best"), "right", is_header=True),
        cell(i18n.t("weekly.table_header_sets"), "right", is_header=True),
        cell(i18n.t("weekly.table_header_tonnage"), "right", is_header=True),
    ]
    body = [
        [
            cell(row.name),
            cell(f"{format_weight(row.top_weight)}{u}", "right"),
            cell(str(row.sets_count), "right"),
            cell(format_tonnage(row.tonnage, unit), "right"),
        ]
        for row in _weekly_shown(rows)
    ]
    return InputRichBlockTable(cells=[header, *body], is_striped=True, is_bordered=True)


def format_rank_gap(gap) -> str:  # analytics.RankGap
    """Недостача до следующего звания словами тренера, а не сокращениями системы.

    «ещё 12 тренировок», а не «ещё 12 трен.»: то же самое расстояние пуш уже
    называет полным словом (engagement._workouts_phrase), и разнобой между
    экраном и пушем читается как два разных голоса.
    """
    if gap.axis == "workouts":
        n = int(gap.value)
        return i18n.t("rank.gap_workouts", n=n)
    if gap.axis == "tonnage":
        tons = gap.value / 1000
        # Меньше сотни килограммов — «0.0 т» выглядело бы как «уже всё»,
        # поэтому остаток ниже центнера договариваем килограммами.
        # Округление как у порогов рядом («200 т», не «200.0 т»): лишний ноль в
        # строке-подсказке выглядит другой единицей измерения, а не той же осью.
        if gap.value >= 100:
            return i18n.t("rank.gap_tonnage_tons", tons=f"{round(tons, 1):g}")
        return i18n.t("rank.gap_tonnage_kg", kg=f"{gap.value:.0f}")
    # Дробная частота («1.5 раза в неделю») берёт ту же форму, что и «2»
    # («тренировки»), — по-русски дробные числа всегда идут с этой формой
    # независимо от целой части (см. ту же уловку в format_tonnage).
    per_week = gap.value
    plural_n = int(per_week) if per_week == int(per_week) else 2
    return i18n.t("rank.gap_frequency", per_week=f"{per_week:g}", n=plural_n)


def format_rank_line(rank, gap=None) -> str:  # gap: analytics.RankGap | None
    """«⚙️ Станок» плюс, если есть куда расти, чего не хватает до следующего."""
    line = i18n.t("rank.line", emoji=rank.emoji, name=escape(rank.name))
    return i18n.t("rank.line_with_gap", line=line, gap=format_rank_gap(gap)) if gap else line


def format_rank_promotion(rank) -> str:
    """Строка повышения на карточке завершения — объявляется один раз."""
    return i18n.t("rank.promotion", emoji=rank.emoji, name=escape(rank.name))


def build_rank_ladder(
    ranks: list,
    current,
    gap=None,  # analytics.RankGap | None
    total_workouts: int | None = None,
    tonnage_kg: float | None = None,
    per_week: float | None = None,
) -> str:
    """Вся лестница званий с порогами и отметкой, где человек сейчас.

    До этого звание нигде не объяснялось: на сводке висела плашка «РАБОТЯГА» без
    контекста, а в зале славы — строка «до следующего: ещё 12 трен.». Из этого
    видно, что есть какая-то система, и не видно ни какая, ни докуда она идёт.
    Непонятная система мотивирует хуже отсутствующей: человек не знает, к чему
    приложить усилие, и перестаёт считать её своей.

    Правило про слабейшую ось названо прямо. Без него лестница читается как
    «набери любое из трёх», и человек с большим тоннажем и месяцем простоя
    считает, что бот ошибся, — хотя это ровно то поведение, которое задумано.

    Свои три числа (`total_workouts`, `tonnage_kg`, `per_week`) — рядом с
    порогами: без них видно, куда идти, и не видно, откуда. «Ещё 12 тренировок»
    у следующей ступени отвечает только за одну ось, а человек в этот момент
    хочет знать, какая из трёх его держит.
    """
    lines = [
        i18n.t("rank.ladder_header"),
        "",
        i18n.t("rank.ladder_explainer", weeks=RANK_FREQUENCY_WEEKS),
        "",
        i18n.t("rank.ladder_break_rule"),
        "",
    ]
    if None not in (total_workouts, tonnage_kg, per_week):
        stats = " · ".join((
            i18n.t("rank.stat_workouts", n=total_workouts),
            i18n.t("rank.stat_tonnage", tons=f"{tonnage_kg / 1000:.1f}"),
            i18n.t("rank.stat_frequency", per_week=f"{per_week:.1f}"),
        ))
        lines.append(i18n.t("rank.current_stats", stats=stats))
        lines.append("")
    for rank in ranks:
        thresholds = i18n.t("rank.threshold_from_start") if rank.level == 0 else " · ".join((
            i18n.t("rank.stat_workouts", n=rank.min_workouts),
            i18n.t("rank.stat_tonnage", tons=f"{rank.min_tonnage_kg / 1000:g}"),
            i18n.t("rank.stat_frequency", per_week=f"{rank.min_per_week:g}"),
        ))
        row = f"{rank.emoji} <b>{escape(rank.name)}</b> — {thresholds}"
        if rank.level == current.level:
            row += i18n.t("rank.you_are_here")
        elif rank.level == current.level + 1 and gap:
            row += f"  ← {format_rank_gap(gap)}"
        lines.append(row)
    return "\n".join(lines)


def build_hall_of_fame(
    total_workouts: int,
    tonnage_kg: float,
    tonnage_equivalent: str | None,
    best_week_streak: int,
    longest_workout_seconds: float,
    top_lifts: list[tuple[str, float, int, float]],  # (name, weight, reps, e1rm); weight 0 = bodyweight
    unit: str = "kg",
    max_chars: int | None = None,
    rank=None,  # analytics.Rank | None
    rank_gap=None,  # analytics.RankGap | None
) -> str:
    """Lifetime totals plus the user's best lifts, shown above the badge grid
    on the '🏅 Достижения' screen — no heading of its own."""
    u = unit_label(unit)
    if total_workouts == 0:
        return i18n.t("hall.empty")

    lines = []
    if rank is not None:
        lines.append(format_rank_line(rank, rank_gap))
    # Без слова после числа: подпись «Всего тренировок» его уже произнесла, и
    # строка читалась как «Всего тренировок: 20 тренировок».
    lines.append(i18n.t("hall.total_workouts", n=total_workouts))

    # `tonnage_kg` arrives in the user's own unit despite the name — a ton is a
    # ton, so convert before comparing against one (see format_tonnage).
    lifetime_kg = to_kg(tonnage_kg, unit)
    if lifetime_kg >= 1000:
        tons = round(lifetime_kg / 1000)
        tonnage_str = i18n.t("tonnage.total", tons=str(tons), n=tons)
    else:
        tonnage_str = f"{tonnage_kg:.0f}{u}"
    tonnage_line = i18n.t("hall.total_tonnage", tonnage=tonnage_str)
    if tonnage_equivalent:
        clause = tonnage_equivalent.rstrip(".")
        clause = clause[:1].lower() + clause[1:]
        tonnage_line += f"  ({clause})"
    lines.append(tonnage_line)

    if best_week_streak >= 2:
        lines.append(i18n.t("hall.best_streak", n=best_week_streak))
    if longest_workout_seconds > 0:
        lines.append(i18n.t("hall.longest_workout", duration=format_duration_hm(longest_workout_seconds)))

    if not top_lifts:
        return "\n".join(lines)

    entries = [_hall_of_fame_lift(name, weight, reps, e1, u) for name, weight, reps, e1 in top_lifts]
    lines.append("")
    lines.append(i18n.t("hall.personal_records"))
    head = "\n".join(lines)

    # The whole list goes into one fold rather than being split into a visible
    # head and a hidden tail: collapsed, the block already shows its first lines,
    # so a manual split only adds a seam in the middle of one list.
    def assemble(keep: list[str]) -> str:
        text = f"{head}\n{collapsible_if_long(chr(10).join(keep))}"
        if len(keep) < len(entries):
            text += f"\n{i18n.t('hall.shown_of', kept=len(keep), total=len(entries))}"
        return text

    kept = entries
    text = assemble(kept)
    while max_chars is not None and len(kept) > 1 and telegram_length(text) > max_chars:
        kept = kept[:-1]
        text = assemble(kept)
    return text


def _progress_session_block(session, is_bodyweight: bool, note: str | None = None) -> str:
    """One dated session on the progress screen: date, its sets, the metric,
    and this session's own note, if it had one — a note only ever applies to
    the specific workout it was written in, not the exercise in general."""
    d = dt.datetime.fromisoformat(session.started_at)
    sets_str = ", ".join(format_set(st.weight, st.reps, st.rpe) for st in session.sets)
    tail = (
        i18n.t("progress.total_reps", n=session.total_reps)
        if is_bodyweight
        else f"e1RM {format_weight(session.top_e1rm)}"
    )
    note_line = f"\n📝 <i>{escape(note)}</i>" if note else ""
    return f"<b>{format_date_ru(d)}</b>\n{sets_str}\n{tail}{note_line}"


def format_progress_screen(
    exercise_name: str,
    sessions: list,  # list[analytics.SessionStats], ascending by date
    comparison,  # analytics.ComparisonDelta | None
    records,  # analytics.PersonalRecords
    limit: int = 8,
    unit: str = "kg",
    session_notes: dict[int, str] | None = None,  # {workout_id: note}
    golds=None,  # analytics.GoldBook | None
) -> str:
    u = unit_label(unit)
    lines = [f"📈 <b>{escape(exercise_name)}</b>", ""]
    if not sessions:
        lines.append(i18n.t("progress.no_history"))
        return "\n".join(lines)

    is_bw = sessions[-1].is_bodyweight_mode
    window = [s for s in sessions if s.sets]
    candidates = window[-limit:]

    if len(candidates) >= 2:
        # Anchored to the same window the chart plots (points[-limit:]), not the
        # full history — otherwise this delta and the chart's own title delta
        # (which is computed from that same limited window) disagree whenever a
        # shorter period is selected, and "с первой тренировки" would be a lie.
        first, last = candidates[0], candidates[-1]
        since = i18n.t("progress.since_first") if len(candidates) == len(window) else i18n.t("progress.since_period")
        if is_bw:
            delta = last.max_reps_in_set - first.max_reps_in_set
            lines.append(i18n.t("progress.reps_delta", delta=format_delta_reps(delta), since=since))
        else:
            delta = last.top_e1rm - first.top_e1rm
            lines.append(i18n.t("progress.e1rm_delta", delta=format_delta(delta, unit), since=since))

    # compute_personal_records honestly tracks both metrics independently of
    # session mode (max_e1rm from weighted sets, max_reps_at_weight from every
    # set including bodyweight ones) — but this screen used to pick only one
    # of the two records to print, gated on sessions[-1] alone. An exercise
    # that switched modes (e.g. weighted pull-ups, then bodyweight ones) lost
    # whichever record belonged to the mode the *last* session happened not
    # to be in — including a genuine, higher e1RM record vanishing from the
    # screen entirely. Show each record whose mode actually occurs in the
    # history, not just the last session's mode.
    have_weighted = any(not s.is_bodyweight_mode for s in sessions if s.sets)
    have_bw = any(s.is_bodyweight_mode for s in sessions if s.sets)
    if have_weighted:
        lines.append(i18n.t(
            "progress.record_e1rm",
            set=format_set(records.best_e1rm_weight, records.best_e1rm_reps),
            value=format_weight(records.max_e1rm),
            u=u,
        ))
    if have_bw:
        best_reps = max(records.max_reps_at_weight.values()) if records.max_reps_at_weight else 0
        lines.append(i18n.t("progress.record_reps", n=best_reps))

    gold_lines = build_gold_book_lines(golds, unit=unit, is_bodyweight=is_bw)
    if gold_lines:
        lines.append("")
        lines.extend(gold_lines)

    header = "\n".join(lines)
    notes = session_notes or {}
    # Каждая сессия подписывается своим собственным режимом (повторы или
    # e1RM), а не режимом последней тренировки в истории — иначе тяжёлая
    # тренировка с весом подписывалась бы «всего повторов», если человек
    # позже перешёл на упражнение своим весом (и наоборот).
    blocks = [
        _progress_session_block(s, s.is_bodyweight_mode, notes.get(s.workout_id))
        for s in reversed(candidates)
    ]  # newest first

    # A bodyweight exercise's screen is measured in reps and never prints an
    # e1RM, so there's nothing for the footnote to explain there — but if the
    # history also has weighted sessions, the hint is still relevant.
    footer = _e1rm_hint() if have_weighted else None

    sep = "\n\n"

    def assemble(keep: list[str]) -> str:
        # Пустая строка перед тогглом: шапка с золотой книгой и свёрнутый
        # список тренировок — разные блоки, слипшиеся они читались как один.
        parts = [f"{header}\n\n{collapsible_if_long(sep.join(keep))}"]
        if len(window) > len(keep):
            parts.append(i18n.t("progress.shown_of", kept=len(keep), total=len(window), n=len(window)))
        if footer:
            parts.append(footer)
        return "\n\n".join(parts).rstrip()

    # Drop the oldest sessions until the whole thing fits the caption cap. The
    # finished text is what gets measured (folding doesn't shrink it, and the
    # "Показано N из M" tail grows as sessions are dropped), so this can't be a
    # length estimate — it has to be the real string.
    kept = blocks
    text = assemble(kept)
    while len(kept) > 1 and telegram_length(text) > CAPTION_LIMIT:
        kept = kept[:-1]
        text = assemble(kept)
    return text


def _bodyweight_days(logs: list) -> list[tuple[dt.date, float]]:
    """Одна строка на день — последнее взвешивание этого дня, по возрастанию дат.

    Взвеситься можно и трижды подряд (переоделся, сходил в туалет, передумал), и
    раньше каждая попытка занимала отдельную строку списка: три записи за одну
    дату выглядели тремя днями. Вес — величина дня, а не минуты.
    """
    by_date: dict[dt.date, float] = {}
    for r in logs:
        by_date[dt.datetime.fromisoformat(r["logged_at"]).date()] = float(r["weight"])
    return sorted(by_date.items())


def build_bodyweight_screen(logs: list, unit: str = "kg", period_logs: list | None = None) -> str:
    """Text for the ⚖️ Вес тела screen: latest value, entry count, and a
    date - weight list for the selected period.

    logs: all rows with `weight` and `logged_at`, ascending by date (as
    db.list_bodyweight_logs returns). period_logs: the subset to list
    (defaults to `logs`) — the caller windows this by the selected period.

    The date - weight list folds behind Telegram's own expandable blockquote
    once it's long enough (collapsible_if_long — same client-side fold as the
    session list on the 📈 Progress screen), instead of a button round-trip.

    The entry list is trimmed to fit CAPTION_LIMIT: this text is sent as a photo
    caption, and an over-long one doesn't truncate — safe_edit_photo has already
    deleted the previous screen by the time the send fails, so the whole screen
    would vanish from the chat. Same guard, and same reason, as
    format_progress_screen.
    """
    u = unit_label(unit)
    if not logs:
        return i18n.t("bodyweight.empty")
    days = _bodyweight_days(logs)
    latest = logs[-1]
    latest_weight = latest["weight"]
    d = dt.datetime.fromisoformat(latest["logged_at"])
    head = [
        i18n.t("bodyweight.header"),
        "",
        i18n.t("bodyweight.now_line", weight=format_weight(latest_weight), u=u, date=format_date_ru(d)),
        i18n.t("bodyweight.total_entries", n=len(days)),
        "",
    ]

    entries = list(reversed(_bodyweight_days(logs if period_logs is None else period_logs)))
    rendered = [f"{day.strftime('%d.%m.%Y')} — {format_weight(weight)}{u}" for day, weight in entries]

    def assemble(keep: list[str]) -> str:
        lines = list(head)
        lines.append(collapsible_if_long("\n".join(keep)))
        if len(keep) < len(rendered):
            lines.append(i18n.t("bodyweight.shown_of", kept=len(keep), total=len(rendered)))
        lines.append("")
        lines.append(i18n.t("bodyweight.prompt"))
        return "\n".join(lines)

    kept = rendered
    text = assemble(kept)
    while len(kept) > 1 and telegram_length(text) > CAPTION_LIMIT:
        kept = kept[:-1]  # oldest first — the recent entries are the interesting ones
        text = assemble(kept)
    return text


def build_bodyweight_list_screen(
    rows: list, unit: str, page: int, page_size: int, total: int
) -> str:
    """Text for the "✏️ Записи" screen: every raw entry (not collapsed by day,
    unlike build_bodyweight_screen) so a duplicate same-day weigh-in can be
    told apart and deleted individually. rows: one page, newest-first (as
    db.list_bodyweight_logs_page returns).
    """
    u = unit_label(unit)
    head = [i18n.t("bodyweight_list.header"), ""]
    if not rows:
        head.append(i18n.t("bodyweight_list.empty"))
        return "\n".join(head)
    lines = [
        f"{i}. {dt.datetime.fromisoformat(r['logged_at']).strftime('%d.%m.%Y %H:%M')} "
        f"— {format_weight(r['weight'])}{u}"
        for i, r in enumerate(rows, start=1)
    ]
    text = "\n".join(head + lines)
    if total > page_size:
        start = page * page_size + 1
        text += f"\n\n{i18n.t('bodyweight_list.shown_of', start=start, end=start + len(rows) - 1, total=total)}"
    text += i18n.t("bodyweight_list.delete_hint")
    return text


def format_progression_hint(suggestion, achieved: bool = False) -> str:
    """"Цель: …" nudge from analytics.suggest_progression, on its own line under
    the "Прошлый раз" line (no bold — the surrounding line is already italicized).
    """
    if suggestion.is_bodyweight:
        goal = i18n.t("progression.goal_reps", n=suggestion.target_reps)
    else:
        goal = format_set(suggestion.target_weight, suggestion.target_reps)
    if achieved:
        return i18n.t("progression.goal_achieved", goal=goal)
    return i18n.t("progression.goal", goal=goal, reason=_progression_reason(suggestion))


def _progression_reason(suggestion) -> str:
    """Short "почему именно столько" clause, only where the number surprises.

    An unexplained prescribed weight reads as arbitrary — the commonest
    complaint about apps that hand out numbers. But this line is redrawn on
    every logged set, so it earns its width only when the target jumps: the
    weight went up because the rep range topped out. The "+1 повтор at the same
    weight" case explains itself against the "Прошлый раз" line right above,
    and a clause there would be noise on every single render.
    """
    if suggestion.action != "add_weight" or not suggestion.from_reps:
        return ""
    # Цель из правила программы объясняется правилом, а не общим наблюдением:
    # человек это правило видел в превью и должен узнать его здесь.
    if getattr(suggestion, "from_rule", False):
        return i18n.t("progression.reason_from_rule", n=suggestion.from_reps)
    return i18n.t("progression.reason_add_weight", n=suggestion.from_reps)


# ---------- дневник питания ----------


@dataclass
class FoodItemView:
    """One product within a logged meal, with its own portion and КБЖУ."""
    name: str
    portion: str = ""
    calories: float | None = None
    protein: float | None = None
    fat: float | None = None
    carbs: float | None = None


@dataclass
class FoodEntryView:
    """One logged meal, as the food-diary screen shows it."""
    id: int
    description: str
    items: list[FoodItemView] | None = None
    calories: float | None = None
    protein: float | None = None
    fat: float | None = None
    carbs: float | None = None
    has_photo: bool = False


def format_kcal(value: float | None) -> str:
    return "—" if value is None else i18n.t("food.kcal", n=f"{round(value):g}")


def _macros_line(protein: float | None, fat: float | None, carbs: float | None) -> str:
    """"Б30 · Ж12 · У60" / "P30 · F12 · C60" — skipped entirely when the model
    gave no macros. No space between the label and the number (same compact
    style as "80×5" for a set or "@9" for RPE elsewhere), and no trailing
    "г"/"g": the labels already say these are grams, unlike a bare number
    that needs a unit.
    """
    parts = [
        (label, v)
        for label, v in (
            (i18n.t("food.macro_protein_label"), protein),
            (i18n.t("food.macro_fat_label"), fat),
            (i18n.t("food.macro_carbs_label"), carbs),
        )
        if v is not None
    ]
    if not parts:
        return ""
    return " · ".join(f"{label}{round(v):g}" for label, v in parts)


def _item_line(item: FoodItemView) -> str:
    """"Гранола — 150 г — 630 ккал (Б 15 · Ж 20 · У 90 г)" — portion and macros
    only when the model actually gave them, so a bare guess doesn't show "None"."""
    head = escape(item.name)
    if item.portion:
        head += f" — {escape(item.portion)}"
    head += f" — {format_kcal(item.calories)}"
    macros = _macros_line(item.protein, item.fat, item.carbs)
    return f"{head} ({macros})" if macros else head


def build_food_estimate_text(
    description: str,
    items: list[FoodItemView] | None = None,
    calories: float | None = None,
    protein: float | None = None,
    fat: float | None = None,
    carbs: float | None = None,
    comment: str = "",
    header: str | None = None,
) -> str:
    """The confirmation card shown after the model reads a meal, and (with a
    different header) the preview of a correction.

    `header` default — None, а не готовая русская строка: как и в
    build_history_list, обычная константа в сигнатуре застыла бы на языке,
    который был активен при импорте модуля.
    """
    if header is None:
        header = i18n.t("food.estimate_header")
    lines = [header, "", f"<b>{escape(description or i18n.t('food.default_meal_name'))}</b>"]
    # Один компонент ничего не добавляет к названию приёма — не дублируем
    # (та же логика, что в build_food_day_screen).
    if items and len(items) > 1:
        lines.extend(f"• {_item_line(i)}" for i in items)
    if calories is not None:
        # Ничего не выдумываем: без числа "Итого" не показываем вовсе (а не
        # "Итого: —") — так карточка от режима "без КБЖУ" не выглядит
        # недосчитанной, ей просто нечего тут показывать.
        macros = _macros_line(protein, fat, carbs)
        totals = f"{format_kcal(calories)} · {macros}" if macros else format_kcal(calories)
        lines.append("")
        lines.append(i18n.t("food.totals_line", totals=totals))
    if comment:
        lines.append("")
        lines.append(f"<i>{escape(comment)}</i>")
    return "\n".join(lines)


def build_food_day_screen(date: dt.date, entries: list[FoodEntryView]) -> str:
    """One day of the diary: every meal with its per-item КБЖУ, then the day's total."""
    head = i18n.t("food.day_header", date=format_date_ru(dt.datetime.combine(date, dt.time())))
    if not entries:
        return i18n.t("food.day_empty", head=head)

    def render_entry(i: int, e: FoodEntryView) -> list[str]:
        out = []
        photo = " 📷" if e.has_photo else ""
        out.append(f"<b>{i}. {escape(shorten(e.description, FOOD_DESC_LIMIT))}</b>{photo}")
        items = e.items or []
        # Один компонент ничего не добавляет к названию приёма — не дублируем.
        # Больше одного — под сворачиваемую цитату, а не разворачиваем на весь
        # экран: раскладка нужна для точности, а не для чтения на каждый день.
        if len(items) > 1:
            breakdown = "\n".join(f"• {_item_line(item)}" for item in items)
            out.append(collapsible(f"<i>{breakdown}</i>"))
        # Калории приёма и его БЖУ — одной итоговой строкой под раскладкой,
        # а не калории наверху при названии и БЖУ отдельно внизу.
        totals = [p for p in (format_kcal(e.calories) if e.calories is not None else "",
                               _macros_line(e.protein, e.fat, e.carbs)) if p]
        if totals:
            out.append(f"<b>{' · '.join(totals)}</b>")
        return out

    # Номер приёма — тот же, что на кнопке «🗑 N», поэтому нумерация считается
    # по всему дню, а не по показанному: при обрезке уезжает начало списка, а
    # номера оставшихся не должны съезжать относительно клавиатуры.
    rendered = [render_entry(i, e) for i, e in enumerate(entries, start=1)]

    def assemble(keep: list[list[str]]) -> str:
        lines = [head, ""]
        if len(keep) < len(rendered):
            lines.append(i18n.t("food.shown_last_of_day", kept=len(keep), total=len(rendered)))
            lines.append("")
        for j, block in enumerate(keep):
            if j > 0:
                # Пустая строка только МЕЖДУ приёмами — над чертой-разделителем её
                # быть не должно, последний приём должен идти к ней вплотную.
                lines.append("")
            lines.extend(block)
        lines.append(_food_day_footer(entries))
        return "\n".join(lines)

    # Итоги дня считаются по всем записям, даже если часть не поместилась на
    # экран: «Итого за день» обязано быть честным, иначе обрезка тихо занижает
    # съеденное. Обрезается только список — с головы, самое старое.
    kept = rendered
    text = assemble(kept)
    while len(kept) > 1 and telegram_length(text) > MESSAGE_LIMIT:
        kept = kept[1:]
        text = assemble(kept)
    return text


def _food_day_footer(entries: list[FoodEntryView]) -> str:
    """Итоговая строка дня плюс подсказка — то, что идёт под списком приёмов."""
    lines: list[str] = []
    known = [e.calories for e in entries if e.calories is not None]
    meals = i18n.t("food.meals_count", n=len(entries))
    day_macros = _macros_line(
        _sum_or_none(e.protein for e in entries),
        _sum_or_none(e.fat for e in entries),
        _sum_or_none(e.carbs for e in entries),
    )
    day_totals = [p for p in (format_kcal(sum(known)) if known else "", day_macros) if p]
    if day_totals:
        total_line = i18n.t(
            "food.day_total_line", divider=DIVIDER, totals=" · ".join(day_totals), meals=meals
        )
    else:
        total_line = i18n.t("food.day_total_line_no_cal", divider=DIVIDER, meals=meals)
    if known and len(known) < len(entries):
        total_line += i18n.t("food.without_calories", n=len(entries) - len(known))
    lines.append(total_line)
    lines.append("")
    lines.append(i18n.t("food.add_more_hint"))
    return "\n".join(lines)


def _sum_or_none(values) -> float | None:
    known = [v for v in values if v is not None]
    return sum(known) if known else None


@dataclass
class FoodDayView:
    """One day's row in the history list, with its totals and what was eaten."""
    date: dt.date
    entries: int
    calories: float | None = None
    protein: float | None = None
    fat: float | None = None
    carbs: float | None = None
    descriptions: list[str] = field(default_factory=list)


def build_food_history_list(days: list[FoodDayView]) -> str:
    """The history tab: one block per logged day, newest first — date, totals,
    entry count, then the food itself folded behind a collapsible quote."""
    if not days:
        return i18n.t("food.history_empty")
    day_blocks = []
    for d in days:
        meals = i18n.t("food.meals_count", n=d.entries)
        macros = _macros_line(d.protein, d.fat, d.carbs)
        totals = [p for p in (format_kcal(d.calories) if d.calories is not None else "", macros) if p]
        block = [f"<b>{format_date_ru(dt.datetime.combine(d.date, dt.time()))}</b>"]
        if totals:
            block.append(" · ".join(totals))
        block.append(meals)
        if d.descriptions:
            food_list = "\n".join(f"{i}. {escape(name)}" for i, name in enumerate(d.descriptions, start=1))
            block.append(collapsible(food_list))
        day_blocks.append("\n".join(block))
    return i18n.t("food.history_header") + "\n\n" + "\n\n".join(day_blocks)
