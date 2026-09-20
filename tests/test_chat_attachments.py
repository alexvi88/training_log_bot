"""Вложения к истории чата с AI-тренером — save_photo/save_video_frame/delete
чистым модулем, без HTTP-слоя (его проверяет tests/test_api_v1_ai.py).

Каталог на время теста подменяется во временный (config.AI_CHAT_MEDIA_DIR) —
тот же приём, что в tests/test_exercise_photos.py.
"""

import subprocess

import imageio_ffmpeg
import pytest

import chat_attachments
import config


@pytest.fixture(autouse=True)
def media_dir(tmp_path, monkeypatch):
    path = tmp_path / "ai_chat"
    monkeypatch.setattr(config, "AI_CHAT_MEDIA_DIR", str(path))
    return path


def _real_video_bytes(tmp_path, duration=2) -> bytes:
    """Настоящий, декодируемый ffmpeg-ом mp4 — synthetic testsrc, а не файл
    с диска: не тянуть же в репозиторий бинарник ради одного теста."""
    path = tmp_path / "src.mp4"
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run(
        [ffmpeg, "-y", "-f", "lavfi", "-i", f"testsrc=duration={duration}:size=64x64:rate=5", str(path)],
        capture_output=True, check=True,
    )
    return path.read_bytes()


# ---------- save_photo ----------


def test_save_photo_writes_file_and_returns_name(media_dir):
    name = chat_attachments.save_photo(42, b"\xff\xd8\xff-fake-jpeg-bytes", "jpg")
    assert name.startswith("u42_")
    assert name.endswith(".jpg")
    path = chat_attachments.path_for(name)
    assert path is not None
    with open(path, "rb") as fh:
        assert fh.read() == b"\xff\xd8\xff-fake-jpeg-bytes"


def test_save_photo_two_calls_give_different_names(media_dir):
    """uuid на каждую загрузку — иначе повторная загрузка переписала бы файл
    под тем же адресом, и http-кэш клиента продолжал бы показывать старое."""
    a = chat_attachments.save_photo(1, b"aaa", "jpg")
    b = chat_attachments.save_photo(1, b"bbb", "jpg")
    assert a != b


def test_save_photo_rejects_unsupported_extension(media_dir):
    with pytest.raises(ValueError):
        chat_attachments.save_photo(1, b"data", "gif")


def test_save_photo_rejects_empty_payload(media_dir):
    with pytest.raises(ValueError):
        chat_attachments.save_photo(1, b"", "jpg")


# ---------- path_for: защита от выхода за каталог ----------


def test_path_for_rejects_traversal(media_dir):
    chat_attachments.save_photo(1, b"data", "jpg")
    assert chat_attachments.path_for("../secret") is None
    assert chat_attachments.path_for("../../etc/passwd") is None


def test_path_for_missing_file_returns_none(media_dir):
    assert chat_attachments.path_for("u1_doesnotexist.jpg") is None


def test_path_for_none_returns_none(media_dir):
    assert chat_attachments.path_for(None) is None


# ---------- delete ----------


def test_delete_removes_file(media_dir):
    name = chat_attachments.save_photo(1, b"data", "jpg")
    assert chat_attachments.path_for(name) is not None
    chat_attachments.delete(name)
    assert chat_attachments.path_for(name) is None


def test_delete_missing_file_is_not_an_error(media_dir):
    chat_attachments.delete("u1_nonexistent.jpg")  # не должно поднимать исключение
    chat_attachments.delete(None)


# ---------- save_video_frame ----------


def test_save_video_frame_extracts_a_real_frame(media_dir, tmp_path):
    video_bytes = _real_video_bytes(tmp_path)
    name = chat_attachments.save_video_frame(7, video_bytes)
    assert name is not None
    assert name.startswith("u7_")
    assert name.endswith(".jpg")
    path = chat_attachments.path_for(name)
    assert path is not None
    with open(path, "rb") as fh:
        frame = fh.read()
    assert len(frame) > 0
    # JPEG-магия — уверенность, что это правда картинка, а не мусор.
    assert frame[:2] == b"\xff\xd8"


def test_save_video_frame_short_clip_falls_back_to_first_frame(media_dir, tmp_path):
    """Ролик короче секунды — основной сик на 00:00:01.0 не находит кадра
    (ffmpeg просто не пишет файл), и модуль должен откатиться на 00:00:00.0,
    а не остаться без кадра вовсе."""
    video_bytes = _real_video_bytes(tmp_path, duration=0.3)
    name = chat_attachments.save_video_frame(7, video_bytes)
    assert name is not None


def test_save_video_frame_garbage_input_returns_none(media_dir):
    """Не видео вовсе — ffmpeg честно откажется, и это не должно ронять
    вызывающего (ask_video всё равно должен ответить, см. test_api_v1_ai.py)."""
    name = chat_attachments.save_video_frame(7, b"this is not a video file at all")
    assert name is None


def test_save_video_frame_empty_bytes_returns_none(media_dir):
    assert chat_attachments.save_video_frame(7, b"") is None
