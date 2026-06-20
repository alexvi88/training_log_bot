"""Tolerant parser for free-text set input (weight, reps[, set count])."""

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
    is_warmup: bool = False


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
        return [ParsedSet(weight=0.0, reps=reps, is_warmup=is_warmup)]

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
