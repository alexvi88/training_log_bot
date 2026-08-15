"""Turn a transcribed spoken set ("сто на восемь", "100 8 три подхода") into a
line the text parser already understands ("100 8").

Kept deliberately small and forgiving: transcription models usually emit digits
already, so the main job is (a) reading Russian number words when they don't, and
(b) treating gym connector words ("на", "по", "раз", "подхода") as the boundary
between weight and reps. Anything it can't make sense of returns None, and the
caller falls back to asking the user to type.
"""

import re

_WORD_UNITS = {
    "ноль": 0, "один": 1, "одна": 1, "одно": 1, "два": 2, "две": 2, "три": 3,
    "четыре": 4, "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9,
    "десять": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13,
    "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17,
    "восемнадцать": 18, "девятнадцать": 19,
}
_WORD_TENS = {
    "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50, "шестьдесят": 60,
    "семьдесят": 70, "восемьдесят": 80, "девяносто": 90,
}
_WORD_HUNDREDS = {
    "сто": 100, "двести": 200, "триста": 300, "четыреста": 400, "пятьсот": 500,
    "шестьсот": 600, "семьсот": 700, "восемьсот": 800, "девятьсот": 900,
}

# Chunk boundaries between separate sets in one utterance.
_CHUNK_SPLIT_RE = re.compile(r"[,\n]|потом|затем|далее|дальше|ещё|еще", re.IGNORECASE)

_TOKEN_RE = re.compile(r"[а-яёa-z]+|\d+", re.IGNORECASE)


_RANK_NONE = 4  # sentinel above hundreds so the first component is always accepted

# A plate-loaded stack's real increments are commonly one decimal digit
# (29.6кг, 39.3, 83.6, 95.3, 97.6 kg — a fixed lb-per-plate step converted to
# kg), so a fractional weight is routine here, not an edge case. Read as
# "девяносто семь и шесть" (whole + connector + fraction) or with an explicit
# "запятая"/"точка". Before this, "и" fell into the generic separator branch
# below like any other word — which didn't just mis-parse the weight, it lost
# the *reps* entirely: "97 и 6 на 8" flushed into two top-level numbers (97, 6)
# before "8" was even reached, and transcript_to_sets_line only keeps the
# first two numbers of a chunk.
#
# "и" specifically is ambiguous, unlike the explicit "запятая"/"точка": "сто и
# восемь" reads exactly like "сто восемь" — a bare weight-and-reps pair with
# the "на" dropped — every bit as much as "девяносто семь и шесть" reads like
# a decimal. The two only tell apart by what follows: when a real reps number
# shows up afterwards ("...и шесть на восемь" — "на" already carries the
# reps), the "и"-glued number can only be finishing the weight, so it stays a
# decimal. When nothing follows ("сто и восемь" is the whole utterance), there
# is no way to tell "100.8" from "100 reps-of-8" apart from world knowledge —
# so resolve toward the more common gym-log shape, weight × reps, and split
# it back into two numbers ("100 8"). See `_chunk_to_numbers` for where that
# split happens. "запятая"/"точка" are unambiguous on their own (nobody says
# "сто точка восемь" to mean "100 reps of 8"), so they always stay a decimal.
_DECIMAL_MARKERS = {"и", "запятая", "точка"}
_AMBIGUOUS_DECIMAL_MARKER = "и"
# "два с половиной" — "с" is the bare preposition and mustn't flush the number
# in progress; "половиной"/etc. supplies the ".5".
_IGNORED_TOKENS = {"с", "со"}
_HALF_WORDS = {"половина", "половиной", "половину"}


def _decimal_fraction(value: int) -> float:
    """1-9 spoken/typed after a decimal marker is tenths (…97.6), 10-99 is
    hundredths (…97.65) — matches how many digits would follow the point if
    written out."""
    return value / 10 if value < 10 else value / 100


