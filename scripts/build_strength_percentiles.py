"""Таблица перцентилей для `/tools/strength-percentile` (seo_tools.percentile).

Источник — открытые данные OpenPowerlifting (opl-data, общественное
достояние: https://gitlab.com/openpowerlifting/opl-data), протоколы
соревнований USAPL: файлы `meet-data/usapl/<соревнование>/entries.csv`.
Те же 400 соревнований, по которым посчитаны доли упражнений в сумме
(`seo_tools.LIFT_SHARE`).

Кого берём: классика (Equipment = Raw), полное троеборье (Event = SBD),
засчитанное выступление (Place — число: без DQ, DD, NS и гостей G), вес
тела указан. Один атлет — одна строка на клетку (лучший результат), чтобы
тот, кто выступал двадцать раз, не весил в таблице в двадцать раз больше.

Клетка — пол × весовая категория IPF × упражнение (присед, жим, тяга,
сумма). В JSON — 21 квантиль (1, 5, 10, …, 95, 99-й) и число атлетов.

Запуск (по каталогу с entries.csv; имена файлов любые, нужны колонки
протокола OpenPowerlifting):

    git clone --depth 1 https://gitlab.com/openpowerlifting/opl-data
    python scripts/build_strength_percentiles.py opl-data/meet-data/usapl/*/entries.csv

или по сохранённым копиям: `python scripts/build_strength_percentiles.py <каталог>/*.csv`.
Пишет `seo_data/strength_percentiles.json`; дату сборки и число
соревнований кладёт туда же — страница называет их рядом с результатом.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys

CLASSES = {"m": (59, 66, 74, 83, 93, 105, 120), "f": (47, 52, 57, 63, 69, 76, 84)}
QUANTILES = (1, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 99)
LIFTS = {"squat": "Best3SquatKg", "bench": "Best3BenchKg", "deadlift": "Best3DeadliftKg", "total": "TotalKg"}
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "seo_data", "strength_percentiles.json")


def weight_class(sex: str, bodyweight: float) -> str:
    for limit in CLASSES[sex]:
        if bodyweight <= limit:
            return str(limit)
    return f"{CLASSES[sex][-1]}+"


def quantile(sorted_values: list[float], q: float) -> float:
    """Линейная интерполяция между соседними (как numpy, method="linear")."""
    pos = (len(sorted_values) - 1) * q / 100
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def build(paths: list[str]) -> dict:
    best: dict[tuple[str, str, str], dict[str, float]] = {}
    meets = 0
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            meets += 1
            for row in csv.DictReader(fh):
                sex = {"M": "m", "F": "f"}.get(row.get("Sex", ""))
                if not sex or row.get("Equipment") != "Raw" or row.get("Event") != "SBD":
                    continue
                if not (row.get("Place") or "").isdigit():
                    continue
                try:
                    bodyweight = float(row["BodyweightKg"])
                except (KeyError, ValueError):
                    continue
                cls = weight_class(sex, bodyweight)
                for lift, column in LIFTS.items():
                    try:
                        value = float(row.get(column) or 0)
                    except ValueError:
                        continue
                    if value <= 0:
                        continue
                    cell = best.setdefault((sex, cls, lift), {})
                    name = row.get("Name", "")
                    cell[name] = max(cell.get(name, 0.0), value)
    table: dict[str, dict[str, dict[str, dict]]] = {}
    for (sex, cls, lift), by_name in sorted(best.items()):
        values = sorted(by_name.values())
        if len(values) < 30:
            continue
        table.setdefault(sex, {}).setdefault(cls, {})[lift] = {
            "n": len(values), "q": [round(quantile(values, q), 1) for q in QUANTILES],
        }
    return {
        "source": "OpenPowerlifting opl-data (public domain), USAPL meets, Raw SBD",
        "source_url": "https://gitlab.com/openpowerlifting/opl-data",
        "meets": meets,
        "built": dt.date.today().isoformat(),
        "quantiles": list(QUANTILES),
        "classes": {k: [str(x) for x in v] + [f"{v[-1]}+"] for k, v in CLASSES.items()},
        "table": table,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("files", nargs="+", help="entries.csv протоколов OpenPowerlifting")
    parser.add_argument("--out", default=OUT)
    args = parser.parse_args()
    data = build(sorted(args.files))
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write("\n")
    cells = sum(len(lifts) for classes in data["table"].values() for lifts in classes.values())
    print(f"{args.out}: {data['meets']} meets, {cells} cells", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
