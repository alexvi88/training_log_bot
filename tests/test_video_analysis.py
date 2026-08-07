"""video_analysis: чистка ответа модели и текст наблюдений для тренера.

Оба сценария в _sanitize взяты с живых прогонов Qwen3-VL, а не придуманы:
модель писала в not_visible «напряжение мышц кора и ягодиц», сама же поясняя,
что это не ограничение ракурса, и одновременно держала usable=true с пустым
problem при списке «судить нельзя» из четырёх пунктов.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
import video_analysis

pytestmark = pytest.mark.asyncio


def _analysis(**over):
    base = {
        "exercise": "становая тяга",
        "reps_seen": 3,
        "view": {"angle": "сбоку", "usable": True, "problem": ""},
        "description": [
            {"aspect": "траектория снаряда", "what_i_see": "гриф идёт по ногам", "when": "0:04"}
        ],
        "observations": [],
        "not_visible": [],
        "camera_advice": "",
    }
    base.update(over)
    return base


async def test_unobservable_items_dropped_from_not_visible():
    """«Напряжение мышц» — не ограничение ракурса, и промптом это не лечится."""
    out = video_analysis._sanitize(
        _analysis(
            view={"angle": "сзади", "usable": False, "problem": "спина к камере"},
            not_visible=[
                {"what": "напряжение мышц кора и ягодиц", "why": "не видно по телу"},
                {"what": "дыхание и ритм", "why": "не видно по грудной клетке"},
                {"what": "положение стоп", "why": "обрезаны по щиколотки"},
            ],
        )
    )
    what = [item["what"] for item in out["not_visible"]]
    assert what == ["положение стоп"]


async def test_long_not_visible_trimmed_when_view_declared_fine():
    """usable=true с пустым problem и «судить нельзя» на четыре пункта — отписка."""
    out = video_analysis._sanitize(
        _analysis(
            not_visible=[
                {"what": "изгиб позвоночника", "why": "виден только силуэт"},
                {"what": "положение лопаток", "why": "камера сбоку"},
                {"what": "положение головы", "why": "не разглядеть"},
                {"what": "угол таза", "why": "не определить"},
            ]
        )
    )
    assert len(out["not_visible"]) == 2


async def test_not_visible_kept_when_view_really_bad():
    """А когда ракурс признан плохим — список ограничений законный, не режем."""
    out = video_analysis._sanitize(
        _analysis(
            view={"angle": "сзади", "usable": False, "problem": "обрезано по колено"},
            not_visible=[
                {"what": "изгиб позвоночника", "why": "спина к камере"},
                {"what": "траектория грифа", "why": "обрезано по колено"},
                {"what": "угол в колене", "why": "колени вне кадра"},
            ],
        )
    )
    assert len(out["not_visible"]) == 3


async def test_observation_without_evidence_dropped():
    """Поле evidence — весь смысл затеи: без него наблюдение дописано, а не увидено."""
    out = video_analysis._sanitize(
        _analysis(
            observations=[
                {"what": "круглая спина", "evidence": "на 0:05 поясница выгнута наружу",
                 "severity": "мешает", "confidence": "высокая"},
                {"what": "колени внутрь", "evidence": "   ", "severity": "мешает"},
                {"what": "рывок с пола", "severity": "мелочь"},
            ]
        )
    )
    assert [o["what"] for o in out["observations"]] == ["круглая спина"]


async def test_observations_sorted_by_severity():
    """Тренеру важное подаётся первым, иначе он озвучит мелочь как главное."""
    out = video_analysis._sanitize(
        _analysis(
            observations=[
                {"what": "мелочь", "evidence": "видно", "severity": "мелочь"},
                {"what": "главное", "evidence": "видно", "severity": "мешает"},
                {"what": "среднее", "evidence": "видно", "severity": "стоит поправить"},
            ]
        )
    )
    assert [o["what"] for o in out["observations"]] == ["главное", "среднее", "мелочь"]


async def test_context_block_states_empty_findings_honestly():
    """Пустой разбор не должен превращаться в «идеально» — это разные утверждения."""
    block = video_analysis.to_context_block(_analysis())
    assert "Отклонений не нашли" in block
    assert "Не выдумывай ошибку" in block
    assert "становая тяга" in block


async def test_context_block_carries_severity_and_confidence():
    block = video_analysis.to_context_block(
        _analysis(
            observations=[{
                "what": "круглая спина", "phase": "съём", "when": "0:05", "reps": [2, 3],
                "evidence": "поясница выгнута наружу", "severity": "мешает",
                "confidence": "низкая",
            }]
        )
    )
    assert "мешает" in block and "низкая" in block
    assert "поясница выгнута наружу" in block
    # Тренеру прямо сказано, как обращаться с неуверенным наблюдением.
    assert "предположение" in block


async def test_analyze_logs_real_usage_for_pricing(monkeypatch):
    """Цену считает дневной отчёт по usage — значит логировать надо настоящий."""
    logged = {}

    async def fake_log(
        user_id, event_type, *, model=None, prompt_tokens=0, completion_tokens=0,
        cached_tokens=0, reasoning_tokens=0,
    ):
        logged.update(
            user_id=user_id, event_type=event_type, model=model,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        )

    monkeypatch.setattr(video_analysis.db, "log_cost_event", fake_log)
    monkeypatch.setattr(config, "NOVITA_API_KEY", "test-key")

    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(_analysis())))],
            usage=SimpleNamespace(prompt_tokens=24000, completion_tokens=900),
        )
    )
    monkeypatch.setattr(video_analysis, "_get_client", lambda: client)

    out = await video_analysis.analyze(b"fake-mp4-bytes", 42)

    assert out is not None
    assert logged["event_type"] == "llm_call"
    assert logged["model"] == config.NOVITA_VIDEO_MODEL
    assert logged["prompt_tokens"] == 24000
    assert logged["completion_tokens"] == 900
    # Модель в этом прайсе должна быть, иначе отчёт посчитает её по дефолтной
    # ставке Grok и цифра уедет.
    assert config.NOVITA_VIDEO_MODEL in config.LLM_PRICES_USD_PER_1K


async def test_analyze_sends_video_as_data_url_not_telegram_link(monkeypatch):
    """В URL файла Telegram лежит токен бота — наружу он уехать не должен."""
    monkeypatch.setattr(video_analysis.db, "log_cost_event", AsyncMock())
    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(_analysis())))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )
    )
    monkeypatch.setattr(video_analysis, "_get_client", lambda: client)

    await video_analysis.analyze(b"bytes", 1)

    content = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
    video_part = next(p for p in content if p["type"] == "video_url")
    assert video_part["video_url"]["url"].startswith("data:video/mp4;base64,")
    assert "api.telegram.org" not in video_part["video_url"]["url"]


async def test_analyze_returns_none_on_non_json(monkeypatch):
    """Мусор вместо JSON — тренер отвечает без видео, а не падает."""
    monkeypatch.setattr(video_analysis.db, "log_cost_event", AsyncMock())
    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Оценка техники: 8.5/10 💪"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )
    )
    monkeypatch.setattr(video_analysis, "_get_client", lambda: client)

    assert await video_analysis.analyze(b"bytes", 1) is None


async def test_analyze_returns_none_when_provider_fails(monkeypatch):
    monkeypatch.setattr(video_analysis.db, "log_cost_event", AsyncMock())
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=RuntimeError("502"))
    monkeypatch.setattr(video_analysis, "_get_client", lambda: client)

    assert await video_analysis.analyze(b"bytes", 1) is None
