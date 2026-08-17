"""Генерация картинок техники, где упражнение показывает НАШ тренер.

Зачем: `media/exercises` — 200 статичных фото из free-exercise-db (MIT). Они
лицензионно чистые и покрывают весь каталог, но это чужие люди в чужом стиле, а
`TONE_OF_VOICE.md` требует одного персонажа во всех картинках продукта. Этот
скрипт складывает вторую, свою пачку: тот же тренер с аватарки выполняет те же
упражнения. Старые фото остаются на месте и работают фолбэком для всего, что
ещё не сгенерировано.

Ручной работы здесь ровно два места, и оба разовые: посмотреть контактный лист
и выписать неудачные слаги. Всё остальное — описания поз, промпты, имена
файлов, повторы после падений — делает скрипт.

    # 1. описания поз для всего каталога (один дешёвый вызов текстовой модели
    #    на пачку, результат кэшируется в media/exercises/poses.json)
    python3 scripts/gen_exercise_images.py --poses

    # 2. посмотреть, что уйдёт в генератор, никуда не ходя и ничего не тратя
    python3 scripts/gen_exercise_images.py --dry-run --only barbell_bench_press_medium_grip

    # 3. собственно генерация — десятком за заход, уже сделанное пропускается
    python3 scripts/gen_exercise_images.py --limit 10

    # 4. контактный лист на просмотр: 20 картинок с подписями на одном полотне
    python3 scripts/gen_exercise_images.py --sheet

    # 5. перегенерить забракованное (слаги — по одному на строку в файле)
    python3 scripts/gen_exercise_images.py --only @rejects.txt --force

Бэкенды. `--backend openai` (по умолчанию) шлёт вместе с промптом ДВЕ
референс-картинки: аватарку тренера (кто это) и кадр из free-exercise-db (что
за поза) — именно так персонаж держится одинаковым от упражнения к упражнению,
а техника не выдумывается. `--backend xai` ходит в Грок, но его картиночный
эндпоинт (на момент написания) принимает только текст: персонаж будет плыть от
кадра к кадру, а поза — врать. Проверь текущее состояние API, прежде чем
выбирать xai: если он научился принимать картинки на вход, поправить нужно одну
функцию `_render_xai`.
"""

import argparse
import base64
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import exercise_media  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE_DIR = ROOT / "media" / "exercises"
OUT_DIR = SOURCE_DIR / "coach"
POSES_PATH = SOURCE_DIR / "poses.json"
COACH_REFERENCE = ROOT / "media" / "push" / "coach_incoming_call.jpg"

# ---------------------------------------------------------------- промпт

# Блок стиля — слово в слово раздел «Стиль картинок» из TONE_OF_VOICE.md. Он
# одинаковый в КАЖДОМ запросе и меняться не должен: персонаж держится ровно тем,
# что описание не переписывают от кадра к кадру.
STYLE = """\
Same character in every image, matching the attached character reference exactly:
a huge middle-aged bodybuilder, deep tan, heavy square jaw, short dark-blond hair
combed back, gold aviator sunglasses, thick gold chain, black tank top. Calm,
confident face - never shouting, never grinning, never winking. He is a coach,
not a mascot.

Rendering: bold black outline, flat fills with soft muscle highlights (cel
shading), western comic book / arcade cabinet art. No photorealism, no 3D render.
Detail goes into the muscles and the iron; the background stays simpler than the
figure.

Palette: warm amber lamp light and a sunset window against cold dark-grey iron,
red brick wall. Tan skin and gold are the only bright accents. A night basement
gym, not a white-lit fitness club."""

# Кадр — единственное осознанное отступление от гайда: в пушах тренер снят по
# пояс, а в демонстрации техники нужны всё тело и снаряд целиком. Если эту пачку
# принимаем в продукт, отступление стоит дописать в TONE_OF_VOICE.md, а не
# держать только здесь.
FRAME = """\
Full body in frame, three-quarter view from the side, camera at chest height, the
figure fills the frame with a small margin. The equipment (barbell, dumbbells or
machine) is fully visible and correctly positioned. Anatomically correct joint
angles and grip, matching the attached pose reference. Square 1:1 composition."""

