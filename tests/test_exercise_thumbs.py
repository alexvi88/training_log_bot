"""Миниатюры первых кадров каталога (scripts/gen_exercise_thumbs.py).

Строки списков рисуют кадр 52x40 pt, а полный кадр — ~65 КБ; `thumb` в /v1
указывает на уменьшенную копию, а полные кадры карточки остаются как были.
"""

import os
import sys

import httpx
import pytest
from PIL import Image

import api_v1
import exercise_media

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import gen_exercise_thumbs  # noqa: E402


def test_every_first_frame_has_a_thumb():
    """Новый кадр каталога без миниатюры — `python scripts/gen_exercise_thumbs.py`."""
    assert gen_exercise_thumbs.source_frames()
    assert gen_exercise_thumbs.missing() == []


def test_thumbs_are_small_and_wide_enough_for_retina():
    for source in gen_exercise_thumbs.source_frames():
        thumb = gen_exercise_thumbs.thumb_path(source)
        with Image.open(thumb) as img:
            # Плитка 52x40 pt на 3x — 156x120 px: ширины с запасом, высоты хватает.
            assert img.width == gen_exercise_thumbs.THUMB_WIDTH
            assert img.height >= 120, thumb
        assert os.path.getsize(thumb) < 25_000, thumb
        assert os.path.getsize(thumb) < os.path.getsize(source) / 2, thumb


def test_every_catalog_thumb_url_points_at_a_thumb_file():
    seen = 0
    for name in exercise_media.EXERCISE_IMAGE_SLUGS:
        url = exercise_media.thumb_url_for({"name": name, "original_name": name})
        if url is None:
            continue
        seen += 1
        assert url.startswith("/media/exercises/thumbs/"), url
        assert os.path.isfile(os.path.join(exercise_media.MEDIA_DIR, url.removeprefix("/media/exercises/")))
    assert seen


def test_thumb_falls_back_to_full_frame_without_a_thumb_file(monkeypatch, tmp_path):
    monkeypatch.setattr(exercise_media, "THUMBS_DIR", str(tmp_path))
    url = exercise_media.thumb_url_for({"name": "Жим штанги лёжа", "original_name": "Жим штанги лёжа"})
    assert url == "/media/exercises/barbell_bench_press_medium_grip_1.jpg"


@pytest.mark.asyncio
async def test_thumb_is_served_immutable():
    transport = httpx.ASGITransport(app=api_v1.build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        thumb = await client.get("/media/exercises/thumbs/barbell_bench_press_medium_grip_1.jpg")
        full = await client.get("/media/exercises/barbell_bench_press_medium_grip_1.jpg")
    assert thumb.status_code == 200
    assert thumb.headers["content-type"] == "image/jpeg"
    assert thumb.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert len(thumb.content) < len(full.content) / 2
