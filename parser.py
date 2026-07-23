"""Tolerant parser for free-text set input (weight, reps[, set count])."""

import datetime as dt
import re
from dataclasses import dataclass

EXAMPLES_HINT = "Не понял ввод. Примеры: 100 8 · 100x8 · 100x8x3 · +20 8 · 8 (свой вес)"


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

# Cap on how many sets one multi-token line ("100 8, 100 7, 95 8") may produce,
# so a pasted wall of text can't spawn hundreds of DB writes in one message.
MAX_SETS_PER_LINE = 40

# Separators between sets on a single line: comma, semicolon, or newline. Spaces
# are *not* here on purpose — "100 8" is one set (weight + reps), not two.
_LINE_SPLIT_RE = re.compile(r"[,;\n]+")


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
    if not (0 < count <= MAX_SETS_PER_TOKEN):
        raise ParseError("Странное количество подходов")

    return [ParsedSet(weight=weight, reps=reps, rpe=rpe) for _ in range(count)]


def parse_sets_line(text: str) -> list[ParsedSet]:
    """Parse one message that may hold several sets, split by comma/semicolon/newline.

    "100 8" stays one set; "100 8, 100 7, 95 8" becomes three. Each chunk goes
    through parse_single_token, so every per-token form (counts like 100x8x3,
    bare reps, @RPE, +weight) still works inside a chunk. A single bad chunk
    fails the whole line — partial logging would be more confusing than a reparse.
    """
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


def parse_ru_date(text: str) -> dt.date:
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
    if date > dt.date.today():
        raise ParseError("Дата в будущем — для прошлой тренировки нужна дата не позже сегодня")
    return date