def _chunk_to_numbers(chunk: str) -> list[float]:
    """Read the numbers out of one chunk, in order.

    Number words accumulate only while their magnitude strictly decreases
    (hundreds → tens → units), so "сто двадцать пять" is 125 but "восемь три"
    (two units in a row — never one number in Russian) splits into 8 and 3.
    """
    numbers: list[float] = []
    # Indices into `numbers` that came from an ambiguous "и"-decimal (as
    # opposed to an explicit "запятая"/"точка", or a plain non-decimal
    # number) — see the split-back-apart step at the end of this function.
    ambiguous_indices: set[int] = set()
    current: float = 0
    last_rank = _RANK_NONE
    awaiting_decimal = False
    decimal_marker_is_ambiguous = False

    def flush(ambiguous: bool = False) -> None:
        nonlocal current, last_rank
        if last_rank != _RANK_NONE:
            if ambiguous:
                ambiguous_indices.add(len(numbers))
            numbers.append(current)
        current = 0
        last_rank = _RANK_NONE

    def add(value: int, rank: int) -> None:
        nonlocal current, last_rank
        if rank >= last_rank:
            flush()
        current += value
        last_rank = rank

    for tok in _TOKEN_RE.findall(chunk.lower()):
        if tok in _IGNORED_TOKENS:
            continue
        if tok in _HALF_WORDS:
            if last_rank != _RANK_NONE:
                current += 0.5
            flush()
            continue
        if tok in _DECIMAL_MARKERS:
            # Only a decimal point if it's actually gluing onto a number —
            # otherwise (stray "и" with nothing before it) fall back to the
            # old boundary behaviour.
            awaiting_decimal = last_rank != _RANK_NONE
            decimal_marker_is_ambiguous = tok == _AMBIGUOUS_DECIMAL_MARKER
            if not awaiting_decimal:
                flush()
            continue
        if awaiting_decimal:
            awaiting_decimal = False
            frac_value = int(tok) if tok.isdigit() else _WORD_UNITS.get(tok)
            if frac_value is not None:
                current += _decimal_fraction(frac_value)
                # Single-digit fractions ("...и восемь" → .8) are exactly the
                # range a reps count would also fall in, so only those are
                # eligible for the trailing split below; two-digit fractions
                # ("...и шестьдесят пять" → .65) don't read as a plausible
                # reps count and stay an unambiguous decimal either way.
                flush(ambiguous=decimal_marker_is_ambiguous and frac_value < 10)
                continue
            # Whatever followed the marker wasn't a plausible fraction digit —
            # treat the marker as if it had just been an ordinary boundary and
            # fall through to parse this token normally.
            flush()

        if tok.isdigit():
            flush()
            numbers.append(int(tok))
        elif tok in _WORD_HUNDREDS:
            add(_WORD_HUNDREDS[tok], 3)
        elif tok in _WORD_TENS:
            add(_WORD_TENS[tok], 2)
        elif tok in _WORD_UNITS:
            add(_WORD_UNITS[tok], 1)
        else:
            # Separator or unknown word (exercise name, "килограмм", filler): both
            # act as a boundary that flushes any number in progress.
            flush()
    flush()

    # An ambiguous "и"-decimal that turned out to be the *last* number in the
    # chunk means nothing disambiguated it as a fraction (no further reps
    # number showed up afterwards) — resolve toward weight × reps, the more
    # common shape for a complete gym-log utterance, by splitting it back into
    # a whole part and a small integer: "сто и восемь" → [100, 8], not [100.8].
    # When something *does* follow ("...и шесть на восемь"), the decimal
    # merge already happened above and is left alone — see the ambiguous_indices
    # check only ever looking at the trailing slot.
    if numbers and (len(numbers) - 1) in ambiguous_indices:
        value = numbers[-1]
        whole = int(value)
        frac_digit = round((value - whole) * 10)
        if frac_digit:
            numbers[-1:] = [float(whole), float(frac_digit)]

    return numbers


def _format_number(n: float) -> str:
    """Whole numbers print bare ("100"); fractions keep only as many decimal
    digits as they actually have ("97.6", not "97.60")."""
    if n == int(n):
        return str(int(n))
    return f"{n:.2f}".rstrip("0").rstrip(".")


def transcript_to_sets_line(text: str) -> str | None:
    """Best-effort "100 8, 95 8"-style line from a transcript, or None if no
    numbers were found. Only weight+reps are taken per chunk — spoken set counts
    are dropped rather than guessed at."""
    line, _ = transcript_to_sets_line_with_hint(text)
    return line


def transcript_to_sets_line_with_hint(text: str) -> tuple[str | None, bool]:
    """Same as `transcript_to_sets_line`, plus whether a trailing number (a
    spoken set count, "...три подхода") was dropped from any chunk — the caller
    can then warn the user that only one set from that phrase was recorded,
    instead of silently under-logging "100 8 три подхода" as a single set.
    """
    if not text:
        return None, False
    lines: list[str] = []
    dropped = False
    for chunk in _CHUNK_SPLIT_RE.split(text):
        nums = _chunk_to_numbers(chunk)
        if not nums:
            continue
        if len(nums) > 2 and nums[2] > 1:
            dropped = True
        nums = nums[:2]  # weight, reps — ignore any trailing "three sets"
        if len(nums) == 1:
            lines.append(_format_number(nums[0]))  # a lone number = bodyweight reps
        else:
            lines.append(f"{_format_number(nums[0])} {_format_number(nums[1])}")
    return (", ".join(lines) if lines else None), dropped
