"""Офлайн-прогон разбора видео: папка роликов + ожидания → таблица и доля «сдал».

Зачем. Разбор техники проверяется только живыми роликами: юнит-тесты держат
чистку и протокол, но не глаза модели. Этот скрипт гоняет один и тот же набор
роликов через оба пути (`openai` — свои кадры в модель зрения тренера,
`novita` — ролик целиком в Qwen) и сверяет с тем, что человек знает про ролик
наверняка: упражнение, число повторов, какие точки обязаны быть «норма» или
«отклонение», каких слов в разборе быть не должно.

Ожидания — JSON (или YAML, если установлен pyyaml), пример:
docs/video_eval_expectations.example.json. Поля клипа (все, кроме file,
необязательны):

    file              имя файла в папке роликов
    hint              подпись атлета к ролику (как в чате)
    exercise_any      хотя бы одна из подстрок есть в распознанном упражнении
    reps              столько повторов видно (reps_seen)
    checkpoints       {"траектория снаряда": "норма", ...} — вердикты ПОСЛЕ чистки
    max_observations  не больше стольких наблюдений
    forbid_words      подстрок нет ни в наблюдениях, ни в описаниях точек

Ключи берутся из окружения ровно как у бота (config: OPENAI_API_KEY,
NOVITA_API_KEY) и никуда не печатаются — печатается только «есть / нет».
Вызовы платные. Цена каждого прогона печатается по той же формуле, что у
дневного отчёта (config.call_price_usd), но в cost_events НЕ пишется: это
прогон разработчика, а не расход атлетов.

Локально:

    python scripts/video_eval.py ~/clips docs/video_eval_expectations.example.json

На сервере (Fly), где ключи уже в окружении:

    fly ssh sftp shell -a training-log-bot     # put clip.mp4 /data/video_eval/clip.mp4 …
    fly ssh console -a training-log-bot
    cd /app && python scripts/video_eval.py /data/video_eval /data/video_eval/expect.json

Флаги: --backends openai,novita (по умолчанию оба, что настроены),
--verbose — сырые строки модели и чистки из лога, --json-out файл — всё
разобранное целиком для разбора глазами.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import mimetypes
import pathlib
import sys
import time
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import db  # noqa: E402
import video_analysis  # noqa: E402


def load_expectations(path: pathlib.Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        import yaml  # только если ожидания в YAML

        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    clips = data.get("clips") if isinstance(data, dict) else data
    if not isinstance(clips, list) or not all(isinstance(c, dict) and c.get("file") for c in clips):
        raise SystemExit(f"{path}: нужен список clips с полем file у каждого")
    return clips


def check(expect: dict[str, Any], result: dict[str, Any] | None) -> list[str]:
    """Что не сошлось (пустой список — сдал)."""
    if result is None:
        return ["разбора нет (None)"]
    fails = []
    exercise = str(result.get("exercise") or "").lower()
    wanted = expect.get("exercise_any")
    if wanted and not any(w.lower() in exercise for w in wanted):
        fails.append(f"упражнение {exercise!r}, ждали одно из {wanted}")
    if "reps" in expect and result.get("reps_seen") != expect["reps"]:
        fails.append(f"повторов {result.get('reps_seen')}, ждали {expect['reps']}")
    verdicts = {item["point"]: item["verdict"] for item in result.get("checklist") or []}
    for point, verdict in (expect.get("checkpoints") or {}).items():
        if verdicts.get(point) != verdict:
            fails.append(f"«{point}»: {verdicts.get(point)}, ждали {verdict}")
    observations = result.get("observations") or []
    if "max_observations" in expect and len(observations) > expect["max_observations"]:
        fails.append(f"наблюдений {len(observations)} > {expect['max_observations']}")
    text = " ".join(
        [f"{o.get('what')} {o.get('evidence')}" for o in observations]
        + [str(i.get("what_i_see")) for i in result.get("checklist") or []]
    ).lower()
    for word in expect.get("forbid_words") or []:
        if word.lower() in text:
            fails.append(f"в разборе есть «{word}»")
    return fails


class CostMeter:
    """Перехват cost-событий: цена прогона на экран, а не в базу атлетов."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def log_cost_event(self, user_id, event_type, *, model=None, prompt_tokens=0,
                             completion_tokens=0, cached_tokens=0, reasoning_tokens=0, **_kw) -> None:
        self.events.append({
            "model": model, "prompt": prompt_tokens, "completion": completion_tokens,
            "cached": cached_tokens, "reasoning": reasoning_tokens,
            "usd": config.call_price_usd(model or "", prompt_tokens, completion_tokens,
                                         cached_tokens, reasoning_tokens),
        })

    def take(self) -> dict[str, Any]:
        events, self.events = self.events, []
        return {
            "prompt": sum(e["prompt"] for e in events),
            "completion": sum(e["completion"] for e in events),
            "reasoning": sum(e["reasoning"] for e in events),
            "usd": sum(e["usd"] for e in events),
            "model": ",".join(sorted({str(e["model"]) for e in events})),
        }