BANS = """\
No readable text, letters, numbers or watermarks anywhere in the image. No logos
or brands. No other people - the coach trains alone. No mirrors showing a second
person. No glossy fitness models, no supplements. No blood, no grimaces, no
rust-and-gore "hardcore" styling."""

PROMPT = """\
{style}

{frame}

He is performing: {exercise}, {phase} position: {pose}.

{bans}"""

PHASE_WORD = {1: "start", 2: "end"}


def build_prompt(pose: dict, phase: int) -> str:
    """Полный промпт под один кадр. `phase` — 1 (начальная позиция) или 2 (конечная)."""
    return PROMPT.format(
        style=STYLE,
        frame=FRAME,
        exercise=pose["en"],
        phase=PHASE_WORD[phase],
        pose=pose["start"] if phase == 1 else pose["end"],
        bans=BANS,
    )


# ---------------------------------------------------------------- позы

# Затравка на восемь упражнений: она же образец формата для модели, когда та
# описывает оставшиеся девяносто с лишним. Восемь, а не два — на паре примеров
# модель копирует длину фразы, но не то, что в позе называют суставы и снаряд.
SEED_POSES = {
    "barbell_bench_press_medium_grip": {
        "en": "barbell bench press",
        "start": "lying flat on a bench, bar locked out at arms' length above the chest, feet planted",
        "end": "bar lowered to mid-chest, elbows at about 45 degrees to the body, shoulder blades pinned",
    },
    "barbell_squat": {
        "en": "barbell back squat",
        "start": "standing tall, bar resting on the upper back, feet shoulder-width apart",
        "end": "hips below knee level, chest up, knees tracking over the toes",
    },
    "barbell_deadlift": {
        "en": "conventional barbell deadlift",
        "start": "bar on the floor against the shins, hips high, back flat, arms straight",
        "end": "standing upright, bar at hip level, shoulders back, knees locked",
    },
    "pullups": {
        "en": "pull-ups",
        "start": "hanging from a bar at full stretch, overhand grip slightly wider than the shoulders",
        "end": "chin above the bar, elbows driven down to the ribs, chest to the bar",
    },
    "dumbbell_bicep_curl": {
        "en": "dumbbell biceps curl",
        "start": "standing, dumbbells at the sides, arms straight, palms facing forward",
        "end": "dumbbells curled to shoulder height, elbows pinned to the sides",
    },
    "leg_press": {
        "en": "machine leg press",
        "start": "seated in the sled, legs almost straight, feet shoulder-width on the platform",
        "end": "knees bent to about 90 degrees, platform lowered towards the chest, lower back on the pad",
    },
    "side_lateral_raise": {
        "en": "dumbbell lateral raise",
        "start": "standing, dumbbells hanging at the sides, slight bend in the elbows",
        "end": "arms raised to shoulder height, elbows slightly above the wrists",
    },
    "plank": {
        "en": "forearm plank",
        "start": "forearms and toes on the floor, body in a straight line from head to heels",
        "end": "same straight line held, abs and glutes braced, hips neither sagging nor piked",
    },
}

POSES_INSTRUCTION = """\
Ты помогаешь готовить промпты для генератора картинок с техникой упражнений.
На вход — русские названия упражнений из каталога силового бота. На каждое
верни JSON-объект:

  "<slug>": {"en": "...", "start": "...", "end": "..."}

- en: общепринятое английское название упражнения, без выдумок и без бренда.
- start / end: одна фраза про ПОЗУ в начальной и конечной точке повтора —
  что согнуто, где снаряд, куда смотрят локти/колени. По-английски, строчными,
  без точки в конце, не длиннее 120 символов.
- Изометрику (планка, вис) описывай двумя ракурсами одного удержания, а не
  выдумывай ей движение.

Отвечай ОДНИМ JSON-объектом со всеми слагами, без markdown и без пояснений."""

# Пачками по столько упражнений уходит на описание поз: пачка целиком влезает в
# один ответ, а инструкция с примерами (самая длинная часть запроса) при этом
# оплачивается один раз на двадцать упражнений, а не на каждое.
POSES_BATCH = 20


