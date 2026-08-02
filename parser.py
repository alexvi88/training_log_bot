"""Tolerant parser for free-text set input (weight, reps[, set count])."""

import datetime as dt
import re
from dataclasses import dataclass

EXAMPLES_HINT = (
    "Не понял ввод. Примеры:\n"
    "100 8\n"
    "100x8\n"
    "100x8x3\n"
    "+20 8\n"
    "8 (свой вес)"
)


class ParseError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


@dataclass
class ParsedSet:
    weight: float
    reps: int
    weight_omitted: bool = False  # bare reps, e.g. "8" — caller may fill weight from the previous set
    rpe: float | None = None  # optional "@9" suffix; applies to every set produced by the token


_SEP = r"[xXхХ*/]"
_WEIGHT = r"\+?(?P<weight>\d+(?:[.,]\d+)?)"
# Optional trailing "@RPE", e.g. "@9" or "@8.5" — subjective effort 1-10.
_RPE = r"(?:\s*@\s*(?P<rpe>\d+(?:[.,]\d+)?))?"

_X_SEP_RE = re.compile(rf"^{_WEIGHT}\s*{_SEP}\s*(?P<reps>\d+)(?:\s*{_SEP}\s*(?P<count>\d+))?{_RPE}$")
_SPACE_SEP_RE = re.compile(rf"^{_WEIGHT}\s+(?P<reps>\d+)(?:\s+(?P<count>\d+))?{_RPE}$")
_BODYWEIGHT_RE = re.compile(rf"^(?P<reps>\d+){_RPE}$")

MAX_SETS_PER_TOKEN = 20

# Sanity ceilings on a single set. Deliberately far above anything a human logs
# (the heaviest competition deadlift ever is ~500kg; a plate-loaded sled tops out
# well under this) — the job here is to reject the physically impossible, not to
# second-guess a real lift.
#
# Why this matters more than it looks: an extra zero is the commonest numeric
# typo, and an over-large set is silently permanent. It becomes the exercise's
# all-time record, so every later "vs предыдущего рекорда" is measured against
# it; it lands in lifetime tonnage and the Hall of Fame; and it unlocks the
# weight-club achievements, which are never revoked even after the set is fixed.
# Typos in the other direction are merely odd-looking, which is why the softer
# history-based nudge (handlers/workout._suspicious_weight_warning) was not
# enough on its own.
MAX_WEIGHT = 1500.0
MAX_REPS = 500

# Cap on how many sets one multi-token line ("100 8, 100 7, 95 8") may produce,
# so a pasted wall of text can't spawn hundreds of DB writes in one message.
MAX_SETS_PER_LINE = 40

# Separators between sets on a single line: comma, semicolon, or newline. Spaces
# are *not* here on purpose — "100 8" is one set (weight + reps), not two.
_LINE_SPLIT_RE = re.compile(r"[,;\n]+")

# A decimal weight typed with a comma ("102,5 8", normal for a RU keyboard) looks
# exactly like this line's own set separator once split, and the split runs
# first — "102,5 8" silently became two bogus sets (0×102 with carry-forward,
# then 5×8) instead of one 102.5kg set. Protect it: a comma between digits that
# reads like a decimal fraction (1-2 digits, then a non-digit or end) is turned
# into a dot before splitting. A comma actually separating two sets is either
# followed by a space ("100 8, 100 7") or by 3+ digits — both survive untouched.
_DECIMAL_COMMA_RE = re.compile(r"(?<=\d),(?=\d{1,2}(?:\D|$))")


def _parse_rpe(raw: str | None) -> float | None:
    if not raw:
        return None
    rpe = float(raw.replace(",", "."))
    if not (0 < rpe <= 10):
        raise ParseError("RPE должен быть от 1 до 10")
    return rpe


