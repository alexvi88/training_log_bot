"""Разбор видео по своим кадрам (путь "openai").

Ролики для тестов генерирует тот же ffmpeg из imageio-ffmpeg, которым режет
кадры прод (testsrc — синтетическая картинка, без файлов в репозитории).
Модель — поддельный chat-клиент, как у остальных машинных вызовов тренера
(chat/completions, один ответ): проверяем, что уходит (кадры по порядку,
модель зрения, effort, строгая схема, ключ кэша) и что приходит (цена в
cost_events, квота, чистка).
"""

import json
import os
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import ai_trainer
import config
import i18n
import video_analysis


def _make_clip(seconds: float, size: str = "1280x720", fmt: str = "mp4", rate: int = 25) -> bytes:
    import imageio_ffmpeg

    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, f"clip.{fmt}")
        args = [
            imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size={size}:rate={rate}",
        ]
        if fmt == "mp4":
            args += ["-pix_fmt", "yuv420p", "-c:v", "mpeg4"]
        subprocess.run(args + [out], check=True, capture_output=True, timeout=60)
        with open(out, "rb") as fh:
            return fh.read()


@pytest.fixture(scope="module")
def clip_3s() -> bytes:
    return _make_clip(3)


def _jpeg_size(data: bytes) -> tuple[int, int]:
    import io

    from PIL import Image

    return Image.open(io.BytesIO(data)).size


# ---------- нарезка ----------


@pytest.mark.parametrize(
    "duration,count",
    [(0.5, 6), (3, 6), (8, 16), (8.03, 16), (20, 40), (60, 40), (None, 40)],
)
def test_frame_count_is_proportional_to_length_with_floor_and_cap(duration, count):
    """2 кадра в секунду; пол 6 (короткий клип всё же даёт верх и низ),
    потолок — от MAX_VIDEO_SECONDS (20 с → 40)."""
    assert round(config.MAX_VIDEO_SECONDS * config.VIDEO_FRAMES_PER_SECOND) == config.VIDEO_FRAMES_MAX
    n, step, start = video_analysis.frame_plan(duration)
    assert n == count
    if duration:
        # кадры ровно покрывают весь ролик: первый на полшага, последний — за полшага до конца
        assert start == pytest.approx(step / 2)
        assert start + (n - 1) * step == pytest.approx(duration - step / 2)


def test_extracts_evenly_spaced_downscaled_jpegs(clip_3s):
    frames, duration = video_analysis.extract_frames(clip_3s)
    assert duration == pytest.approx(3, abs=0.1)
    assert [f.index for f in frames] == list(range(1, 7))
    assert all(f.jpeg[:2] == b"\xff\xd8" for f in frames)
    seconds = [f.seconds for f in frames]
    assert seconds == sorted(seconds) and seconds[0] == pytest.approx(0.25, abs=0.01)
    # 1280×720 вписан в 768×512 без искажения пропорций
    assert _jpeg_size(frames[0].jpeg) == (768, 432)
    # кадры разные (testsrc движется), а не один и тот же
    assert len({f.jpeg for f in frames}) == len(frames)


def test_portrait_clip_fits_the_portrait_box():
    frames, _ = video_analysis.extract_frames(_make_clip(1, size="720x1280"))
    w, h = _jpeg_size(frames[0].jpeg)
    assert (w, h) == (432, 768)


def test_small_clip_is_not_upscaled():
    frames, _ = video_analysis.extract_frames(_make_clip(1, size="320x240"))
    assert _jpeg_size(frames[0].jpeg) == (320, 240)


def test_gif_animation_is_read_and_duplicates_dropped():
    """Telegram-анимация бывает image/gif; у GIF на 2 кадра в секунду при пороге
    в 6 кадров fps-фильтр продублировал бы кадры — повторы не шлём."""
    frames, _ = video_analysis.extract_frames(_make_clip(1, size="320x240", fmt="gif", rate=2))
    assert 1 <= len(frames) <= 3
    assert len({f.jpeg for f in frames}) == len(frames)
    assert [f.index for f in frames] == list(range(1, len(frames) + 1))