def available_backends() -> list[str]:
    have = []
    if config.OPENAI_API_KEY:
        have.append("openai")
    if config.NOVITA_API_KEY:
        have.append("novita")
    return have


async def run(clips_dir: pathlib.Path, clips: list[dict[str, Any]], backends: list[str],
              json_out: pathlib.Path | None) -> int:
    meter = CostMeter()
    db.log_cost_event = meter.log_cost_event  # type: ignore[assignment]
    db.is_open = lambda: True  # type: ignore[assignment]
    rows, dump = [], []
    for clip in clips:
        path = clips_dir / clip["file"]
        if not path.is_file():
            print(f"!! нет файла {path}", file=sys.stderr)
            continue
        data = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "video/mp4"
        for backend in backends:
            started = time.monotonic()
            try:
                result = await video_analysis.analyze(
                    data, None, mime, exercise_hint=clip.get("hint"), backend=backend
                )
            except Exception as exc:  # прогон целиком не роняем из-за одного ролика
                result = None
                print(f"!! {clip['file']} / {backend}: {type(exc).__name__}: {exc}", file=sys.stderr)
            seconds = time.monotonic() - started
            cost = meter.take()
            fails = check(clip, result)
            rows.append((clip["file"], backend, result, fails, cost, seconds))
            dump.append({"file": clip["file"], "backend": backend, "fails": fails,
                         "cost": cost, "seconds": round(seconds, 1), "result": result})

    header = f"{'клип':28} {'путь':7} {'упражнение':24} {'повт':>4} {'откл':>4} {'вход':>6} {'выход':>6} {'$':>8} {'сек':>5}  итог"
    print(header)
    print("-" * len(header))
    for file, backend, result, fails, cost, seconds in rows:
        res = result or {}
        flagged = sum(1 for i in res.get("checklist") or [] if i.get("verdict") == "отклонение")
        print(
            f"{file[:28]:28} {backend:7} {str(res.get('exercise') or '—')[:24]:24} "
            f"{str(res.get('reps_seen') if result else '—'):>4} {flagged:>4} "
            f"{cost['prompt']:>6} {cost['completion']:>6} {cost['usd']:>8.4f} {seconds:>5.0f}  "
            + ("ok" if not fails else "FAIL: " + "; ".join(fails))
        )
    print()
    for backend in backends:
        mine = [r for r in rows if r[1] == backend]
        if not mine:
            continue
        passed = sum(1 for r in mine if not r[3])
        spent = sum(r[4]["usd"] for r in mine)
        print(f"{backend}: сдал {passed}/{len(mine)} ({passed / len(mine):.0%}), "
              f"потрачено ${spent:.4f}, в среднем {sum(r[5] for r in mine) / len(mine):.0f} с")
    if json_out:
        json_out.write_text(json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nцеликом: {json_out}")
    return 0 if rows and all(not r[3] for r in rows) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("clips_dir", type=pathlib.Path)
    parser.add_argument("expectations", type=pathlib.Path)
    parser.add_argument("--backends", default="", help="openai,novita (по умолчанию — все настроенные)")
    parser.add_argument("--json-out", type=pathlib.Path)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    have = available_backends()
    print("ключи в окружении: " + ", ".join(
        f"{name} {'есть' if name in have else 'нет'}" for name in ("openai", "novita")
    ))
    wanted = [b.strip() for b in args.backends.split(",") if b.strip()] or have
    missing = [b for b in wanted if b not in have]
    if missing:
        raise SystemExit(f"нет ключа для {missing}")
    if not wanted:
        raise SystemExit("ни одного настроенного пути")
    return asyncio.run(run(args.clips_dir, load_expectations(args.expectations), wanted, args.json_out))


if __name__ == "__main__":
    sys.exit(main())
