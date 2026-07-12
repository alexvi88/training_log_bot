import os

import exercise_media
from seed_data import EXERCISE_TEMPLATES


def test_media_names_match_a_real_template():
    template_names = {ex_name for _group, ex_name in EXERCISE_TEMPLATES}
    for ex_name in exercise_media.EXERCISE_IMAGE_SLUGS:
        assert ex_name in template_names, f"{ex_name!r} is not in EXERCISE_TEMPLATES"


def test_slugs_are_unique():
    slugs = list(exercise_media.EXERCISE_IMAGE_SLUGS.values())
    assert len(slugs) == len(set(slugs))


def test_every_slug_has_both_image_files_on_disk():
    for ex_name, slug in exercise_media.EXERCISE_IMAGE_SLUGS.items():
        for suffix in ("_1.jpg", "_2.jpg"):
            path = os.path.join(exercise_media.MEDIA_DIR, f"{slug}{suffix}")
            assert os.path.isfile(path), f"missing {path} for {ex_name!r}"


def test_get_images_returns_two_paths_for_known_exercise():
    paths = exercise_media.get_images("Присед со штангой")
    assert len(paths) == 2
    assert all(os.path.isfile(p) for p in paths)


def test_get_images_returns_empty_for_unknown_exercise():
    assert exercise_media.get_images("Совсем не упражнение") == []