@pytest.mark.parametrize("payload", [b"", b"not a video at all" * 100, b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 500])
def test_corrupt_upload_gives_none(payload):
    assert video_analysis.extract_frames(payload) is None


# ---------- выбор пути ----------


@pytest.mark.parametrize(
    "choice,openai_key,novita_key,expected",
    [
        ("openai", "sk", "nv", "openai"),
        ("openai", "", "nv", "novita"),
        ("novita", "sk", "nv", "novita"),
        ("novita", "sk", "", "openai"),
        ("garbage", "sk", "nv", "novita"),
        ("openai", "", "", None),
    ],
)
def test_backend_choice_and_fallback(monkeypatch, choice, openai_key, novita_key, expected):
    monkeypatch.setattr(config, "VIDEO_ANALYSIS_BACKEND", choice)
    monkeypatch.setattr(config, "OPENAI_API_KEY", openai_key)
    monkeypatch.setattr(config, "NOVITA_API_KEY", novita_key)
    assert config.video_analysis_backend() == expected
    assert config.video_analysis_available() is (expected is not None)
    assert video_analysis.is_configured() is (expected is not None)


def test_vision_model_has_a_price():
    assert config.OPENAI_VISION_MODEL in config.LLM_PRICES_USD_PER_1K


# ---------- путь "openai" целиком ----------


def _checklist(**over):
    return [
        {
            "point": p, "verdict": over.get(p, "норма"),
            "what_i_see": f"по {p} на кадрах 2–5 видно вот это", "frames": [2, 5], "when": "1.0 с",
        }
        for p in video_analysis.CHECKPOINTS
    ]


def _answer(**over):
    base = {
        "view": {"angle": "сбоку", "head_side": "справа", "feet_side": "слева", "usable": True, "problem": ""},
        "exercise": "присед со штангой",
        "exercise_confidence": "высокая",
        "reps": [
            {"rep": 1, "from_frame": 1, "to_frame": 3, "top_frame": 1, "bar_at_top": "над серединой стопы",
             "bottom_frame": 2, "bar_at_bottom": "над серединой стопы", "note": ""},
            {"rep": 2, "from_frame": 4, "to_frame": 6, "top_frame": 4, "bar_at_top": "над серединой стопы",
             "bottom_frame": 5, "bar_at_bottom": "над серединой стопы", "note": ""},
        ],
        "reps_seen": 2,
        "bar_path": {"top": "не видно", "touch": "не видно"},
        "checklist": _checklist(**{"колени и локти": "отклонение"}),
        "observations": [{
            "point": "колени и локти", "what": "колени сваливаются внутрь на подъёме", "phase": "подъём",
            "frames": [2, 5], "when": "1.0 с", "reps": [1, 2],
            "evidence": "на кадрах 2 и 5 колени внутри линии стоп", "severity": "стоит поправить",
            "confidence": "высокая",
        }],
        "not_visible": [],
        "camera_advice": "",
    }
    base.update(over)
    return base


@pytest.fixture
def luna(monkeypatch):
    """Поддельная модель зрения + перехват цены и квоты."""
    monkeypatch.setattr(config, "VIDEO_ANALYSIS_BACKEND", "openai")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")
    # один голос — проверяем сам вызов; голосование — отдельными тестами ниже
    monkeypatch.setattr(config, "VIDEO_ANALYSIS_VOTES", 1)
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(_answer(), ensure_ascii=False)))],
        usage=SimpleNamespace(
            prompt_tokens=7400, completion_tokens=3100,
            prompt_tokens_details=SimpleNamespace(cached_tokens=3000),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=1900),
        ),
    ))
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)
    costs = []

    async def fake_cost(user_id, event_type, **kwargs):
        costs.append({"user_id": user_id, "event_type": event_type, **kwargs})

    monkeypatch.setattr(ai_trainer.db, "log_cost_event", fake_cost)
    monkeypatch.setattr(ai_trainer.db, "is_open", lambda: True)
    video_ok = AsyncMock()
    video_fail = AsyncMock()
    monkeypatch.setattr(video_analysis.db, "increment_ai_video_count", video_ok)
    monkeypatch.setattr(video_analysis.db, "increment_ai_usage", video_fail)
    novita = MagicMock()
    novita.chat.completions.create = AsyncMock(side_effect=AssertionError("novita must not be called"))
    monkeypatch.setattr(video_analysis, "_get_client", lambda: novita)
    return SimpleNamespace(client=client, costs=costs, video_ok=video_ok, video_fail=video_fail)


