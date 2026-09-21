"""`api_v1_common.decode_data_url` — оценка размера по длине base64-строки
ДО декодирования (см. докстринг функции и разбор аудита безопасности:
раньше весь base64 уходил в память, и только потом сравнивался с
MAX_*_BYTES)."""

import base64

import pytest

import api_v1_common as common

ApiError = common.ApiError

_EXT_BY_MIME = {"image/png": "png"}


def _data_url(raw: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def test_decode_data_url_rejects_oversized_payload_before_decoding(monkeypatch):
    """Заведомо превышающий потолок payload должен быть отсечён по длине
    base64-строки — `base64.b64decode` не должен даже вызваться."""
    called = False
    real_b64decode = base64.b64decode

    def spy_b64decode(*args, **kwargs):
        nonlocal called
        called = True
        return real_b64decode(*args, **kwargs)

    monkeypatch.setattr(base64, "b64decode", spy_b64decode)

    # ~1.4 МБ закодированной строки при потолке в 4 байта — заведомо больше.
    huge_raw = b"x" * (1024 * 1024)
    data_url = _data_url(huge_raw)

    with pytest.raises(ApiError) as exc_info:
        common.decode_data_url(
            data_url,
            _EXT_BY_MIME,
            field="image_data_url",
            max_bytes=4,
            too_big_error=(400, "photo_too_big", "too big"),
        )
    assert exc_info.value.status_code == 400
    assert exc_info.value.code == "photo_too_big"
    assert exc_info.value.message == "too big"
    assert called is False


def test_decode_data_url_allows_payload_within_limit():
    raw = b"small photo bytes"
    data_url = _data_url(raw)
    decoded, mime, ext = common.decode_data_url(
        data_url,
        _EXT_BY_MIME,
        field="image_data_url",
        max_bytes=len(raw),
        too_big_error=(400, "photo_too_big", "too big"),
    )
    assert decoded == raw
    assert mime == "image/png"
    assert ext == "png"


def test_decode_data_url_without_max_bytes_keeps_old_behavior():
    """Вызывающие, которые не просят лимит (max_bytes не передан), продолжают
    получать полный decode без проверки — обратная совместимость."""
    raw = b"whatever size, nobody asked to cap it here"
    data_url = _data_url(raw)
    decoded, _mime, _ext = common.decode_data_url(data_url, _EXT_BY_MIME, field="image_data_url")
    assert decoded == raw


def test_decode_data_url_post_decode_check_still_catches_borderline_case():
    """Оценка по длине строки — верхняя граница, поэтому то, что проходит
    оценку, гарантированно проходит и точную проверку; но если бы оценка
    была неверной, точная проверка после decode всё равно должна сработать
    (та же ошибка, тот же код)."""
    raw = b"x" * 100
    data_url = _data_url(raw)
    with pytest.raises(ApiError) as exc_info:
        common.decode_data_url(
            data_url,
            _EXT_BY_MIME,
            field="image_data_url",
            max_bytes=10,
            too_big_error=(400, "photo_too_big", "too big"),
        )
    assert exc_info.value.code == "photo_too_big"
