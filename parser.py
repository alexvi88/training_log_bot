"""Tolerant parser for free-text set input (weight, reps[, set count])."""

import datetime as dt
import re
from dataclasses import dataclass, field

EXAMPLES_HINT = "Не понял ввод. Примеры: 100 8 · 100x8 · 100x8x3 · +20 8 · 8 (свой вес)"


class ParseError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


@dataclass
class ParsedSet:
    weight: float
    reps: int
    is_warmup: bool = False
    weight_omitted: bool = False  # bare reps, e.g. "8" — caller may fill weight from the previous set


_WARMUP_PREFIX_RE = re.compile(r"^\s*w\s+", re.IGNORECASE)
_WARMUP_SUFFIX_RE = re.compile(r"\s*разм\.?\s*$", re.IGNORECASE)

_SEP = r"[xXхХ*/]"
_WEIGHT = r"\+?(?P<weight>\d+(?:[.,]\d+)?)"

_X_SEP_RE = re.compile(rf"^{_WEIGHT}\s*{_SEP}\s*(?P<reps>\d+)(?:\s*{_SEP}\s*(?P<count>\d+))?$")
_SPACE_SEP_RE = re.compile(rf"^{_WEIGHT}\s+(?P<reps>\d+)(?:\s+(?P<count>\d+))?$")
_BODYWEIGHT_RE = re.compile(r"^(?P<reps>\d+)$")

MAX_SETS_PER_TOKEN = 20


def _strip_warmup(text: str) -> tuple[str, bool]:
    is_warmup = False
    stripped = text
    if _WARMUP_PREFIX_RE.search(stripped):
        stripped = _WARMUP_PREFIX_RE.sub("", stripped)
        is_warmup = True
    if _WARMUP_SUFFIX_RE.search(stripped):
        stripped = _WARMUP_SUFFIX_RE.sub("", stripped)
        is_warmup = True
    return stripped.strip(), is_warmup


def parse_single_token(token: str) -> list[ParsedSet]:
    """Parse one weight/reps[/count] token, e.g. '100x8x3', '100 8', '8', '+20 8'."""
    raw = token.strip()
    if not raw:
        raise ParseError(EXAMPLES_HINT)

    text, is_warmup = _strip_warmup(raw)
    if not text:
        raise ParseError(EXAMPLES_HINT)

    bw_match = _BODYWEIGHT_RE.match(text)
    if bw_match:
        reps = int(bw_match.group("reps"))
        if reps <= 0:
            raise ParseError("Повторы должны быть больше 0")
        return [ParsedSet(weight=0.0, reps=reps, is_warmup=is_warmup, weight_omitted=True)]

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

    return [ParsedSet(weight=weight, reps=reps, is_warmup=is_warmup) for _ in range(count)]


# ---------- bulk backfill input: "Упражнение: 100x8, 100x7, 90x8" per line ----------

@dataclass
class BulkExerciseEntry:
    name: str
    sets: list[ParsedSet] = field(default_factory=list)


def parse_bulk_session(text: str) -> list[BulkExerciseEntry]:
    """Parse a multi-line bulk session, one exercise per line: 'Имя: сет, сет, ...'."""
    entries: list[BulkExerciseEntry] = []
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if ":" not in line:
            raise ParseError(
                f"Строка {line_no}: нужен формат «Упражнение: сеты», например «Присед: 100x8, 100x7»"
            )
        name, sets_part = line.split(":", 1)
        name = name.strip()
        if not name:
            raise ParseError(f"Строка {line_no}: не указано название упражнения")
        tokens = [t.strip() for t in sets_part.split(",") if t.strip()]
        if not tokens:
            raise ParseError(f"Строка {line_no}: не указаны сеты для «{name}»")
        sets: list[ParsedSet] = []
        for token in tokens:
            try:
                sets.extend(parse_single_token(token))
            except ParseError as e:
                raise ParseError(f"Строка {line_no} («{name}»): {e.message}")
        entries.append(BulkExerciseEntry(name=name, sets=sets))
    if not entries:
        raise ParseError("Не нашёл ни одного упражнения. Формат: «Упражнение: 100x8, 100x7»")
    return entries


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