async def test_frames_go_to_the_vision_model_in_order_and_come_back_parsed(luna, clip_3s):
    out = await video_analysis.analyze(clip_3s, 42, exercise_hint="присед")

    kwargs = luna.client.chat.completions.create.await_args.kwargs
    assert kwargs["model"] == config.OPENAI_VISION_MODEL
    assert kwargs["extra_body"]["reasoning_effort"] == config.VIDEO_ANALYSIS_REASONING_EFFORT == "medium"
    assert kwargs["extra_body"]["prompt_cache_key"] == "video-42"
    assert kwargs["response_format"]["type"] == "json_schema"
    assert kwargs["response_format"]["json_schema"]["strict"] is True
    assert kwargs["timeout"] == config.VIDEO_ANALYSIS_TIMEOUT_SECONDS
    assert "max_tokens" not in kwargs and kwargs["max_completion_tokens"] >= config.VIDEO_ANALYSIS_MAX_TOKENS

    system, user = kwargs["messages"]
    # системный — первым и неизменным (шапка кэша); всё про ролик — в пользовательском
    assert system == {"role": "system", "content": video_analysis.SYSTEM_PROMPT}
    parts = user["content"]
    assert "присед" in parts[0]["text"] and "Кадров: 6" in parts[0]["text"]
    labels = [p["text"] for p in parts[1:] if p["type"] == "text"]
    images = [p["image_url"] for p in parts if p["type"] == "image_url"]
    assert labels[0] == "кадр 1 из 6, 0.2 с" and labels[-1].startswith("кадр 6 из 6")
    assert len(images) == 6
    assert all(i["url"].startswith("data:image/jpeg;base64,") for i in images)
    assert all(i["detail"] == config.VIDEO_FRAME_DETAIL for i in images)

    assert out["exercise"] == "присед со штангой"
    assert out["view"]["head_side"] == "справа"
    assert [o["what"] for o in out["observations"]] == ["колени сваливаются внутрь на подъёме"]
    assert out["observations"][0]["frames"] == [2, 5]
    assert "кадры 2, 5" in video_analysis.to_context_block(out)

    # цена — настоящим usage, под именем модели зрения, через общую машинку
    assert len(luna.costs) == 1
    cost = luna.costs[0]
    assert cost["event_type"] == "llm_call" and cost["model"] == config.OPENAI_VISION_MODEL
    assert (cost["prompt_tokens"], cost["completion_tokens"], cost["cached_tokens"]) == (7400, 3100, 3000)
    luna.video_ok.assert_awaited_once_with(42)
    luna.video_fail.assert_not_awaited()


async def test_garbage_answer_is_billed_and_counted_as_a_failure(luna, clip_3s):
    luna.client.chat.completions.create.return_value.choices[0].message.content = "8.5/10 💪"
    assert await video_analysis.analyze(clip_3s, 42) is None
    assert len(luna.costs) == 1
    luna.video_ok.assert_not_awaited()
    luna.video_fail.assert_awaited_once()


async def test_provider_error_gives_none(luna, clip_3s):
    luna.client.chat.completions.create.side_effect = RuntimeError("502")
    assert await video_analysis.analyze(clip_3s, 42) is None
    luna.video_ok.assert_not_awaited()


