"""Regenerate the trigger/text table in PUSH_IDEAS.md from push_texts.py.

Run after changing anything in push_texts.py (new variant, new category,
reworded line) so the doc never drifts from what the bot actually sends:

    python3 scripts/gen_push_table.py

Splices the generated table between the `<!-- PUSH_TABLE_START -->` and
`<!-- PUSH_TABLE_END -->` markers in PUSH_IDEAS.md, leaving the rest of the
doc untouched.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import push_texts  # noqa: E402

DOC_PATH = pathlib.Path(__file__).resolve().parent.parent / "PUSH_IDEAS.md"
START_MARKER = "<!-- PUSH_TABLE_START -->"
END_MARKER = "<!-- PUSH_TABLE_END -->"

# Trigger descriptions per category — kept here (not in push_texts.py) since
# they describe engagement.py's orchestration logic, not the copy itself.
TRIGGERS = {
    push_texts.STREAK_AT_RISK: "Сб/вс, `week_streak >= 2`, тренировок на этой неделе — 0",
    push_texts.SKIP: "Ровно N дней с последней тренировки, N ∈ {3, 5, 7, 10, 14}",
    push_texts.WIN_BACK: "`days_since_last >= 21`, затем каждые 10 дней (21, 31, 41…)",
    push_texts.TIMING: (
        "Сегодня — самый частый день тренировок по истории (нужно ≥10 тренировок), "
        "сегодня ещё не тренировался"
    ),
    push_texts.PLATEAU: "Вс: тот же рабочий вес 3 тренировки подряд, каждый раз 12+ повторов",
    push_texts.WEEKLY_DIGEST: "Вс, нет активного плато, суммарный тоннаж за 30 дней > 0",
    push_texts.CHALLENGE: "Пн (старт) или чт при < 2 тренировок за неделю (прогресс)",
    push_texts.FOLLOWUP: (
        "Через 2 ч после завершения живой (не задним числом) тренировки — "
        "транзакционное, не конкурирует за дневной слот"
    ),
}

# engagement.build_daily_push()'s actual evaluation order.
ORDER = [
    push_texts.STREAK_AT_RISK,
    push_texts.SKIP,
    push_texts.WIN_BACK,
    push_texts.TIMING,
    push_texts.PLATEAU,
    push_texts.WEEKLY_DIGEST,
    push_texts.CHALLENGE,
    push_texts.FOLLOWUP,
]

# Stand-ins for {placeholder} templates so the table shows readable examples.
PLACEHOLDER_EXAMPLES = {
    "{weeks}": "6",
    "{days_left}": "последний день",
    "{exercise}": "Жим лёжа",
    "{tonnage}": "4.2 т",
    "{week_count}": "2 тренировки",
}


def render_example(template: str) -> str:
    text = template
    for placeholder, value in PLACEHOLDER_EXAMPLES.items():
        text = text.replace(placeholder, value)
    return text.replace("|", "\\|")


def build_table() -> str:
    lines = [
        "| Ранг | Категория | Триггер | Вариант | Текст пуша (пример) |",
        "|---|---|---|---|---|",
    ]
    for rank, category in enumerate(ORDER, start=1):
        rank_label = str(rank) if category != push_texts.FOLLOWUP else "—"
        label = push_texts.CATEGORY_LABELS[category]
        trigger = TRIGGERS[category]
        for i, template in enumerate(push_texts.TEXTS[category], start=1):
            cat_cell = f"**{label}**" if i == 1 else ""
            trig_cell = trigger if i == 1 else ""
            rank_cell = rank_label if i == 1 else ""
            lines.append(f"| {rank_cell} | {cat_cell} | {trig_cell} | {i} | {render_example(template)} |")
    return "\n".join(lines)


def main() -> None:
    doc = DOC_PATH.read_text(encoding="utf-8")
    if START_MARKER not in doc or END_MARKER not in doc:
        raise SystemExit(f"Markers not found in {DOC_PATH} — did the doc structure change?")

    before, rest = doc.split(START_MARKER, 1)
    _, after = rest.split(END_MARKER, 1)
    doc = f"{before}{START_MARKER}\n{build_table()}\n{END_MARKER}{after}"

    DOC_PATH.write_text(doc, encoding="utf-8")
    print(f"Updated {DOC_PATH}")


if __name__ == "__main__":
    main()
