"""Кастомные видео-демонстрации упражнений: генерация клипов нашим тренером.

Запускается руками и локально — в бот этот файл не попадает, в бот попадает
только результат: `media/exercises/<slug>_demo.mp4`. Бот сам ничего не
генерирует, он отдаёт готовый файл (exercise_media.get_animation).

Зачем вообще: в карточках упражнений сейчас живёт случайный человек из
открытой базы — единственное место в продукте, где вместо нашего тренера
кто-то другой (TONE_OF_VOICE.md, «Стиль картинок»: персонаж один на весь
продукт).

Как устроено — четыре стадии, каждая запускается отдельно:

    frames   готовые фото старт/конец → те же позы, но нашим тренером
    video    два кадра → клип движения между ними
    loop     клип → бесшовный повтор вниз-вверх, 480p, без звука (ffmpeg)
    install  принятый клип → media/exercises/<slug>_demo.mp4

Разделение не для красоты. Кадры дешёвые, и их не жалко перегенерить, пока
поза не станет честной; видео дорогое, и гнать его по кривому кадру — это
платить за заведомый брак. Поэтому кадры проверяются глазами до `video`, а
клип — до `install`.

Позы берутся с существующих фото (image-to-image), а не выдумываются по
тексту: биомеханику генератор врёт уверенно и красиво, а демонстрация с
неправильной техникой хуже честного стокового фото. С фото приходит поза,
из промпта — только персонаж и рисовка.

Примеры:

    python scripts/gen_exercise_demos.py status
    python scripts/gen_exercise_demos.py frames --exercise "Присед со штангой"
    python scripts/gen_exercise_demos.py video  --exercise "Присед со штангой"
    python scripts/gen_exercise_demos.py loop   --exercise "Присед со штангой"
    python scripts/gen_exercise_demos.py install --exercise "Присед со штангой"

    python scripts/gen_exercise_demos.py pilot --dry-run   # пять упражнений, без трат
    python scripts/gen_exercise_demos.py pilot

Нужно из окружения:

    NOVITA_API_KEY   тот же ключ, что уже разбирает технику по видео
                     (config.NOVITA_API_KEY, video_analysis.py)
    ffmpeg           только для стадии loop

Имена моделей и пути эндпоинтов вынесены в NOVITA_* ниже и переопределяются
переменными окружения: каталог у провайдера меняется быстрее, чем этот файл,
и упереться в «нет такой модели» не должно значить «правь скрипт».
"""

import argparse
import base64
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import exercise_media  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
MEDIA_DIR = pathlib.Path(exercise_media.MEDIA_DIR)
# Черновики лежат отдельно от готовых ассетов и не коммитятся: на принятый
# клип приходится несколько отбракованных дублей, и им в репозитории нечего
# делать. В media/exercises переезжает только то, что прошло стадию install.
WORK_DIR = MEDIA_DIR / "_demo_work"

# Пилот: пять разных механик — ноги, жим, тяга, изоляция, статика. Если
# генератор врёт, врать он будет по-разному, и на однотипной пятёрке это
# не вылезет.
PILOT = [
    "Присед со штангой",
    "Жим штанги лёжа",
    "Тяга верхнего блока",
    "Подъём штанги на бицепс",
    "Планка",
]

# --- Провайдер (Novita, async-задачи) ---------------------------------------
#
# Точки входа: POST /v3/async/<path> отдаёт task_id, GET /v3/async/task-result
# по нему возвращает статус и ссылки на готовые файлы. Схемы полей у моделей
# отличаются, поэтому payload собирается в одном месте (_frame_payload,
# _video_payload) — при расхождении с доками правится там, а не по всему файлу.
NOVITA_BASE = os.getenv("NOVITA_GEN_BASE_URL", "https://api.novita.ai/v3/async")
NOVITA_FRAME_PATH = os.getenv("NOVITA_FRAME_PATH", "img2img")
NOVITA_VIDEO_PATH = os.getenv("NOVITA_VIDEO_PATH", "vidu-q1-startend2video")
NOVITA_FRAME_MODEL = os.getenv("NOVITA_FRAME_MODEL", "")
POLL_TIMEOUT_S = int(os.getenv("NOVITA_POLL_TIMEOUT", "600"))

