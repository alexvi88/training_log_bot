#!/usr/bin/env python3
"""Обложки гайдов «Дневника атлета» — media/guides/<slug>.jpg, 1024×1024.

    python3 -B scripts/gen_guide_covers.py <куда> [slug,slug,...] [--n 3]

fal.ai, `meta/muse-image/edit` (~$0.01 за кадр), как `exercise-art` в iOS-репо.
Первым референсом — эталон тренера `media/push/coach_reference.jpg`: тот же
тренер, что `coach_incoming_call.jpg` (TONE_OF_VOICE.md, «Стиль картинок»), но с
меньшим числом вен на руках — так решил владелец 2026-11-01 (экран загрузки), сцена — текстом. Ключ fal — секрет среды:
заголовок `Authorization: Key …` подставляет прокси (fal.run, *.fal.run,
fal.media, *.fal.media). Сумму называть владельцу ДО запуска.

На выходе `<куда>/<slug>_<n>.jpg` — варианты; ответ модели приводится к
1024×1024 центральным кропом. Выбранный вариант кладётся в media/guides/<slug>.jpg
руками (JPEG q85).

Промпт = PREFIX + сцена + STYLE (стиль — из TONE_OF_VOICE.md, «Стиль картинок»).
Сцены — SCENES ниже; полный промпт любого сюжета: PREFIX + SCENES[slug] + STYLE.
"""

import argparse
import base64
import concurrent.futures as cf
import io
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
REF = ROOT / "media" / "push" / "coach_reference.jpg"

PREFIX = (
    "Image 1 is the character reference: the gym coach. Draw THE SAME MAN in a new scene: a huge middle-aged "
    "bodybuilder, deep tan, heavy square jaw, short dark-blond hair slicked back, gold aviator sunglasses, thick "
    "gold chain, black tank top. Calm, confident face: not shouting, not grinning, not winking. Only a few thin "
    "subtle veins on the forearms, no bulging vein network, but the muscles keep their crisp comic-book contours "
    "and shading. Scene: "
)
STYLE = (
    " Style exactly like image 1: bold black outlines, flat color fill with soft cel shading on the muscles, "
    "western comic book / arcade cover look; no photorealism, no 3D render; detail in the muscles and iron, the "
    "background simpler than the figure. Palette: warm amber light from a hanging lamp and a sunset window with an "
    "orange city, cold dark-grey iron, red brick wall; tan skin and gold are the only bright spots. A night "
    "basement gym, not a bright white fitness club. Square 1:1 composition, the main subject centered and large, "
    "readable at a glance as a small icon, few small details, empty margins near the corners and the bottom edge. "
    "No text, no letters, no numbers, no logos, no labels, no brands, no watermarks. No other people unless the "
    "scene names them."
)

