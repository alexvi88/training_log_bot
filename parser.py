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


_SEP = r"[xXхХ*/]"
_WEIGHT = r"\+?(?P<weight>\d+(?:[.,]\d+)?)"

_X_SEP_RE = re.compile(rf"^{_WEIGHT}\s*{_SEP}\s*(?P<reps>\d+)(?:\s*{_SEP}\s*(?P<count>\d+))?$")
_SPACE_SEP_RE = re.compile(rf"^{_WEIGHT}\s+(?P<reps>\d+)(?:\s+(?P<count>\d+))?$")
_BODYWEIGHT_RE = re.compile(r"^(?P<reps>\d+)$")

MAX_SETS_PER_TOKEN = 20


def parse_single_token(token: str) -> list[ParsedSet]:
    """Parse one weight/reps[/count] token, e.g. '100x8x3', '100 8', '8', '+20 8'."""
    text = token.strip()
    if not text:
        raise ParseError(EXAMPLES_HINT)

    bw_match = _BODYWEIGHT_RE.match(text)
    if bw_match:
        reps = int(bw_match.group("reps"))
        if reps <= 0:
            raise ParseError("Повторы должны быть больше 0")
        return [ParsedSet(weight=0.0, reps=reps, weight_omitted=True)]

    match = _X_SEP_RE.match(text) or _SPACE_SEP_RE.match(text)
    if not match:
        raise ParseError(EXAMPLES_HINT)

    weight = float(match.group("weight").replace(",", "."))
    reps = int(match.group("reps"))
    count = int(match.group("count")) if match.group("count") else 1

    if reps <= 0:
        raise ParseError("Повторы должны быть больше 0")
    if not (0 < count <= MAX_SETS_PER_TOKEN):
        raise ParseError("Странное количество подходов")

    return [ParsedSet(weight=weight, reps=reps) for _ in range(count)]


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
        raise ParseError("Такой даты не существует")
    if date > dt.date.today():
        raise ParseError("Дата в будущем — для прошлой тренировки нужна дата не позже сегодня")
    return date