# Персонаж — дословно по TONE_OF_VOICE.md, раздел «Стиль картинок». Меняется
# только вместе с ним: разъедутся — в карточках заведётся второй тренер.
CHARACTER = (
    "huge middle-aged bodybuilder, deep tan, heavy square jaw, short dark-blond "
    "hair combed back, gold aviator sunglasses, thick gold chain, black tank top; "
    "calm confident face, not shouting, not grinning, not winking. "
    "Bold black outline, flat fills with soft muscle shading (cel shading), "
    "no photorealism, no 3D render — western comic and arcade-cover look. "
    "Warm amber lamp light and sunset in the window against cold dark-grey iron, "
    "red brick wall; tan and gold are the only bright spots. Night basement gym: "
    "brick, dumbbell rack, mirror, lamp on a wire. He is alone in the gym. "
    "No text, no letters, no logos, no watermarks, no other people, "
    "no glossy fitness models, no blood, no grimaces."
)
POSE_INSTRUCTION = (
    "Redraw this exact photo as the character described, keeping the body pose, "
    "camera angle, limb positions and equipment exactly as in the source image. "
    "The pose is the source of truth — do not correct, improve or restyle it."
)


class GenError(RuntimeError):
    """Провайдер ответил не тем, чего мы ждали. Текст ответа внутри."""


def _api_key() -> str:
    key = os.getenv("NOVITA_API_KEY", "")
    if not key:
        sys.exit(
            "Нет NOVITA_API_KEY в окружении. Это тот же ключ, которым бот уже "
            "разбирает технику по видео. Прогон без трат: добавь --dry-run."
        )
    return key


def _request(url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": "application/json",
        },
        method="POST" if data else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # Тело ответа дословно: при расхождении с доками там лежит имя поля,
        # которое не сошлось, и это разница между «правка на минуту» и
        # «непонятная 400».
        raise GenError(f"HTTP {e.code} от {url}\n{e.read().decode(errors='replace')}") from e


def _start_task(path: str, payload: dict) -> str:
    body = _request(f"{NOVITA_BASE}/{path}", payload)
    task_id = body.get("task_id") or (body.get("task") or {}).get("task_id")
    if not task_id:
        raise GenError(f"Ответ без task_id: {json.dumps(body, ensure_ascii=False)[:400]}")
    return task_id


def _wait_for_task(task_id: str) -> list[str]:
    """Ждёт задачу и возвращает ссылки на готовые файлы."""
    deadline = time.monotonic() + POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        body = _request(f"{NOVITA_BASE}/task-result?task_id={task_id}")
        task = body.get("task") or {}
        status = task.get("status", "")
        if status.endswith("SUCCEED"):
            urls = [
                item["video_url"] if "video_url" in item else item.get("image_url")
                for item in (body.get("videos") or body.get("images") or [])
            ]
            urls = [u for u in urls if u]
            if not urls:
                raise GenError(f"Задача {task_id} готова, но файлов нет: {body}")
            return urls
        if status.endswith("FAILED"):
            raise GenError(f"Задача {task_id} упала: {task.get('reason') or body}")
        print(f"    … {status or 'ждём'}")
        time.sleep(5)
    raise GenError(f"Задача {task_id} не уложилась в {POLL_TIMEOUT_S} с")


def _download(url: str, dest: pathlib.Path) -> None:
    with urllib.request.urlopen(url, timeout=300) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


def _b64(path: pathlib.Path) -> str:
    return base64.b64encode(path.read_bytes()).decode()


def _frame_payload(source: pathlib.Path) -> dict:
    payload = {
        "request": {
            "image_base64": _b64(source),
            "prompt": f"{POSE_INSTRUCTION} {CHARACTER}",
            "image_num": 1,
            "steps": 30,
            "strength": 0.55,
        }
    }
    if NOVITA_FRAME_MODEL:
        payload["request"]["model_name"] = NOVITA_FRAME_MODEL
    return payload


def _video_payload(start: pathlib.Path, end: pathlib.Path) -> dict:
    return dict(
        _video_payload_body(),
        images=[f"data:image/jpeg;base64,{_b64(start)}", f"data:image/jpeg;base64,{_b64(end)}"],
    )


def _video_payload_body() -> dict:
    """Всё, кроме самих кадров — чтобы сухой прогон показывал запрос, не
    требуя файлов, которых на этой стадии ещё нет."""
    return {
        "prompt": (
            "The lifter performs one slow controlled repetition from the first "
            "frame to the second. Static camera, no cuts, no zoom. "
            "The equipment, the weight plates and the gym stay identical."
        ),
        "duration": 4,
    }


