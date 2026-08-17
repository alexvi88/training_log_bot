"""Turn a transcribed spoken set ("сто на восемь"/"one hundred eight for eight")
into a line the text parser already understands ("100 8").

Kept deliberately small and forgiving: transcription models usually emit digits
already, so the main job is (a) reading number words in either language when
they don't, and (b) treating gym connector words ("на"/"for", "по"/"by",
"раз"/"times", "подхода"/"sets") as the boundary between weight and reps.
Anything it can't make sense of returns None, and the caller falls back to
asking the user to type.

Both languages are recognised unconditionally, not switched on i18n.get_lang():
Russian and English number words use disjoint alphabets (Cyrillic vs Latin), so
there's no ambiguity to resolve by picking one — trying both only ever adds
coverage, never a false positive. This also matters because a Russian-speaking
lifter's speech routinely has English exercise-name fragments in it ("жим лежа"
next to "bench press"), while the reverse essentially never happens — so even a
lang-keyed design would have had to special-case RU→include-EN-numbers anyway.
Trying both everywhere is simpler and also more robust to a wrong STT language
hint (see ai_trainer.transcribe_voice).
"""

import re

_WORD_UNITS = {
    # Русские.
    "ноль": 0, "один": 1, "одна": 1, "одно": 1, "два": 2, "две": 2, "три": 3,
    "четыре": 4, "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9,
    "десять": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13,
    "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17,
    "восемнадцать": 18, "девятнадцать": 19,
    # English. "oh"/"o" — spoken zero, only meaningful as a digit inside a
    # digit-group number ("one oh five"), handled in _normalize_english_numbers.
    # Note "twenty" is NOT here even though it's a bare number word too — it
    # goes in _WORD_TENS below, because unlike "one".."nineteen" it also has
    # to combine with a following unit ("twenty-five" = 20 + 5) via the same
    # decreasing-rank merge Russian "двадцать" uses. Duplicating it into both
    # dicts would make the tens+unit merge below skip it as already "used up"
    # at rank 1.
    "zero": 0, "oh": 0, "o": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_WORD_TENS = {
    # Русские.
    "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50, "шестьдесят": 60,
    "семьдесят": 70, "восемьдесят": 80, "девяносто": 90,
    # English.
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
_WORD_HUNDREDS = {
    # Russian hundreds are fixed-value words ("двести" IS 200) and slot
    # straight into the rank/accumulate machine below. English has no
    # equivalent here on purpose: "two hundred" is unit(2) * multiplier
    # ("hundred"), not a word meaning 200 outright — that grammar is handled
    # separately by _normalize_english_numbers before this dict is consulted.
    "сто": 100, "двести": 200, "триста": 300, "четыреста": 400, "пятьсот": 500,
    "шестьсот": 600, "семьсот": 700, "восемьсот": 800, "девятьсот": 900,
}

# Chunk boundaries between separate sets in one utterance.
_CHUNK_SPLIT_RE = re.compile(
    r"[,\n]|потом|затем|далее|дальше|ещё|еще|\bthen\b", re.IGNORECASE
)

_TOKEN_RE = re.compile(r"[а-яёa-z]+|\d+", re.IGNORECASE)

# English-only views of the merged dicts above, needed by the hundred/
# digit-group grammar in _normalize_english_numbers (it has to tell "this
# English unit word is about to combine with a following tens/hundred word"
# apart from the Russian entries, which never participate in that grammar).
_EN_UNIT_VALUES = {k: v for k, v in _WORD_UNITS.items() if k.isascii()}
_EN_TENS_VALUES = {k: v for k, v in _WORD_TENS.items() if k.isascii()}


def _consume_plain_english(tokens: list[str], j: int) -> tuple[int, int]:
    """Read an optional tens[+unit] or a bare unit/teen starting at tokens[j] —
    the part that can follow "hundred" ("...hundred and twenty five",
    "...hundred eight"). Returns (value, next index); value is 0 and index
    unchanged if nothing plausible is there."""
    if j < len(tokens):
        tok = tokens[j]
        if tok in _EN_TENS_VALUES:
            val = _EN_TENS_VALUES[tok]
            k = j + 1
            if k < len(tokens) and _EN_UNIT_VALUES.get(tokens[k], 99) < 10:
                val += _EN_UNIT_VALUES[tokens[k]]
                k += 1
            return val, k
        if tok in _EN_UNIT_VALUES:
            return _EN_UNIT_VALUES[tok], j + 1
    return 0, j


def _normalize_english_numbers(tokens: list[str]) -> list[str]:
    """Collapse English number-word phrases the generic rank/accumulate loop
    below can't read on its own into single digit-string tokens, in place.

    Two constructs need this, both because English number grammar isn't "one
    fixed word per magnitude" the way Russian is:

    1. "hundred" is a *multiplier* word ("two hundred" = 2 × 100), not a fixed
       value like Russian "двести" — so it can't just sit in a rank-3 lookup
       table. "a hundred and five", "one hundred eight", "two hundred and
       twenty-five" are all handled here.
    2. Gym-speak digit-groups said like a room number: "two twenty-five" (225),
       "one thirty" (130), "one oh five" (105) — a lone 1-9 word immediately
       followed by a tens/teen word (or "oh"+digit) means "hundreds digit,
       then the rest", not two separate numbers. The generic decreasing-rank
       merge below would otherwise flush "two" as its own number the moment
       "twenty" (higher rank) arrived.

    Plain compounds without this ambiguity ("twenty-five", "eighty") are left
    alone — the generic loop already merges tens+units by decreasing rank,
    same mechanism as Russian "сто двадцать пять".
    """
    out: list[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        # "a hundred" / "an hundred and five" — implicit unit of one, same
        # shape as the bare-"hundred" branch below.
        if tok in ("a", "an") and i + 1 < n and tokens[i + 1] == "hundred":
            j = i + 2
            if j < n and tokens[j] == "and":
                j += 1
            rest, j = _consume_plain_english(tokens, j)
            out.append(str(100 + rest))
            i = j
            continue
        unit = _EN_UNIT_VALUES.get(tok)
        if unit is not None and unit < 10 and i + 1 < n and tokens[i + 1] == "hundred":
            j = i + 2
            if j < n and tokens[j] == "and":
                j += 1
            rest, j = _consume_plain_english(tokens, j)
            out.append(str(unit * 100 + rest))
            i = j
            continue
        if tok == "hundred":  # bare "hundred" — implicit unit of one.
            j = i + 1
            if j < n and tokens[j] == "and":
                j += 1
            rest, j = _consume_plain_english(tokens, j)
            out.append(str(100 + rest))
            i = j
            continue
        if unit is not None and unit < 10 and i + 1 < n and tokens[i + 1] in _EN_TENS_VALUES:
            # Digit-group: "two twenty(-five)" -> 220 or 225.
            tens_val = _EN_TENS_VALUES[tokens[i + 1]]
            j = i + 2
            extra = 0
            if j < n and _EN_UNIT_VALUES.get(tokens[j], 99) < 10:
                extra = _EN_UNIT_VALUES[tokens[j]]
                j += 1
            out.append(str(unit * 100 + tens_val + extra))
            i = j
            continue
        if unit is not None and unit < 10 and i + 1 < n and tokens[i + 1] in ("oh", "o"):
            # Digit-group: "one oh five" -> 105 (single trailing digit only).
            j = i + 2
            digit = _EN_UNIT_VALUES.get(tokens[j]) if j < n else None
            if digit is not None and digit < 10:
                out.append(str(unit * 100 + digit))
                i = j + 1
                continue
            # "oh" not followed by a plausible digit — not this pattern,
            # leave both tokens for the generic loop to deal with.
        out.append(tok)
        i += 1
    return out


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
#
# "point" is English's unambiguous decimal marker — same role as "точка", never
# a weight×reps connector, unlike Russian's ambiguous "и" (English has no
# equivalent ambiguity here: "and" is already fully consumed by
# _normalize_english_numbers wherever it could mean anything number-related).
_DECIMAL_MARKERS = {"и", "запятая", "точка", "point"}
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

    tokens = _normalize_english_numbers(_TOKEN_RE.findall(chunk.lower()))
    for tok in tokens:
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