async def test_corrupt_video_costs_nothing(luna):
    assert await video_analysis.analyze(b"garbage" * 50, 42) is None
    luna.client.chat.completions.create.assert_not_awaited()
    assert luna.costs == []
    luna.video_ok.assert_not_awaited()
    luna.video_fail.assert_not_awaited()


async def test_novita_backend_still_sends_the_whole_video(monkeypatch, luna):
    monkeypatch.setattr(config, "NOVITA_API_KEY", "nv")
    novita = MagicMock()
    novita.chat.completions.create = AsyncMock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(_answer(), ensure_ascii=False)))],
        usage=SimpleNamespace(prompt_tokens=7000, completion_tokens=900),
    ))
    monkeypatch.setattr(video_analysis, "_get_client", lambda: novita)

    out = await video_analysis.analyze(b"mp4-bytes", 42, backend="novita")

    assert out is not None
    luna.client.chat.completions.create.assert_not_awaited()
    kwargs = novita.chat.completions.create.await_args.kwargs
    assert kwargs["messages"][0]["content"] == video_analysis.NOVITA_SYSTEM_PROMPT
    assert any(p["type"] == "video_url" for p in kwargs["messages"][1]["content"])


async def test_default_is_qwen_with_frames_fallback(monkeypatch, luna, clip_3s):
    """Основной — Qwen; он ответил мусором — тот же ролик кадрами в модель
    тренера, и неудача Qwen в счётчик неудач не идёт (владелец, 2026-11-02)."""
    monkeypatch.setattr(config, "VIDEO_ANALYSIS_BACKEND", "novita")
    monkeypatch.setattr(config, "NOVITA_API_KEY", "nv")
    novita = MagicMock()
    novita.chat.completions.create = AsyncMock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="8.5/10 💪"))],
        usage=SimpleNamespace(prompt_tokens=7000, completion_tokens=900),
    ))
    monkeypatch.setattr(video_analysis, "_get_client", lambda: novita)

    out = await video_analysis.analyze(clip_3s, 42)

    novita.chat.completions.create.assert_awaited_once()
    luna.client.chat.completions.create.assert_awaited()
    assert out is not None and out["exercise"] == "присед со штангой"
    luna.video_ok.assert_awaited_once_with(42)
    luna.video_fail.assert_not_awaited()


async def test_default_qwen_answer_is_used_without_frames(monkeypatch, luna):
    monkeypatch.setattr(config, "VIDEO_ANALYSIS_BACKEND", "novita")
    monkeypatch.setattr(config, "NOVITA_API_KEY", "nv")
    novita = MagicMock()
    novita.chat.completions.create = AsyncMock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(_answer(), ensure_ascii=False)))],
        usage=SimpleNamespace(prompt_tokens=7000, completion_tokens=900),
    ))
    monkeypatch.setattr(video_analysis, "_get_client", lambda: novita)

    out = await video_analysis.analyze(b"mp4-bytes", 42)

    assert out is not None
    luna.client.chat.completions.create.assert_not_awaited()
    luna.video_ok.assert_awaited_once_with(42)


# ---------- кадры как доказательство ----------


def _sanitize(answer, frames=6, hint=None):
    return video_analysis._sanitize(answer, frame_count=frames, exercise_hint=hint)


async def test_deviation_without_frames_is_dropped():
    answer = _answer()
    answer["checklist"][4]["frames"] = []
    answer["observations"][0]["frames"] = []
    out = _sanitize(answer)
    knees = next(i for i in out["checklist"] if i["point"] == "колени и локти")
    assert knees["verdict"] == "не видно"
    assert out["observations"] == []


async def test_frames_outside_the_clip_do_not_count():
    answer = _answer()
    answer["checklist"][4]["frames"] = [9, 14]
    answer["observations"][0]["frames"] = [0, 7, 30]
    out = _sanitize(answer)
    assert next(i for i in out["checklist"] if i["point"] == "колени и локти")["verdict"] == "не видно"
    assert out["observations"] == []