# --- Стадии -----------------------------------------------------------------


def _slug(exercise: str) -> str:
    slug = exercise_media.EXERCISE_IMAGE_SLUGS.get(exercise)
    if slug is None:
        sys.exit(
            f"Упражнения {exercise!r} нет в EXERCISE_IMAGE_SLUGS. "
            "Список того, что есть: status."
        )
    return slug


def _work(slug: str, create: bool = True) -> pathlib.Path:
    """Каталог черновиков упражнения. create=False для сухого прогона: он не
    должен оставлять после себя пустых папок, которые status потом покажет
    как «в работе»."""
    path = WORK_DIR / slug
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def cmd_frames(exercise: str, dry_run: bool, force: bool) -> None:
    slug = _slug(exercise)
    work = _work(slug, create=not dry_run)
    sources = [MEDIA_DIR / f"{slug}_1.jpg", MEDIA_DIR / f"{slug}_2.jpg"]
    for source, name in zip(sources, ("start.jpg", "end.jpg"), strict=True):
        dest = work / name
        if dest.exists() and not force:
            print(f"  {name}: уже есть, пропускаю (--force чтобы перегенерить)")
            continue
        print(f"  {name}: {source.name} → тренер")
        if dry_run:
            payload = _frame_payload(source)
            # Промпт персонажа один на все упражнения и длинный — в сухом
            # прогоне на пять упражнений он занял бы весь экран десять раз.
            payload["request"] = dict(
                payload["request"],
                image_base64=f"<{source.name} base64>",
                prompt=f"{POSE_INSTRUCTION[:60]}… + CHARACTER ({len(CHARACTER)} симв.)",
            )
            print(f"    POST {NOVITA_BASE}/{NOVITA_FRAME_PATH}")
            print(f"    {json.dumps(payload, ensure_ascii=False)}")
            continue
        task_id = _start_task(NOVITA_FRAME_PATH, _frame_payload(source))
        _download(_wait_for_task(task_id)[0], dest)
        print(f"    готово: {dest}")
    if not dry_run:
        print(f"  Посмотри оба кадра глазами: {work}")
        print("  Поза кривая — перегенери: frames --force. Всё честно — гони video.")


def cmd_video(exercise: str, dry_run: bool, force: bool) -> None:
    slug = _slug(exercise)
    work = _work(slug, create=not dry_run)
    start, end, raw = work / "start.jpg", work / "end.jpg", work / "raw.mp4"
    print(f"  {slug}: два кадра → клип")
    if dry_run:
        # Кадров на диске в сухом прогоне ещё нет — показываем запрос и идём
        # дальше, иначе `pilot --dry-run` обрывался бы на первой же стадии и
        # не показывал бы того, ради чего его и запускают: всю цепочку.
        print(f"    POST {NOVITA_BASE}/{NOVITA_VIDEO_PATH}")
        payload = dict(
            _video_payload_body(), images=["<start.jpg base64>", "<end.jpg base64>"]
        )
        print(f"    {json.dumps(payload, ensure_ascii=False)}")
        return
    if not (start.exists() and end.exists()):
        raise GenError(f"Сначала кадры: frames --exercise {exercise!r}")
    if raw.exists() and not force:
        print("  raw.mp4 уже есть, пропускаю (--force чтобы перегенерить)")
        return
    task_id = _start_task(NOVITA_VIDEO_PATH, _video_payload(start, end))
    _download(_wait_for_task(task_id)[0], raw)
    print(f"    готово: {raw}")


