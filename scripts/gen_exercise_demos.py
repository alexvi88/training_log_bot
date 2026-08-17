"""Кастомные видео-демонстрации упражнений: генерация клипов нашим тренером.

Запускается руками и локально — в бот этот файл не попадает, в бот попадает
только результат: `media/exercises/<slug>_demo.mp4`. Бот сам ничего не
генерирует, он отдаёт готовый файл (exercise_media.get_animation).

Зачем вообще: в карточках упражнений сейчас живёт случайный человек из
открытой базы — единственное место в продукте, где вместо нашего тренера
кто-то другой (TONE_OF_VOICE.md, «Стиль картинок»: персонаж один на весь
продукт).

Как устроено — четыре стадии, каждая запускается отдельно:

    frames   эталон тренера + готовые фото старт/конец → те же позы, но им
             (позу с фото сперва читает vision-модель, кэш — exercise_poses.json)
    video    два кадра → клип движения между ними
    loop     клип → бесшовный повтор вниз-вверх, 480p, без звука (ffmpeg)
    install  принятый клип → media/exercises/<slug>_demo.mp4

Разделение не для красоты. Кадры дешёвые, и их не жалко перегенерить, пока
поза не станет честной; видео дорогое, и гнать его по кривому кадру — это
платить за заведомый брак. Поэтому кадры проверяются глазами до `video`, а
клип — до `install`.

Позы берутся с существующих фото, а не выдумываются по тексту: биомеханику
генератор врёт уверенно и красиво, а демонстрация с неправильной техникой хуже
честного стокового фото.

Но одной картинки мало — первый живой прогон это показал. Исходником стартового
кадра приседа был мужик, стоящий в полный рост, а нарисовался присед в нижней
точке: фото модель посмотрела, а нарисовала «присед вообще». Оба кадра вышли
одной и той же нижней точкой, движения между ними ноль, и клипу браться неоткуда.
Поэтому позу с фото сначала читает vision-модель, и в рисование она едет ещё и
фразой — текст модель слушается, картинку по настроению.

Второй кадр рисуется от ПЕРВОГО, а не от аватарки: иначе одежда, обувь и
крупность выводятся заново и меняются прямо посреди повтора (в первом прогоне
золотые авиаторы стали чёрными, а белые кроссовки — чёрными).

Примеры:

    python scripts/gen_exercise_demos.py status
    python scripts/gen_exercise_demos.py frames --exercise "Присед со штангой"
    python scripts/gen_exercise_demos.py video  --exercise "Присед со штангой"
    python scripts/gen_exercise_demos.py loop   --exercise "Присед со штангой"
    python scripts/gen_exercise_demos.py install --exercise "Присед со штангой"

    python scripts/gen_exercise_demos.py pilot --dry-run   # пять упражнений, без трат
    python scripts/gen_exercise_demos.py pilot

Персонаж не только описывается словами, но и уходит в запрос картинкой —
media/push/coach_incoming_call.jpg, эталон из TONE_OF_VOICE.md. Без неё
получается «примерно такой же» качок, а на сотне упражнений это сотня похожих
мужиков вместо одного тренера.

Нужно из окружения:

    OPENAI_API_KEY   кадры (gpt-image-1): принимает эталон и позу двумя
                     картинками сразу. Тот же ключ, что расшифровывает голосовые
    NOVITA_API_KEY   видео (Vidu Q1): принимает ОБА кадра и рисует движение
                     между ними. Тот же ключ, что разбирает технику по видео
    ffmpeg           только для стадии loop

Кадры можно увести на Novita (--frames-via novita), но там вход одна картинка,
эталон тренера туда не влезает — это запасной путь, а не равный.

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

# Эталон персонажа уходит в запрос картинкой, а не только словами. Описание
# задаёт «примерно такого же» качка, и на сотне упражнений это разъехалось бы
# в сотню похожих мужиков; тот самый тренер получается только с его же кадром
# на входе. Файл — эталон из TONE_OF_VOICE.md, тот же, что под пушами.
COACH_REFERENCE = ROOT / "media" / "push" / "coach_incoming_call.jpg"

# Поза уходит и картинкой, и СЛОВАМИ. Первый живой прогон показал, зачем:
# на приседе исходником был мужик, стоящий в полный рост, а нарисовался присед
# в нижней точке — фото модель посмотрела, но нарисовала «ну, присед вообще».
# Оба кадра вышли одинаковой нижней точкой, то есть движения между ними ноль
# и клипу браться неоткуда. Текст модель слушается, картинку — по настроению,
# поэтому позу с фото сперва читает vision-модель (стадия poses), а в рисование
# она едет уже фразой.
FRAME_INSTRUCTION = (
    "The FIRST image is the character, style and framing reference: this exact "
    "man — his face, build, sunglasses, chain, tank top, shorts, shoes — the "
    "drawing style, the gym around him and the camera distance must all stay "
    "identical. Only his body position changes. The SECOND image is the pose "
    "reference. Copy its body pose, camera angle, limb positions and equipment "
    "exactly; the pose is the source of truth, do not correct or improve it. "
    "Show the whole body from head to feet. The pose to draw is: "
)

OPENAI_FRAME_MODEL = os.getenv("OPENAI_FRAME_MODEL", "gpt-image-1")
OPENAI_IMAGE_EDIT_URL = os.getenv(
    "OPENAI_IMAGE_EDIT_URL", "https://api.openai.com/v1/images/edits"
)
OPENAI_CHAT_URL = os.getenv("OPENAI_CHAT_URL", "https://api.openai.com/v1/chat/completions")
OPENAI_VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-4o-mini")

# Прочитанные позы лежат файлом и коммитятся: читаются один раз, а
# перегенерировать кадры приходится многократно, и платить за одно и то же
# чтение каждый раз незачем.
POSES_PATH = pathlib.Path(__file__).resolve().parent / "exercise_poses.json"
POSE_QUESTION = (
    "Describe ONLY the body position of the person in this exercise photo, in "
    "one English sentence, for an artist who will redraw it. Say whether he is "
    "standing, sitting or lying, how the legs and arms are bent, where the "
    "weight is, and how far through the movement he is (start / top / bottom). "
    "Do not describe the gym, his clothes or his appearance."
)

# Кем рисуются кадры; ставится флагом --frames-via, по умолчанию OpenAI.
# У Novita img2img вход одна картинка, то есть эталон тренера туда не влезает
# и персонаж задаётся только словами — это запасной путь, а не равный.
FRAMES_VIA = "openai"


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


def _openai_key() -> str:
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        sys.exit(
            "Нет OPENAI_API_KEY в окружении. Это тот же ключ, которым бот "
            "расшифровывает голосовые. Либо переключись: --frames-via novita."
        )
    return key


def _read_pose(photo: pathlib.Path) -> str:
    """Что за поза на фото — одной фразой, глазами vision-модели.

    Читается по самому фото, а не сочиняется по названию упражнения: фото и
    есть источник правды про технику, а словами оно становится только чтобы
    рисующая модель его наконец услышала.
    """
    payload = {
        "model": OPENAI_VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": POSE_QUESTION},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{_b64(photo)}"},
                    },
                ],
            }
        ],
    }
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        OPENAI_CHAT_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {_openai_key()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            answer = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise GenError(f"OpenAI HTTP {e.code}\n{e.read().decode(errors='replace')}") from e
    return answer["choices"][0]["message"]["content"].strip()


def _load_poses() -> dict[str, str]:
    if POSES_PATH.exists():
        return json.loads(POSES_PATH.read_text())
    return {}


def _ensure_poses(slug: str, dry_run: bool, force: bool) -> dict[str, str]:
    """Позы обоих кадров упражнения, читая недостающие и складывая в файл."""
    poses = _load_poses()
    for index in (1, 2):
        key = f"{slug}_{index}"
        if key in poses and not force:
            continue
        if dry_run:
            poses[key] = f"<описание позы с {slug}_{index}.jpg>"
            continue
        print(f"    читаю позу с {slug}_{index}.jpg")
        poses[key] = _read_pose(MEDIA_DIR / f"{slug}_{index}.jpg")
        POSES_PATH.write_text(json.dumps(poses, indent=2, ensure_ascii=False, sort_keys=True))
    return poses


def _multipart(fields: dict[str, str], files: list[tuple[str, pathlib.Path]]) -> tuple[bytes, str]:
    """Тело multipart/form-data. Граница фиксированная: случайность тут ничего
    не даёт, а воспроизводимый запрос удобнее отлаживать."""
    boundary = "----trainingLogBotExerciseDemos"
    body = bytearray()
    for name, value in fields.items():
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        body += f"{value}\r\n".encode()
    for name, path in files:
        body += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
            f'filename="{path.name}"\r\nContent-Type: image/jpeg\r\n\r\n'
        ).encode()
        body += path.read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return bytes(body), boundary


def _frame_prompt(pose: str) -> str:
    return f"{FRAME_INSTRUCTION}{pose} {CHARACTER}"


def _frame_via_openai(
    reference: pathlib.Path, source: pathlib.Path, pose: str, dest: pathlib.Path
) -> None:
    """Кадр через gpt-image-1: эталон и поза уходят двумя картинками плюс фразой.

    `reference` — не всегда аватарка. Второй кадр упражнения рисуется от ПЕРВОГО,
    уже принятого: иначе от аватарки заново выводятся очки, кроссовки и крупность,
    и посреди повтора они менялись бы прямо в кадре (в первом прогоне менялись:
    золотые авиаторы становились чёрными, белые кроссовки — чёрными).

    Запрос собирается руками, мимо пакета openai: в проекте он пришпилен к
    1.57.0, а тот отвергает список в `image` ещё на своей стороне
    («Expected entry at `image` to be bytes… received list»). Дёргать версию
    ради офлайн-скрипта — менять зависимость работающего бота; HTTP-вызов же
    ничего в боте не трогает, а поля у эндпоинта те же самые.
    """
    key = _openai_key()
    if not reference.exists():
        sys.exit(f"Нет эталона для кадра: {reference}")
    body, boundary = _multipart(
        {
            "model": OPENAI_FRAME_MODEL,
            "prompt": _frame_prompt(pose),
            "size": "1024x1024",
            "n": "1",
        },
        # Порядок значим: промпт ссылается на «первую» и «вторую» картинку.
        [("image[]", reference), ("image[]", source)],
    )
    req = urllib.request.Request(
        OPENAI_IMAGE_EDIT_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise GenError(f"OpenAI HTTP {e.code}\n{e.read().decode(errors='replace')}") from e
    try:
        image_b64 = payload["data"][0]["b64_json"]
    except (KeyError, IndexError) as e:
        raise GenError(f"OpenAI: ответ без картинки: {json.dumps(payload)[:400]}") from e
    dest.write_bytes(base64.b64decode(image_b64))


def _print_frame_request(reference: pathlib.Path, source: pathlib.Path, pose: str) -> None:
    """Сухой прогон. Промпт персонажа один на все упражнения и длинный — целиком
    он занял бы весь экран по два раза на упражнение."""
    prompt_note = f"…инструкция + CHARACTER ({len(CHARACTER)} симв.)"
    if FRAMES_VIA == "openai":
        print(f"    POST {OPENAI_IMAGE_EDIT_URL}, model={OPENAI_FRAME_MODEL}")
        print(f"    image=[{reference.name} (эталон), {source.name} (поза)]")
        print(f"    поза словами: {pose}")
        print(f"    prompt={FRAME_INSTRUCTION[:60]}{prompt_note}")
        return
    payload = _frame_payload(source)
    payload["request"] = dict(
        payload["request"],
        image_base64=f"<{source.name} base64>",
        prompt=f"{POSE_INSTRUCTION[:60]}{prompt_note}",
    )
    print(f"    POST {NOVITA_BASE}/{NOVITA_FRAME_PATH}")
    print(f"    {json.dumps(payload, ensure_ascii=False)}")


def cmd_frames(exercise: str, dry_run: bool, force: bool) -> None:
    slug = _slug(exercise)
    work = _work(slug, create=not dry_run)
    poses = _ensure_poses(slug, dry_run, force=False)
    sources = [MEDIA_DIR / f"{slug}_1.jpg", MEDIA_DIR / f"{slug}_2.jpg"]
    for index, (source, name) in enumerate(zip(sources, ("start.jpg", "end.jpg"), strict=True), 1):
        dest = work / name
        # Второй кадр наследует персонажа от первого, а не от аватарки: так
        # одежда, обувь, зал и крупность гарантированно те же, и между кадрами
        # меняется только тело.
        reference = COACH_REFERENCE if index == 1 else work / "start.jpg"
        pose = poses.get(f"{slug}_{index}", "")
        if dest.exists() and not force:
            print(f"  {name}: уже есть, пропускаю (--force чтобы перегенерить)")
            continue
        print(f"  {name}: {source.name} + {reference.name} → кадр")
        if dry_run:
            _print_frame_request(reference, source, pose)
            continue
        if FRAMES_VIA == "openai":
            _frame_via_openai(reference, source, pose, dest)
        else:
            task_id = _start_task(NOVITA_FRAME_PATH, _frame_payload(source))
            _download(_wait_for_task(task_id)[0], dest)
        print(f"    готово: {dest}")
    if not dry_run:
        print(f"  Посмотри оба кадра глазами: {work}")
        print("  Позы должны ОТЛИЧАТЬСЯ — это начало и конец движения.")
        print("  Кривой кадр — перегенери: frames --force. Оба честные — гони video.")


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
    parser.add_argument(
        "--frames-via",
        choices=["openai", "novita"],
        default="openai",
        help="кто рисует кадры: openai (эталон тренера картинкой) или novita (только словами)",
    )
    args = parser.parse_args()

    global FRAMES_VIA
    FRAMES_VIA = args.frames_via

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