async def test_rep_comparison_needs_frames_of_both_reps():
    """Живой провал: «второй повтор не доходит на 5 см» на двух одинаковых
    повторах. Сравнение без кадров обоих повторов — догадка."""
    claim = {
        "point": "амплитуда", "what": "второй повтор не доходит до груди", "phase": "опускание",
        "frames": [5], "when": "2.3 с", "reps": [2],
        "evidence": "на кадре 5 гриф останавливается выше, чем в первом повторе",
        "severity": "стоит поправить", "confidence": "средняя",
    }
    out = _sanitize(_answer(observations=[claim]))
    assert out["observations"] == []

    claim["frames"] = [2, 5]  # нижние точки обоих повторов
    out = _sanitize(_answer(observations=[claim]))
    assert [o["what"] for o in out["observations"]] == ["второй повтор не доходит до груди"]


async def test_reps_checkpoint_deviation_without_two_reps_is_demoted():
    answer = _answer(checklist=_checklist(**{"повторы между собой": "отклонение"}), observations=[])
    answer["checklist"][6]["frames"] = [4, 5]  # оба кадра — второй повтор
    out = _sanitize(answer)
    assert next(i for i in out["checklist"] if i["point"] == "повторы между собой")["verdict"] == "не видно"


async def test_novita_path_has_no_frame_numbers_and_is_not_checked():
    answer = _answer()
    answer["observations"][0]["frames"] = []
    out = video_analysis._sanitize(answer)
    assert len(out["observations"]) == 1


# ---------- живой провал 2.11.2026: жим лёжа со стороны ног ----------

_LIVE_TRAJECTORY = "гриф опускается от лица к низу грудины, но на подъёме уходит назад за голову"


def _live_bench(bar_path, reps=None):
    checklist = _checklist(**{"траектория снаряда": "отклонение"})
    checklist[1]["what_i_see"] = _LIVE_TRAJECTORY
    checklist[1]["frames"] = [3, 6]
    return _answer(
        exercise="Bench press",
        view={"angle": "три четверти", "head_side": "справа", "feet_side": "слева", "usable": True, "problem": ""},
        bar_path=bar_path,
        reps=reps if reps is not None else [],
        checklist=checklist,
        observations=[{
            "point": "траектория снаряда",
            "what": "на подъёме гриф уходит назад за голову вместо плеч",
            "phase": "подъём", "frames": [3, 6], "when": "1.5 с", "reps": [1],
            "evidence": "на кадре 6 гриф над лицом, дальше за голову",
            "severity": "мешает", "confidence": "высокая",
        }],
    )


@pytest.mark.parametrize(
    "bar_path",
    [
        {"top": "над плечами", "touch": "низ груди"},
        # то, что модель пишет своими словами, а не нашей строкой из промпта
        {"top": "над плечевыми суставами", "touch": "нижняя часть груди"},
        {"top": "over the shoulders", "touch": "lower chest"},
    ],
)
async def test_live_bench_failure_is_forced_to_normal(bar_path):
    with i18n.use_lang("en"):
        out = _sanitize(_live_bench(bar_path), hint="bench press")
        block = video_analysis.to_context_block(out)
    trajectory = next(i for i in out["checklist"] if i["point"] == "траектория снаряда")
    assert trajectory["verdict"] == "норма"
    assert "за голову" not in trajectory["what_i_see"]
    assert out["observations"] == []
    assert "за голову" not in block


async def test_live_bench_failure_without_bar_path_uses_the_rep_landmarks():
    reps = [
        {"rep": n, "from_frame": a, "to_frame": b, "top_frame": a, "bar_at_top": "над плечевыми суставами",
         "bottom_frame": a + 2, "bar_at_bottom": "касание на низе грудины", "note": ""}
        for n, a, b in ((1, 1, 8), (2, 9, 16))
    ]
    out = _sanitize(_live_bench(None, reps=reps), frames=16, hint="bench press")
    assert next(i for i in out["checklist"] if i["point"] == "траектория снаряда")["verdict"] == "норма"
    assert out["observations"] == []


