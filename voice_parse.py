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

# Words that end the current number and start the next one (weight → reps).
_SEPARATORS = {
    "на", "по", "и", "раз", "раза", "разок", "повтор", "повтора", "повторов",
    "повторений", "повторения", "подход", "подхода", "подходов", "сет", "сета", "сетов",
}

# Chunk boundaries between separate sets in one utterance.
_CHUNK_SPLIT_RE = re.compile(r"[,\n]|потом|затем|далее|дальше|ещё|еще", re.IGNORECASE)

_TOKEN_RE = re.compile(r"[а-яёa-z]+|\d+", re.IGNORECASE)


_RANK_NONE = 4  # sentinel above hundreds so the first component is always accepted


def _chunk_to_numbers(chunk: str) -> list[int]:
    """Read the numbers out of one chunk, in order.

    Number words accumulate only while their magnitude strictly decreases
    (hundreds → tens → units), so "сто двадцать пять" is 125 but "восемь три"
    (two units in a row — never one number in Russian) splits into 8 and 3.
    """
    numbers: list[int] = []
    current = 0
    last_rank = _RANK_NONE

    def flush() -> None:
        nonlocal current, last_rank
        if last_rank != _RANK_NONE:
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
    return numbers


def transcript_to_sets_line(text: str) -> str | None:
    """Best-effort "100 8, 95 8"-style line from a transcript, or None if no
    numbers were found. Only weight+reps are taken per chunk — spoken set counts
    are dropped rather than guessed at."""
    if not text:
        return None
    lines: list[str] = []
    for chunk in _CHUNK_SPLIT_RE.split(text):
        nums = _chunk_to_numbers(chunk)
        if not nums:
            continue
        nums = nums[:2]  # weight, reps — ignore any trailing "three sets"
        if len(nums) == 1:
            lines.append(str(nums[0]))  # a lone number = bodyweight reps
        else:
            lines.append(f"{nums[0]} {nums[1]}")
    return ", ".join(lines) if lines else None