def load_poses() -> dict:
    if not POSES_PATH.exists():
        return dict(SEED_POSES)
    poses = json.loads(POSES_PATH.read_text(encoding="utf-8"))
    # Затравка выигрывает у кэша: это выверенные вручную образцы, и если модель
    # когда-то перезаписала их своими, вернуть нужно наши.
    poses.update(SEED_POSES)
    return poses


def save_poses(poses: dict) -> None:
    POSES_PATH.write_text(
        json.dumps(poses, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def generate_poses() -> None:
    """Дописать в poses.json всё, чего там ещё нет, одним вызовом на пачку."""
    from openai import OpenAI

    client = OpenAI(api_key=config.XAI_API_KEY, base_url=config.GROK_BASE_URL)
    poses = load_poses()
    missing = [(name, slug) for name, slug in exercise_media.EXERCISE_IMAGE_SLUGS.items()
               if slug not in poses]
    if not missing:
        print("Позы уже описаны для всего каталога")
        return

    example = json.dumps(dict(list(SEED_POSES.items())[:3]), ensure_ascii=False, indent=2)
    for i in range(0, len(missing), POSES_BATCH):
        batch = missing[i:i + POSES_BATCH]
        listing = "\n".join(f"{slug}: {name}" for name, slug in batch)
        response = client.chat.completions.create(
            model=config.GROK_MODEL,
            messages=[
                {"role": "system", "content": f"{POSES_INSTRUCTION}\n\nПример формата:\n{example}"},
                {"role": "user", "content": listing},
            ],
            response_format={"type": "json_object"},
        )
        chunk = json.loads(response.choices[0].message.content or "{}")
        poses.update({k: v for k, v in chunk.items() if _pose_is_sane(v)})
        save_poses(poses)  # после каждой пачки: обрыв на середине не теряет сделанное
        print(f"описано {min(i + POSES_BATCH, len(missing))}/{len(missing)}")


def _pose_is_sane(pose) -> bool:
    return (
        isinstance(pose, dict)
        and all(isinstance(pose.get(key), str) and pose[key].strip() for key in ("en", "start", "end"))
    )


# ---------------------------------------------------------------- генераторы


def _render_openai(prompt: str, references: list[pathlib.Path]) -> bytes:
    """gpt-image-1 через images.edit — единственный путь, где персонаж и поза
    задаются картинками, а не пересказом. Референсы: аватарка тренера и кадр
    из free-exercise-db (MIT — деривативы разрешены)."""
    from openai import OpenAI

    client = OpenAI(api_key=config.OPENAI_API_KEY)
    handles = [path.open("rb") for path in references]
    try:
        response = client.images.edit(
            model="gpt-image-1", image=handles, prompt=prompt, size="1024x1024",
        )
    finally:
        for handle in handles:
            handle.close()
    return base64.b64decode(response.data[0].b64_json)


def _render_xai(prompt: str, references: list[pathlib.Path]) -> bytes:
    """Грок. Референсы игнорируются — эндпоинт принимает только текст, поэтому
    персонаж держится одним описанием в промпте и будет плыть. Оставлено ради
    сравнения и на случай, если API научится принимать картинки."""
    from openai import OpenAI

    client = OpenAI(api_key=config.XAI_API_KEY, base_url=config.GROK_BASE_URL)
    response = client.images.generate(
        model="grok-2-image-1212", prompt=prompt, n=1, response_format="b64_json",
    )
    return base64.b64decode(response.data[0].b64_json)


BACKENDS = {"openai": _render_openai, "xai": _render_xai}


# ---------------------------------------------------------------- прогон


def target_path(slug: str, phase: int) -> pathlib.Path:
    return OUT_DIR / f"{slug}_{phase}.jpg"


def pose_reference(slug: str, phase: int) -> pathlib.Path | None:
    """Кадр из free-exercise-db под ту же фазу — он и есть образец позы."""
    path = SOURCE_DIR / f"{slug}_{phase}.jpg"
    return path if path.exists() else None


def resolve_slugs(only: str | None, poses: dict) -> list[str]:
    if only and only.startswith("@"):
        listing = pathlib.Path(only[1:]).read_text(encoding="utf-8")
        wanted = [line.strip() for line in listing.splitlines() if line.strip()]
    elif only:
        wanted = [part.strip() for part in only.split(",") if part.strip()]
    else:
        wanted = list(exercise_media.EXERCISE_IMAGE_SLUGS.values())
    unknown = [slug for slug in wanted if slug not in poses]
    if unknown:
        print(f"⚠️ нет описания позы, пропускаю: {', '.join(unknown)} (запусти --poses)")
    return [slug for slug in wanted if slug in poses]


def run(args) -> None:
    poses = load_poses()
    slugs = resolve_slugs(args.only, poses)
    render = BACKENDS[args.backend]
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    done = 0
    for slug in slugs:
        for phase in (1, 2):
            out = target_path(slug, phase)
            if out.exists() and not args.force:
                continue
            if args.limit and done >= args.limit:
                print(f"Дошёл до --limit {args.limit}, остальное — следующим заходом")
                return
            prompt = build_prompt(poses[slug], phase)
            if args.dry_run:
                print(f"\n{'=' * 70}\n{out.name}\n{'=' * 70}\n{prompt}")
                done += 1
                continue
            references = [COACH_REFERENCE]
            reference = pose_reference(slug, phase)
            if reference:
                references.append(reference)
            try:
                image = render(prompt, references)
            except Exception as e:  # noqa: BLE001 — падение одной картинки не рушит прогон
                print(f"✗ {out.name}: {e}")
                time.sleep(args.pause)
                continue
            out.write_bytes(image)
            done += 1
            print(f"✓ {out.name}")
            time.sleep(args.pause)
    print(f"\nГотово: {done} картинок в {OUT_DIR.relative_to(ROOT)}")


# ---------------------------------------------------------------- контактный лист

SHEET_COLUMNS = 4
SHEET_CELL = 320
SHEET_CAPTION = 22


def build_sheets() -> None:
    """Контактные листы на просмотр: две фазы рядом, слаг подписью.

    Просмотр — единственное узкое место всей затеи, и листать сотню файлов по
    одному дольше, чем их сгенерировать. С листа брак виден пачкой: выписал
    слаги в rejects.txt и перегнал их одной командой с --force.
    """
    from PIL import Image, ImageDraw

    files = sorted(OUT_DIR.glob("*_[12].jpg"))
    if not files:
        print("Пока нечего смотреть — сначала сгенерируй")
        return
    per_sheet = SHEET_COLUMNS * 5
    for sheet_no, start in enumerate(range(0, len(files), per_sheet), start=1):
        chunk = files[start:start + per_sheet]
        rows = (len(chunk) + SHEET_COLUMNS - 1) // SHEET_COLUMNS
        canvas = Image.new(
            "RGB", (SHEET_COLUMNS * SHEET_CELL, rows * (SHEET_CELL + SHEET_CAPTION)), "white"
        )
        draw = ImageDraw.Draw(canvas)
        for i, path in enumerate(chunk):
            tile = Image.open(path).convert("RGB").resize((SHEET_CELL, SHEET_CELL))
            x = (i % SHEET_COLUMNS) * SHEET_CELL
            y = (i // SHEET_COLUMNS) * (SHEET_CELL + SHEET_CAPTION)
            canvas.paste(tile, (x, y))
            draw.text((x + 4, y + SHEET_CELL + 4), path.stem, fill="black")
        out = OUT_DIR / f"_sheet_{sheet_no:02d}.jpg"
        canvas.save(out, quality=88)
        print(f"✓ {out.relative_to(ROOT)}")


# ---------------------------------------------------------------- CLI


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--poses", action="store_true", help="описать позы для всего каталога и выйти")
    parser.add_argument("--sheet", action="store_true", help="собрать контактные листы и выйти")
    parser.add_argument("--dry-run", action="store_true", help="печатать промпты, никуда не ходить")
    parser.add_argument("--only", help="слаги через запятую или @файл со списком")
    parser.add_argument("--limit", type=int, default=0, help="сколько картинок за заход (0 — без предела)")
    parser.add_argument("--force", action="store_true", help="перерисовывать уже существующие")
    parser.add_argument("--backend", choices=sorted(BACKENDS), default="openai")
    parser.add_argument("--pause", type=float, default=2.0, help="пауза между запросами, секунды")
    args = parser.parse_args()

    if args.poses:
        generate_poses()
        return
    if args.sheet:
        build_sheets()
        return
    run(args)


if __name__ == "__main__":
    main()