async def test_bench_with_bar_over_the_face_is_left_to_the_model():
    out = _sanitize(_live_bench({"top": "над лицом или шеей", "touch": "низ груди"}), hint="bench press")
    assert next(i for i in out["checklist"] if i["point"] == "траектория снаряда")["verdict"] == "отклонение"
    assert len(out["observations"]) == 1


def test_prompt_asks_for_orientation_and_landmarks_before_verdicts():
    prompt = video_analysis.SYSTEM_PROMPT
    assert prompt.index("ШАГ 0") < prompt.index("ШАГ 1")
    assert "head_side" in prompt and "feet_side" in prompt
    assert "«влево», «вправо»" in prompt
    assert "кадры\nОБОИХ сравниваемых повторов" in prompt
    schema = video_analysis._response_schema()["json_schema"]["schema"]["properties"]
    order = list(schema)
    assert order.index("view") < order.index("reps") < order.index("checklist")


def test_privacy_pages_no_longer_say_openai_never_sees_the_video():
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "legal"
    ru = (root / "privacy.ru.html").read_text(encoding="utf-8")
    en = (root / "privacy.en.html").read_text(encoding="utf-8")
    assert "само видео OpenAI не получает" not in ru
    assert "OpenAI does not receive the video itself" not in en
    assert "кадры" in ru and "frames" in en


# ---------- scripts/video_eval.py ----------


def _eval_module():
    import importlib.util

    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "video_eval.py")
    spec = importlib.util.spec_from_file_location("video_eval", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_eval_example_expectations_parse_and_check():
    import pathlib

    ev = _eval_module()
    clips = ev.load_expectations(
        pathlib.Path(__file__).resolve().parent.parent / "docs" / "video_eval_expectations.example.json"
    )
    bench = clips[0]
    good = _sanitize(_live_bench({"top": "над плечами", "touch": "низ груди"}), hint="bench press")
    good["reps_seen"] = 2
    assert ev.check(bench, good) == []
    bad = _answer(exercise="жим лёжа", reps_seen=3)
    bad["observations"][0]["what"] = "гриф уходит за голову"
    fails = ev.check(bench, _sanitize(bad))
    assert any("повторов 3" in f for f in fails)
    assert any("за голов" in f for f in fails)
    assert ev.check(bench, None) == ["разбора нет (None)"]



# ---------- голосование (VIDEO_ANALYSIS_VOTES) ----------


def _reply(answer):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(answer, ensure_ascii=False)))],
        usage=SimpleNamespace(prompt_tokens=7000, completion_tokens=3000),
    )


def _clean_bench():
    """Нормальный жим, два повтора, ни одного отклонения."""
    return _answer(
        exercise="жим лёжа", checklist=_checklist(), observations=[],
        bar_path={"top": "над плечами", "touch": "середина груди"},
    )


async def test_default_is_two_votes_on_shifted_frames_billed_twice_quota_once(luna, monkeypatch, clip_3s):
    monkeypatch.setattr(config, "VIDEO_ANALYSIS_VOTES", 2)
    assert video_analysis._vote_phases(2) == [0.25, 0.75]
    out = await video_analysis.analyze(clip_3s, 42)

    calls = luna.client.chat.completions.create.await_args_list
    assert len(calls) == 2
    labels = [
        [p["text"] for p in c.kwargs["messages"][1]["content"][1:] if p["type"] == "text"]
        for c in calls
    ]
    # свой набор кадров у каждого голоса: первый кадр в 0.1 с и в 0.4 с
    assert {labels[0][0], labels[1][0]} == {"кадр 1 из 6, 0.1 с", "кадр 1 из 6, 0.4 с"}
    assert len(luna.costs) == 2
    luna.video_ok.assert_awaited_once_with(42)
    luna.video_fail.assert_not_awaited()
    # оба голоса отметили колени — подтверждено
    assert [o["point"] for o in out["observations"]] == ["колени и локти"]
    assert out["votes"] == 2