SCENES = {
    "protein-per-day": "the coach holds a big plain shaker bottle with no print; next to him on a bench a plate with grilled chicken and boiled eggs.",
    "creatine": "the coach pours white powder with a small measuring scoop into a clear glass of water.",
    "whey-protein": "the coach shakes a plain shaker bottle after a workout, a towel over his shoulder.",
    "calories-for-muscle-gain": "the coach sits at a table with one big plate: rice, steak and vegetables.",
    "cutting-without-losing-muscle": "the coach stands on a plain bathroom scale with a blank display, a tape measure in his hand.",
    "caffeine-before-workout": "the coach holds a steaming mug of coffee next to a loaded barbell on a rack.",
    "pre-and-post-workout-meal": "two bowls on a gym bench, oatmeal in one and rice with chicken in the other; the coach points at them.",
    "supplements-that-work": "the coach sorts a few plain jars with no labels on a bench and pushes one jar aside.",
    "how-to-keep-a-workout-log": "the coach writes in a worn paper notebook with a pen; a phone lies face down next to it.",
    "progressive-overload": "the coach slides one more plain black plate onto a loaded barbell sleeve.",
    "rest-between-sets": "the coach sits on a bench between sets holding a stopwatch, resting.",
    "break-a-plateau": "the coach stands next to a loaded barbell on a rack, studying it, hand on his chin.",
    "sets-per-muscle-per-week": "the coach stands at a chalkboard with neat rows of empty chalk check boxes, no letters, no digits.",
    "estimate-1rm-safely": "side view: a regular athlete (normal build, grey t-shirt) lies on a flat bench pressing a barbell; the coach stands behind the athlete's head, leaning over, both of his hands hovering just under the bar ready to catch it, eyes on the bar.",
    "warm-up-before-lifting": "the coach warms up his shoulders pulling a resistance band over his head.",
    "switch-from-hevy-or-strong": "the coach carries a cardboard box full of old notebooks down the basement stairs, moving in.",
    "choose-a-program": "the coach stands at a wall with three blank paper sheets pinned on it and points at one of them.",
    "film-your-set": "in the foreground a smartphone on a small tripod, seen from behind (dark blank screen edge), points at the coach; behind it the coach bench presses a barbell on a flat bench, side view, clearly the subject being filmed.",
    "compound-vs-isolation": "the coach holds one dumbbell in one hand; behind him a loaded barbell on a squat rack.",
    "strength-vs-hypertrophy": "two barbells side by side on the floor, one with many big plates, one with few small plates; the coach stands between them.",
    "how-long-should-a-workout-be": "the coach looks up at a big round wall clock with plain tick marks and no digits, hanging over the rack.",
    "first-week-in-the-gym": "the coach stands in the open doorway of the basement gym with a welcoming open-hand gesture.",
    "common-gym-mistakes": "the coach adjusts the elbows of a regular athlete (normal build, seen from behind) doing a bench press.",
    "strength-training-after-40": "the coach, with a little grey at the temples, does a goblet squat holding one dumbbell.",
    "beginner-gains": "the coach nods approvingly next to a staircase of stacked weight plates growing taller step by step.",
    "junk-volume": "a messy heap of dumbbells thrown on the floor; the coach shakes his head.",
    "beginner-gym-program": "the coach shows an empty barbell with no plates on a rack, his palm on the bar.",
    "muscle-gain-program": "the coach in profile does a dumbbell biceps curl, the lamp light emphasizing his arm.",
    "gym-program-for-women": "a regular athletic woman (normal build, loose plain t-shirt and leggings, not sexualized), three-quarter view, does a goblet squat holding one dumbbell at her chest, thighs parallel to the floor, back straight, heels down; the coach stands beside her, giving a calm thumbs up.",
    "increase-bench-press": "the coach lies on a flat bench pressing a loaded barbell, arms straight at the top.",
    "reps-for-muscle-growth": "the coach mid-set with two dumbbells, muscles under load, focused.",
    "training-frequency": "a wall calendar grid with chalk crosses in some squares, no digits, no letters; the coach stands next to it with a towel.",
}


def uri(p):
    im = Image.open(p).convert("RGB")
    im.thumbnail((1024, 1024))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode()


def call(url, body=None, method="GET"):
    r = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    return json.loads(urllib.request.urlopen(r, timeout=120).read())


def square(src, dst):
    im = Image.open(src).convert("RGB")
    s = min(im.size)
    x, y = (im.width - s) // 2, (im.height - s) // 2
    im.crop((x, y, x + s, y + s)).resize((1024, 1024), Image.LANCZOS).save(dst, "JPEG", quality=85)


def gen(out, slug, n, ref):
    q = call(
        "https://queue.fal.run/meta/muse-image/edit",
        {"prompt": PREFIX + SCENES[slug] + STYLE, "image_urls": [ref], "aspect_ratio": "1:1", "num_images": 1},
        "POST",
    )
    for _ in range(120):
        if call(q["status_url"])["status"] == "COMPLETED":
            break
        time.sleep(3)
    raw = f"{out}/{slug}_{n}.raw.jpg"
    urllib.request.urlretrieve(call(q["response_url"])["images"][0]["url"], raw)
    square(raw, f"{out}/{slug}_{n}.jpg")
    os.remove(raw)
    return f"{slug}_{n}"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("slugs", nargs="?", default=",".join(SCENES))
    ap.add_argument("--n", type=int, default=3)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    ref = uri(REF)
    jobs = [(s, i) for s in a.slugs.split(",") for i in range(1, a.n + 1)]
    with cf.ThreadPoolExecutor(8) as ex:
        fs = {ex.submit(gen, a.out, s, i, ref): (s, i) for s, i in jobs}
        for f in cf.as_completed(fs):
            try:
                print("ok", f.result())
            except urllib.error.HTTPError as e:
                print("ERR", fs[f], e.code, e.read().decode()[:200])
            except Exception as e:
                print("ERR", fs[f], e)
