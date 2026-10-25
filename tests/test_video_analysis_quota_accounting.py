"""Разбор видео: что списывается после ответа провайдера — на настоящей базе.

`video_analysis.analyze` сам списывает квоту за каждый состоявшийся платный
вызов (вызывающие этого не делают): валидный разбор — основная квота видео,
мусор — мягкий счётчик провалов (`KIND_VIDEO_FAIL`), без которого ролики
можно гонять за наш счёт бесконечно. Ответ-JSON не объектом (список, число)
— тоже провал, и эта ветка не исполнялась; как и «все модели сняты» (404 у
всех) — тогда денег не ушло и списывать нечего, — и сбой записи цены, который
не должен терять уже оплаченный разбор.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from openai import NotFoundError

import ai_limits
import config
import video_analysis

pytestmark = pytest.mark.asyncio


def _good():
    return {
        "exercise": "становая тяга",
        "reps_seen": 3,
        "view": {"angle": "сбоку", "usable": True, "problem": ""},
        "checklist": [
            {"point": p, "verdict": "норма", "what_i_see": f"по {p} видно", "when": "0:04"}
            for p in video_analysis.CHECKPOINTS
        ],
        "observations": [],
        "not_visible": [],
        "camera_advice": "",
    }


def _client_answering(content):
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=100),
    ))
    return client


async def _counters(db, user_id):
    return (
        await db.get_ai_video_count_today(user_id),
        await db.get_ai_usage_today(user_id, ai_limits.KIND_VIDEO_FAIL),
    )


async def _cost_events(db):
    cur = await db.conn().execute("SELECT COUNT(*) FROM cost_events")
    return (await cur.fetchone())[0]


@pytest.fixture(autouse=True)
def _models(monkeypatch):
    monkeypatch.setattr(config, "NOVITA_API_KEY", "test-key")
    monkeypatch.setattr(video_analysis, "_gone_models", set())


async def test_valid_analysis_spends_the_main_video_quota(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(video_analysis, "_get_client", lambda: _client_answering(json.dumps(_good())))

    assert await video_analysis.analyze(b"mp4", user_id) is not None

    assert await _counters(fresh_db, user_id) == (1, 0)
    assert await _cost_events(fresh_db) == 1


@pytest.mark.parametrize("content", ["[1, 2, 3]", "42", '"просто строка"', "не JSON вовсе"])
async def test_paid_but_unusable_answer_counts_as_a_failure_not_a_free_retry(
    fresh_db, user_id, monkeypatch, content
):
    monkeypatch.setattr(video_analysis, "_get_client", lambda: _client_answering(content))

    assert await video_analysis.analyze(b"mp4", user_id) is None

    # Основная квота не наказана, но провал посчитан, и цена записана —
    # деньги провайдеру ушли.
    assert await _counters(fresh_db, user_id) == (0, 1)
    assert await _cost_events(fresh_db) == 1


async def test_every_model_gone_spends_nothing(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "NOVITA_VIDEO_MODEL", "gone/a")
    monkeypatch.setattr(config, "NOVITA_VIDEO_FALLBACK_MODELS", ["gone/b"])

    async def gone(**_kwargs):
        raise NotFoundError(
            "model not found",
            response=httpx.Response(404, request=httpx.Request("POST", "https://x")),
            body=None,
        )

    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=gone)
    monkeypatch.setattr(video_analysis, "_get_client", lambda: client)

    assert await video_analysis.analyze(b"mp4", user_id) is None

    assert await _counters(fresh_db, user_id) == (0, 0)
    assert await _cost_events(fresh_db) == 0
    assert video_analysis._gone_models == {"gone/a", "gone/b"}


async def test_failed_cost_logging_keeps_the_paid_analysis(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(video_analysis, "_get_client", lambda: _client_answering(json.dumps(_good())))

    async def broken_log(*_args, **_kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(video_analysis.db, "log_cost_event", broken_log)

    assert await video_analysis.analyze(b"mp4", user_id) is not None
    assert await _counters(fresh_db, user_id) == (1, 0)