async def test_deviation_seen_by_one_vote_only_is_dropped(luna, monkeypatch, clip_3s):
    monkeypatch.setattr(config, "VIDEO_ANALYSIS_VOTES", 2)
    luna.client.chat.completions.create.side_effect = [_reply(_answer()), _reply(_clean_bench())]
    luna.client.chat.completions.create.return_value = None

    out = await video_analysis.analyze(clip_3s, 42)

    knees = next(i for i in out["checklist"] if i["point"] == "колени и локти")
    assert knees["verdict"] == "норма"
    assert out["observations"] == []
    block = video_analysis.to_context_block(out)
    assert "2 независимых прогона" in block and "техника чистая" in block


async def test_one_vote_failing_keeps_the_other_minus_rep_comparisons(luna, monkeypatch, clip_3s):
    """Сбой одного голоса не делает тренера немым на реальной ошибке, но
    сравнение повторов без подтверждения не проходит."""
    monkeypatch.setattr(config, "VIDEO_ANALYSIS_VOTES", 2)
    answer = _answer(checklist=_checklist(**{"колени и локти": "отклонение", "повторы между собой": "отклонение"}))
    answer["checklist"][6]["what_i_see"] = "в первом повторе локти в замке, во втором согнуты (кадры 1 и 4)"
    answer["checklist"][6]["frames"] = [1, 4]
    answer["observations"].append({
        "point": "повторы между собой", "what": "во втором повторе локти не выпрямляются",
        "phase": "верхняя точка", "frames": [1, 4], "when": "2.0 с", "reps": [1, 2],
        "evidence": "в кадре 1 локти прямые, в кадре 4 согнуты", "severity": "стоит поправить",
        "confidence": "средняя",
    })
    luna.client.chat.completions.create.side_effect = [_reply(answer), RuntimeError("502")]

    out = await video_analysis.analyze(clip_3s, 42)

    by_point = {i["point"]: i["verdict"] for i in out["checklist"]}
    assert by_point["колени и локти"] == "отклонение"
    assert by_point["повторы между собой"] == "не видно"
    assert [o["point"] for o in out["observations"]] == ["колени и локти"]
    assert len(luna.costs) == 1  # упавший вызов usage не принёс
    luna.video_ok.assert_awaited_once_with(42)


async def test_all_votes_garbage_counts_one_failure(luna, monkeypatch, clip_3s):
    monkeypatch.setattr(config, "VIDEO_ANALYSIS_VOTES", 2)
    luna.client.chat.completions.create.return_value.choices[0].message.content = "не JSON"
    assert await video_analysis.analyze(clip_3s, 42) is None
    assert len(luna.costs) == 2
    luna.video_fail.assert_awaited_once()
    luna.video_ok.assert_not_awaited()


def test_consensus_needs_every_vote_for_a_checkpoint_and_its_observation():
    a = _sanitize(_answer())
    b_answer = _answer()
    b_answer["observations"] = []  # точку отметил, наблюдения не дал
    b = _sanitize(b_answer)
    out = video_analysis._consensus([a, b])
    assert next(i for i in out["checklist"] if i["point"] == "колени и локти")["verdict"] == "отклонение"
    assert out["observations"] == []


def test_votes_summary_is_one_pasteable_line():
    line = video_analysis._votes_summary([_answer(), None])
    assert "\n" not in line
    assert line.startswith("#1 reps=2 ")
    assert "колени и локти=О[2, 5]" in line and "спина=н" in line
    assert "#2 нет ответа" in line


# ---------- амплитуда и сравнение повторов — только по телу ----------

# Дословно из живого лога 2.11.2026 (нормальный жим, камера в руке): чистое
# сравнение высоты грифа на экране, без замка и касания.
_LIVE_AMPLITUDE = (
    "В первом повторе после нижней точки гриф возвращается только до верхнего положения в "
    "кадре 11; оно ниже верхнего положения в кадрах 1 и 15…"
)
_LIVE_REPS = "Верхняя точка первого цикла в кадре 11 ниже верхней точки второго цикла в кадре 15"