def parse_single_token(token: str) -> list[ParsedSet]:
    """Parse one weight/reps[/count][@rpe] token, e.g. '100x8x3', '100 8', '8', '+20 8', '100x8@9'."""
    text = token.strip()
    if not text:
        raise ParseError(EXAMPLES_HINT)

    bw_match = _BODYWEIGHT_RE.match(text)
    if bw_match:
        reps = int(bw_match.group("reps"))
        if reps <= 0:
            raise ParseError("Повторы должны быть больше 0")
        if reps > MAX_REPS:
            raise ParseError(f"Подозрительно много повторов (максимум {MAX_REPS}) — опечатка?")
        rpe = _parse_rpe(bw_match.group("rpe"))
        return [ParsedSet(weight=0.0, reps=reps, weight_omitted=True, rpe=rpe)]

    match = _X_SEP_RE.match(text) or _SPACE_SEP_RE.match(text)
    if not match:
        raise ParseError(EXAMPLES_HINT)

    weight = float(match.group("weight").replace(",", "."))
    reps = int(match.group("reps"))
    count = int(match.group("count")) if match.group("count") else 1
    rpe = _parse_rpe(match.group("rpe"))

    if reps <= 0:
        raise ParseError("Повторы должны быть больше 0")
    if reps > MAX_REPS:
        raise ParseError(f"Подозрительно много повторов (максимум {MAX_REPS}) — опечатка?")
    if weight > MAX_WEIGHT:
        raise ParseError(f"Подозрительно большой вес (максимум {MAX_WEIGHT:.0f}) — лишний ноль?")
    if not (0 < count <= MAX_SETS_PER_TOKEN):
        raise ParseError("Странное количество подходов")

    return [ParsedSet(weight=weight, reps=reps, rpe=rpe) for _ in range(count)]


# "2: 100 8" — replace the 2nd already-logged set of the active exercise.
# No existing token form starts with digits+colon, so this can't collide with
# a normal weight/reps entry (unlike ':' vs '.', which is why '.' isn't
# accepted here — "2.5 8" is a legitimate 2.5kg×8 set, not an edit marker).
_SET_EDIT_RE = re.compile(r"^(?P<index>\d+)\s*:\s*(?P<rest>.+)$")


def parse_set_edit(text: str) -> tuple[int, ParsedSet] | None:
    """Parse "N: 100 8" — replace the Nth already-logged set of the active
    exercise (1-based, in the order the tracker lists them) with a new
    weight/reps[@rpe]. Bare reps ("2: 8") keep whatever weight that set
    already had, exactly like a bare-reps token does when logging fresh.

    Returns None when `text` isn't this form at all, so callers fall through
    to the normal parse_sets_line path. Raises ParseError once it *is*
    recognisably this form but malformed: a non-positive index, or a right
    side that expands to more than one set — editing one set can't fan out
    into several, so "2: 100x8x3" is rejected rather than silently picking one.
    """
    match = _SET_EDIT_RE.match(text.strip())
    if not match:
        return None
    index = int(match["index"])
    if index <= 0:
        raise ParseError("Номер подхода должен быть больше 0")
    sets = parse_single_token(match["rest"])
    if len(sets) != 1:
        raise ParseError("Для правки укажи ровно один подход, без счётчика (например «2: 100 8»)")
    return index, sets[0]


def parse_sets_line(text: str) -> list[ParsedSet]:
    """Parse one message that may hold several sets, split by comma/semicolon/newline.

    "100 8" stays one set; "100 8, 100 7, 95 8" becomes three. Each chunk goes
    through parse_single_token, so every per-token form (counts like 100x8x3,
    bare reps, @RPE, +weight) still works inside a chunk. A single bad chunk
    fails the whole line — partial logging would be more confusing than a reparse.
    """
    text = _DECIMAL_COMMA_RE.sub(".", text)
    chunks = [c.strip() for c in _LINE_SPLIT_RE.split(text) if c.strip()]
    if not chunks:
        raise ParseError(EXAMPLES_HINT)
    sets: list[ParsedSet] = []
    for chunk in chunks:
        sets.extend(parse_single_token(chunk))
    if len(sets) > MAX_SETS_PER_LINE:
        raise ParseError(f"Слишком много подходов в одной строке (максимум {MAX_SETS_PER_LINE})")
    return sets


_BODYWEIGHT_VALUE_RE = re.compile(r"^\d+(?:[.,]\d+)?$")


def parse_bodyweight(text: str) -> float:
    """A single positive body weight, e.g. '80', '80.5', '80,5'."""
    raw = text.strip()
    if not _BODYWEIGHT_VALUE_RE.match(raw):
        raise ParseError("Не понял вес. Напиши число, например 80 или 80.5")
    weight = float(raw.replace(",", "."))
    if not (0 < weight < 1000):
        raise ParseError("Странный вес — напиши реальное число в кг/lb")
    return weight