def cmd_loop(exercise: str, dry_run: bool, force: bool) -> None:
    """Клип + он же задом наперёд = повтор вниз-вверх, который сходится сам.

    Петля из одного прохода всегда стыкуется рывком: последний кадр не равен
    первому. Разворот эту стыковку убирает по построению и заодно вдвое
    удлиняет клип бесплатно.
    """
    slug = _slug(exercise)
    work = _work(slug, create=not dry_run)
    raw, loop = work / "raw.mp4", work / "loop.mp4"
    cmd = [
        "ffmpeg", "-y", "-i", str(raw),
        "-filter_complex",
        "[0:v]scale=480:-2,fps=24,split[a][b];[b]reverse[r];[a][r]concat=n=2:v=1[out]",
        "-map", "[out]",
        "-an",  # Telegram крутит анимацию без звука, дорожка была бы мёртвым весом
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(loop),
    ]
    print(f"  {slug}: петля вниз-вверх")
    if dry_run:
        print(f"    {' '.join(cmd)}")
        return
    if not raw.exists():
        raise GenError(f"Сначала клип: video --exercise {exercise!r}")
    if loop.exists() and not force:
        print("  loop.mp4 уже есть, пропускаю (--force чтобы пересобрать)")
        return
    if shutil.which("ffmpeg") is None:
        sys.exit("Нет ffmpeg — поставь его, эта стадия только на нём.")
    done = subprocess.run(cmd, capture_output=True)
    if done.returncode:
        raise GenError(f"ffmpeg упал:\n{done.stderr.decode(errors='replace')[-800:]}")
    print(f"    готово: {loop} ({loop.stat().st_size // 1024} КБ)")


def cmd_install(exercise: str, dry_run: bool, force: bool) -> None:
    slug = _slug(exercise)
    loop = _work(slug, create=not dry_run) / "loop.mp4"
    dest = MEDIA_DIR / f"{slug}_demo.mp4"
    if dry_run:
        print(f"  {loop} → {dest}")
        return
    if not loop.exists():
        raise GenError(f"Сначала петля: loop --exercise {exercise!r}")
    if dest.exists() and not force:
        print(f"  {dest.name} уже стоит, пропускаю (--force чтобы заменить)")
        return
    size_kb = loop.stat().st_size // 1024
    if size_kb > 1024:
        print(f"  ⚠️  {size_kb} КБ — тяжеловато для автоплея в ленте, но ставлю")
    print(f"  {loop} → {dest}")
    if not dry_run:
        shutil.copyfile(loop, dest)
        print("    готово. Клип подхватится сам, перегенерации ассетов не надо.")


def cmd_status(*_args, **_kwargs) -> None:
    ready, drafts = [], []
    for exercise, slug in sorted(exercise_media.EXERCISE_IMAGE_SLUGS.items()):
        if (MEDIA_DIR / f"{slug}_demo.mp4").exists():
            ready.append(exercise)
        elif (WORK_DIR / slug).exists():
            drafts.append(exercise)
    total = len(exercise_media.EXERCISE_IMAGE_SLUGS)
    print(f"Готовых клипов: {len(ready)} из {total}")
    for exercise in ready:
        print(f"  ✅ {exercise}")
    if drafts:
        print(f"В работе (есть черновики, клип не поставлен): {len(drafts)}")
        for exercise in drafts:
            print(f"  🚧 {exercise}")
    print(f"Остальные {total - len(ready)} показывают прежнюю пару фото.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Генерация видео-демонстраций упражнений нашим тренером.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "stage", choices=["frames", "video", "loop", "install", "status", "pilot"]
    )
    parser.add_argument("--exercise", help="название упражнения ровно как в шаблонах")
    parser.add_argument("--all", action="store_true", help="все упражнения с фото")
    parser.add_argument(
        "--dry-run", action="store_true", help="показать запросы, ничего не тратить"
    )
    parser.add_argument("--force", action="store_true", help="перезаписать готовое")
    args = parser.parse_args()

    if args.stage == "status":
        cmd_status()
        return

    stages = {"frames": cmd_frames, "video": cmd_video, "loop": cmd_loop, "install": cmd_install}
    if args.stage == "pilot":
        targets, chain = PILOT, list(stages.values())
    elif args.all:
        targets, chain = sorted(exercise_media.EXERCISE_IMAGE_SLUGS), [stages[args.stage]]
    elif args.exercise:
        targets, chain = [args.exercise], [stages[args.stage]]
    else:
        parser.error("нужен --exercise, --all или стадия pilot")

    failed = []
    for exercise in targets:
        print(f"\n=== {exercise}")
        for stage in chain:
            try:
                stage(exercise, args.dry_run, args.force)
            except (GenError, subprocess.CalledProcessError) as e:
                # Одно упавшее упражнение не должно ронять весь прогон: на
                # длинном --all это значит «потерять всё из-за одной задачи».
                print(f"  ❌ {e}")
                failed.append(exercise)
                break
    if failed:
        print(f"\nНе получилось: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