def _live_screen_height_answer():
    answer = _answer(
        exercise="жим лёжа",
        reps=[
            {"rep": 1, "from_frame": 1, "to_frame": 11, "top_frame": 1, "bar_at_top": "над плечами",
             "bottom_frame": 9, "bar_at_bottom": "середина груди", "note": ""},
            {"rep": 2, "from_frame": 11, "to_frame": 15, "top_frame": 11, "bar_at_top": "над плечами",
             "bottom_frame": 13, "bar_at_bottom": "середина груди", "note": ""},
        ],
        bar_path={"top": "над плечами", "touch": "середина груди"},
        checklist=_checklist(**{"амплитуда": "отклонение", "повторы между собой": "отклонение"}),
        observations=[
            {"point": "амплитуда", "what": "первый повтор не доходит до верхнего положения",
             "phase": "верхняя точка", "frames": [1, 11, 15], "when": "5.5 с", "reps": [1],
             "evidence": _LIVE_AMPLITUDE, "severity": "стоит поправить", "confidence": "средняя"},
            {"point": "повторы между собой", "what": "верхняя точка первого повтора ниже второго",
             "phase": "верхняя точка", "frames": [11, 15], "when": "5.5 с", "reps": [1, 2],
             "evidence": _LIVE_REPS, "severity": "мелочь", "confidence": "средняя"},
        ],
    )
    answer["checklist"][3]["what_i_see"] = _LIVE_AMPLITUDE
    answer["checklist"][3]["frames"] = [1, 11, 15]
    answer["checklist"][6]["what_i_see"] = _LIVE_REPS
    answer["checklist"][6]["frames"] = [11, 15]
    return answer


def test_live_screen_height_comparisons_are_dropped():
    with i18n.use_lang("en"):
        out = _sanitize(_live_screen_height_answer(), frames=15, hint="bench press")
    by_point = {i["point"]: i["verdict"] for i in out["checklist"]}
    assert by_point["амплитуда"] == "не видно"
    assert by_point["повторы между собой"] == "не видно"
    assert out["observations"] == []


@pytest.mark.parametrize(
    "text",
    [
        "в верхней точке второго повтора локти согнуты, замка нет (кадр 13)",
        "во втором повторе гриф не касается груди, останавливается над грудью (кадр 13)",
        "second rep: elbows stay bent at the top, no lockout",
    ],
)
def test_amplitude_stated_against_the_body_survives(text):
    answer = _live_screen_height_answer()
    answer["checklist"][3]["what_i_see"] = text
    answer["observations"] = [{**answer["observations"][0], "evidence": text, "frames": [9, 13]}]
    out = _sanitize(answer, frames=15)
    assert next(i for i in out["checklist"] if i["point"] == "амплитуда")["verdict"] == "отклонение"
    assert len(out["observations"]) == 1


def test_frames_prompt_bans_screen_height_comparisons():
    prompt = video_analysis.SYSTEM_PROMPT
    assert "Высоту снаряда между кадрами по экрану НЕ сравнивай" in prompt
    assert "Небольшая разница в верхней точке при выпрямленных локтях — НЕ отклонение" in prompt


def test_coach_prompt_forbids_filler_tips():
    """Живой провал: на чистом жиме тренер дал «каждый повтор до одной точки»,
    «держи траекторию», «контролируй финиш» — ни одна не из наблюдений."""
    prompt = ai_trainer.SYSTEM_PROMPT
    assert "по одной\n  короткой повелительной подсказке НА КАЖДОЕ отклонение" in prompt
    assert "Подсказка без своего отклонения — вода" in prompt
    assert "Поправок при этом не давать ни одной" in prompt
    block = video_analysis.to_context_block(_sanitize(_answer()))
    assert "Подсказок не больше, чем отклонений" in block