# ---------- date input: дд.мм.гггг ----------

_DATE_RE = re.compile(r"^(?P<d>\d{1,2})[.\-/](?P<m>\d{1,2})[.\-/](?P<y>\d{2,4})$")


def parse_ru_date(text: str, today: dt.date | None = None) -> dt.date:
    raw = text.strip()
    match = _DATE_RE.match(raw)
    if not match:
        raise ParseError("Не понял дату. Формат: дд.мм.гггг, например 14.03.2025")
    day, month, year = int(match["d"]), int(match["m"]), int(match["y"])
    if year < 100:
        year += 2000
    try:
        date = dt.date(year, month, day)
    except ValueError:
        raise ParseError("Такой даты не существует") from None
    # today is passed in by callers that know the user's timezone — at the far
    # ends of the world the server's date is a day off, and typing your own
    # today's date shouldn't be rejected as "в будущем".
    if date > (today or dt.date.today()):
        raise ParseError("Дата в будущем — для прошлой тренировки нужна дата не позже сегодня")
    return date


# ---------- whole workout on one line ----------

# "жим 80x8x3, присед 100 5" — a name, then its sets, repeated. The split is on
# commas/newlines like parse_sets_line, but each chunk may carry a name, and a
# chunk without one continues the previous exercise ("жим 80x8, 75x8").
MAX_QUICK_ENTRIES = 15

# Where the name ends and the numbers begin: the first token that parses as a
# set. Names can contain digits ("жим 45 градусов" is not a set), so the split
# can't be "first digit" — it has to be "first thing that is a set".
_QUICK_NAME_MAX = 60


@dataclass
class QuickEntry:
    """One exercise and the sets typed for it on a quick-log line."""
    name: str
    sets: list[ParsedSet]


def _split_name_and_sets(chunk: str) -> tuple[str, list[ParsedSet]]:
    """Split at the earliest point whose tail parses as a set, so the name stays
    as short as the numbers allow.

    Shortest-name-first matters: in "присед 100 5" a longer name would swallow
    the weight ("присед 100" + 5 bodyweight reps). Names can still contain
    digits — "жим 45 градусов 60x10" only splits where the tail actually parses,
    which is after "градусов".
    """
    words = chunk.split()
    for cut in range(len(words) + 1):
        name = " ".join(words[:cut]).strip(" -–—:")
        tail = " ".join(words[cut:])
        if not tail:
            continue
        try:
            sets = parse_single_token(tail)
        except ParseError:
            continue
        return name, sets
    raise ParseError(f"Не понял, где тут вес и повторы: «{chunk}»")


def parse_quick_workout(text: str) -> list[QuickEntry]:
    """A whole session typed as one line: "жим 80x8x3, присед 100 5".

    Entries are comma/newline separated. A chunk that starts with a name opens a
    new exercise; a chunk that is only numbers adds another set to the exercise
    before it, so "жим 80x8, 75x8" is one exercise with two sets rather than a
    nameless second one.
    """
    text = _DECIMAL_COMMA_RE.sub(".", text)
    chunks = [c.strip() for c in _LINE_SPLIT_RE.split(text) if c.strip()]
    if not chunks:
        raise ParseError("Напиши, например: жим 80x8x3, присед 100x5")

    entries: list[QuickEntry] = []
    for chunk in chunks:
        name, sets = _split_name_and_sets(chunk)
        if not name:
            if not entries:
                raise ParseError(f"Не понял, какое это упражнение: «{chunk}»")
            entries[-1].sets.extend(sets)
            continue
        if len(name) > _QUICK_NAME_MAX:
            raise ParseError(f"Слишком длинное название: «{name[:30]}…»")
        entries.append(QuickEntry(name=name, sets=sets))

    if len(entries) > MAX_QUICK_ENTRIES:
        raise ParseError(f"Слишком много упражнений в одной строке (максимум {MAX_QUICK_ENTRIES})")
    total = sum(len(e.sets) for e in entries)
    if total > MAX_SETS_PER_LINE:
        raise ParseError(f"Слишком много подходов в одной строке (максимум {MAX_SETS_PER_LINE})")
    return entries
